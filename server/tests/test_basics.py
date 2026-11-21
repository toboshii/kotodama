"""Protocol, text normalisation, archetypes, profile resolution, library."""

from __future__ import annotations

import json
import unittest

from kotodama import protocol
from kotodama.archetypes import Archetypes, stable_hash
from kotodama.library import Library
from kotodama.profiles import ProfileResolver, describe
from kotodama.protocol import Speaker
from kotodama.textnorm import normalize, scrub_player

from helpers import KotodamaTestCase


class ProtocolTests(unittest.TestCase):
    def test_decode_rejects_garbage(self):
        self.assertIsNone(protocol.decode(b""))
        self.assertIsNone(protocol.decode(b"not json"))
        self.assertIsNone(protocol.decode(b"[1,2]"))
        self.assertIsNone(protocol.decode(b'{"no_t": 1}'))
        self.assertIsNone(protocol.decode(b"x" * (protocol.MAX_DATAGRAM + 1)))
        self.assertEqual(protocol.decode(b'{"t":"ping"}'), {"t": "ping"})

    def test_roundtrip_unicode(self):
        msg = {"t": "log", "msg": "Å – 日本語"}
        self.assertEqual(protocol.decode(protocol.encode(msg)), msg)

    def test_parse_speak(self):
        sp = protocol.parse_speak({"t": "speak", "seq": 3, "kind": "cutscene", "mode": 150,
                                   "text": "  Hi!  ", "speaker": {"name": "Prishe", "race": 4}})
        self.assertEqual((sp.seq, sp.kind, sp.text, sp.speaker.name, sp.speaker.race),
                         (3, "cutscene", "Hi!", "Prishe", 4))
        self.assertIsNone(protocol.parse_speak({"text": "   "}))
        self.assertIsNone(protocol.parse_speak({"text": 5}))
        self.assertEqual(protocol.parse_speak({"text": "x", "kind": "weird"}).kind, "ambient")
        self.assertEqual(len(protocol.parse_speak({"text": "a" * 5000}, max_chars=100).text), 100)


class TextTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("  Hello \n\t world~ "), "Hello world")

    def test_scrub_player(self):
        self.assertEqual(scrub_player("Adventurer, meet Adventurers friend. adventurer!", "Adventurer"),
                         "{player}, meet Adventurers friend. {player}!")
        self.assertEqual(scrub_player("anything", None), "anything")


class ArchetypeTests(unittest.TestCase):
    def setUp(self):
        self.arch = Archetypes.load()

    def test_key_selection(self):
        k = self.arch.key_for
        self.assertEqual(k(name="X", race=8, creature=None), "galka")
        self.assertEqual(k(name="X", race=5, creature=None), "taru_m")
        self.assertEqual(k(name="Moogle Guide", race=0, creature=None), "moogle")
        self.assertEqual(k(name="X", race=0, creature="orc"), "orc")
        self.assertEqual(k(name="Nobody", race=None, creature=None), "unknown")
        self.assertEqual(k(name=None, race=None, creature=None, narrator=True), "narrator")
        # creature hint beats race; unknown creature falls through
        self.assertEqual(k(name="X", race=2, creature="goblin"), "goblin")
        self.assertEqual(k(name="X", race=2, creature="nonsense"), "hume_f")

    def test_all_archetypes_are_complete_and_valid(self):
        for key, a in self.arch.items.items():
            for f in ("gender", "age", "pitch", "pace"):
                self.assertIn(a[f], self.arch.vocab[f], f"{key}.{f}")
            for voice in a["voices"]["openai"]:
                self.assertTrue(self.arch.valid_voice("openai", voice), f"{key}: {voice}")

    def test_stable_hash_is_deterministic_and_case_insensitive(self):
        self.assertEqual(stable_hash("Prishe"), stable_hash("prishe"))
        self.assertNotEqual(stable_hash("Prishe"), stable_hash("Shantotto"))

    def test_sanitize(self):
        clean = self.arch.sanitize({"gender": "MALE", "age": "ancient", "tone": " dry   wit ",
                                    "voice": {"openai": "onyx", "other": "x", "bad": 3}, "junk": 1})
        self.assertEqual(clean, {"gender": "male", "tone": "dry wit",
                                 "voice": {"openai": "onyx", "other": "x"}})
        self.assertEqual(self.arch.sanitize("nope"), {})
        with self.assertRaises(ValueError):
            self.arch.sanitize({"age": "ancient"}, strict=True)
        with self.assertRaises(ValueError):
            self.arch.sanitize({"voice": {"openai": "not-a-voice"}}, strict=True)


class ProfileTests(KotodamaTestCase):
    def setUp(self):
        super().setUp()
        self.arch = Archetypes.load()
        cfg = self.make_config()
        self.lib = Library(self.arch, cfg.path("library.file"), cfg.path("library.packs_dir"),
                           cfg.path("library.overrides"), save_delay=0.05)
        self.res = ProfileResolver(self.arch, self.lib)

    def test_same_name_same_voice_different_names_vary(self):
        a = self.res.resolve(Speaker(name="Ayame", race=2), None).profile
        b = self.res.resolve(Speaker(name="Ayame", race=2), 99).profile
        self.assertEqual(a, b)
        voices = {self.res.resolve(Speaker(name=f"NPC {i}", race=2), None).profile["voice"] for i in range(40)}
        self.assertGreater(len(voices), 2, "name hash should spread NPCs over several voices")
        for v in voices:
            self.assertIn(v, self.arch.items["hume_f"]["voices"]["openai"])

    def test_race_drives_traits(self):
        galka = self.res.resolve(Speaker(name="Rok", race=8), None)
        taru = self.res.resolve(Speaker(name="Kip", race=5), None)
        self.assertEqual(galka.archetype, "galka")
        self.assertIn(galka.profile["pitch"], ("very low", "low"))
        self.assertEqual(taru.archetype, "taru_m")
        self.assertIn(taru.profile["pitch"], ("high", "very high"))

    def test_layer_precedence_and_zone_keys(self):
        sp = Speaker(name="Prishe", race=4)
        self.lib.put("Prishe", {"tone": "gruff", "age": "young"}, source="llm:test")
        self.assertEqual(self.res.resolve(sp, 100).profile["tone"], "gruff")
        # zone-specific library entry beats the general one
        self.lib.put("Prishe@100", {"tone": "frightened"}, source="curated")
        self.assertEqual(self.res.resolve(sp, 100).profile["tone"], "frightened")
        self.assertEqual(self.res.resolve(sp, 101).profile["tone"], "gruff")
        # overrides beat everything, per field
        self.lib.set_override("Prishe", {"tone": "sweet", "voice": {"openai": "coral"}})
        r = self.res.resolve(sp, 100)
        self.assertEqual((r.profile["tone"], r.profile["voice"], r.profile["age"]), ("sweet", "coral", "young"))
        self.assertEqual(r.sources[0], "archetype:elvaan_f")
        self.assertEqual(r.sources[-1], "override")

    def test_library_source_precedence(self):
        """curated > pack > learned (llm)"""
        sp = Speaker(name="Cid", race=1)
        self.lib.put("Cid", {"tone": "llm-tone"}, source="llm:m")
        self.lib.packs["cid"] = {"key": "Cid", "profile": {"tone": "pack-tone"}, "meta": {"source": "curated"}}
        self.assertEqual(self.res.resolve(sp, None).profile["tone"], "pack-tone")
        self.lib.put("Cid", {"tone": "curated-tone"}, source="curated")
        self.assertEqual(self.res.resolve(sp, None).profile["tone"], "curated-tone")

    def test_role_keys(self):
        self.lib.set_override("@narrator", {"voice": {"openai": "sage"}, "tone": "hushed"})
        r = self.res.resolve(Speaker(narrator=True), None)
        self.assertEqual((r.archetype, r.profile["voice"], r.profile["tone"]), ("narrator", "sage", "hushed"))
        self.lib.set_override("@player", {"pitch": "low"})
        p = self.res.resolve(Speaker(name="Adventurer", is_player=True, race=3), None)
        self.assertEqual((p.archetype, p.profile["pitch"]), ("elvaan_m", "low"))

    def test_needs_inference(self):
        self.assertTrue(self.res.needs_inference(Speaker(name="Stranger")))
        self.lib.put("Stranger", {"tone": "x"}, source="llm:m")
        self.assertFalse(self.res.needs_inference(Speaker(name="Stranger")))
        self.assertFalse(self.res.needs_inference(Speaker(narrator=True)))
        self.assertFalse(self.res.needs_inference(Speaker(name="Me", is_player=True)))

    def test_describe_changes_with_profile(self):
        base = self.res.resolve(Speaker(name="Ayame", race=2), None).profile
        text = describe(base)
        self.assertIn("female", text)
        self.assertIn("exactly as written", text)
        self.assertNotEqual(text, describe({**base, "tone": "sinister"}))


class LibraryTests(KotodamaTestCase):
    def make_lib(self):
        cfg = self.make_config()
        return Library(Archetypes.load(), cfg.path("library.file"), cfg.path("library.packs_dir"),
                       cfg.path("library.overrides"), save_delay=0.05)

    def test_save_load_roundtrip_is_stable_and_scrubbed_by_caller(self):
        lib = self.make_lib()
        lib.put("Zeid", {"gender": "male", "tone": "bitter", "voice": {"openai": "onyx"}},
                source="llm:test-model", game_race="Hume M", zone=230, sample="Ask {player} about it.")
        lib.put("Aaron", {"age": "elderly"}, source="curated")
        lib.flush()
        text = lib.file.read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertEqual(data["format"], 1)
        self.assertEqual(list(data["npcs"]), ["Aaron", "Zeid"], "keys sorted for diff-friendly files")
        self.assertEqual(data["npcs"]["Zeid"]["meta"]["sample_lines"], ["Ask {player} about it."])
        lib.flush(force=True)
        self.assertEqual(lib.file.read_text(encoding="utf-8"), text, "rewrite is byte-identical")

        again = self.make_lib()
        self.assertEqual(again.lookup("zeid")[0]["tone"], "bitter")
        self.assertEqual(again.lookup("zeid")[1]["source"], "llm:test-model")

    def test_clean_close_writes_nothing(self):
        lib = self.make_lib()
        lib.flush()
        self.assertFalse(lib.file.exists())
        self.assertFalse(lib.overrides_file.exists())

    def test_atomic_write_leaves_no_tmp_files(self):
        lib = self.make_lib()
        lib.put("A", {"tone": "x"}, source="manual")
        lib.flush()
        self.assertEqual([p.name for p in lib.file.parent.iterdir()], ["npcs.json"])

    def test_debounced_save_happens(self):
        lib = self.make_lib()
        lib.put("A", {"tone": "x"}, source="manual")
        import time
        deadline = time.time() + 2
        while not lib.file.exists() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(lib.file.exists())

    def test_export_filter_and_import_pack(self):
        lib = self.make_lib()
        lib.put("Zeid", {"tone": "bitter"}, source="llm:m")
        lib.put("Aaron", {"age": "elderly"}, source="curated")
        out = lib.export(["llm"])
        self.assertEqual(list(out["npcs"]), ["Zeid"])

        shared = self.tmp / "friend.json"
        shared.write_text(json.dumps({"format": 1, "npcs": {
            "Naji": {"profile": {"tone": "gruff", "age": "bogus", "evil": "x"}, "meta": {"source": "curated"}},
            "Empty": {"profile": {"age": "bogus"}},
        }}), encoding="utf-8")
        dest, n = lib.import_pack(shared)
        self.assertEqual(n, 1, "entries with no valid fields are dropped")
        self.assertEqual(lib.lookup("naji")[0], {"tone": "gruff"})
        self.assertTrue(dest.is_file())
        self.assertIn("zeid", lib.local, "importing must not lose unsaved local entries")

        bad = self.tmp / "bad.json"
        bad.write_text('{"format": 99, "npcs": {}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            lib.import_pack(bad)

    def test_overrides_persist_and_reset(self):
        lib = self.make_lib()
        lib.set_override("Prishe", {"tone": "sweet"})
        lib.set_override("Prishe", {"pitch": "high"})
        self.assertEqual(lib.override("prishe"), {"tone": "sweet", "pitch": "high"})
        lib.flush()
        self.assertEqual(self.make_lib().override("prishe"), {"tone": "sweet", "pitch": "high"})
        self.assertTrue(lib.reset_override("Prishe"))
        self.assertIsNone(lib.override("prishe"))
        self.assertFalse(lib.reset_override("Prishe"))

    def test_corrupt_files_do_not_crash(self):
        lib = self.make_lib()
        lib.file.parent.mkdir(parents=True, exist_ok=True)
        lib.file.write_text("{ not json", encoding="utf-8")
        lib.load()
        self.assertEqual(lib.local, {})


class ShippedLibraryTests(KotodamaTestCase):
    """library/npcs.default.json ships with Kotodama and an upgrade replaces
    it; library/npcs.json is this install's own and an upgrade never touches it."""

    def setUp(self):
        super().setUp()
        cfg = self.make_config()
        self.shipped = cfg.path("library.defaults")
        self.shipped.parent.mkdir(parents=True, exist_ok=True)
        self.write_shipped({"Zeid": {"profile": {"tone": "shipped"}, "meta": {"source": "llm:m",
                                                                             "sample_lines": ["Hm."]}}})
        self.cfg = cfg

    def write_shipped(self, npcs):
        self.shipped.write_text(json.dumps({"format": 1, "npcs": npcs}), encoding="utf-8")

    def make_lib(self):
        cfg = self.cfg
        return Library(Archetypes.load(), cfg.path("library.file"), cfg.path("library.packs_dir"),
                       cfg.path("library.overrides"), save_delay=0.05, defaults_file=self.shipped)

    def test_shipped_entries_are_used_but_never_written(self):
        lib = self.make_lib()
        before = self.shipped.read_text(encoding="utf-8")
        self.assertEqual(lib.lookup("zeid")[0]["tone"], "shipped")
        self.assertTrue(lib.has_any("Zeid"), "a shipped NPC is not cast again")
        lib.put("Aaron", {"age": "elderly"}, source="llm:m")
        lib.note_seen("Zeid", 230, "A new line.")
        lib.flush(force=True)
        self.assertEqual(self.shipped.read_text(encoding="utf-8"), before)
        self.assertEqual(list(json.loads(lib.file.read_text(encoding="utf-8"))["npcs"]), ["Aaron"],
                         "only this install's entries go in its own file")

    def test_own_and_pack_entries_beat_shipped_ones(self):
        lib = self.make_lib()
        lib.packs["zeid"] = {"key": "Zeid", "profile": {"tone": "pack"}, "meta": {"source": "curated"}}
        self.assertEqual(lib.lookup("zeid")[0]["tone"], "pack")
        lib.put("Zeid", {"tone": "recast"}, source="llm:m")
        del lib.packs["zeid"]
        self.assertEqual(lib.lookup("zeid")[0]["tone"], "recast", "a recast replaces the shipped casting")

    def test_upgrade_adds_new_shipped_entries_and_keeps_ours(self):
        lib = self.make_lib()
        lib.put("Aaron", {"age": "elderly"}, source="llm:m")
        lib.flush()
        self.write_shipped({"Zeid": {"profile": {"tone": "shipped"}},
                            "Naji": {"profile": {"tone": "new in this release"}}})
        again = self.make_lib()
        self.assertEqual(again.lookup("naji")[0]["tone"], "new in this release")
        self.assertEqual(again.lookup("aaron")[0]["age"], "elderly")

    def test_export_with_defaults_rebuilds_the_shipped_file(self):
        lib = self.make_lib()
        lib.put("Aaron", {"age": "elderly"}, source="llm:m")
        self.assertEqual(list(lib.export()["npcs"]), ["Aaron"])
        merged = lib.export(with_defaults=True)["npcs"]
        self.assertEqual(sorted(merged), ["Aaron", "Zeid"])
        lib.put("Zeid", {"tone": "recast"}, source="llm:m")
        self.assertEqual(lib.export(with_defaults=True)["npcs"]["Zeid"]["profile"]["tone"], "recast")

    def test_known_entry_falls_back_to_shipped(self):
        lib = self.make_lib()
        self.assertEqual(lib.known_entry("zeid")["meta"]["sample_lines"], ["Hm."])
        self.assertIsNone(lib.known_entry("nobody"))

    def test_real_shipped_file_is_a_valid_library(self):
        from kotodama.config import ROOT
        path = ROOT / "library" / "npcs.default.json"
        lib = Library(Archetypes.load(), self.tmp / "npcs.json", self.tmp / "packs", self.tmp / "o.json",
                      defaults_file=path)
        self.assertGreater(len(lib.defaults), 0)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["format"], 1)


if __name__ == "__main__":
    unittest.main()
