"""Custom voice lines (//koto say, //koto line): the "custom" kind and role."""

from __future__ import annotations

from kotodama import protocol
from kotodama.archetypes import Archetypes, is_narration
from kotodama.casting import role_for
from kotodama.library import Library
from kotodama.profiles import ProfileResolver
from kotodama.protocol import Speaker

from helpers import FakeTTS, KotodamaTestCase


def custom_msg(text="For the Crystal!", seq=1, **speaker):
    return {"t": "speak", "seq": seq, "kind": "custom", "mode": 0, "text": text, "zone": 230,
            "player": "Adventurer", "speaker": {"role": "custom", "narrator": True, **speaker}}


def narrator_msg(text="For the Crystal!", seq=1):
    return {"t": "speak", "seq": seq, "kind": "cutscene", "mode": 150, "text": text, "zone": 230,
            "player": "Adventurer", "speaker": {"narrator": True}}


class ProtocolTests(KotodamaTestCase):
    def test_custom_kind_and_role_are_kept(self):
        sp = protocol.parse_speak(custom_msg())
        self.assertEqual((sp.kind, sp.speaker.role, sp.speaker.narrator), ("custom", "custom", True))

    def test_unknown_roles_are_dropped(self):
        for role in ("narrator", "admin", 3, None, ""):
            sp = protocol.parse_speak(custom_msg(role=role))
            self.assertIsNone(sp.speaker.role, role)

    def test_unknown_kind_still_falls_back_to_ambient(self):
        self.assertEqual(protocol.parse_speak({**custom_msg(), "kind": "shout"}).kind, "ambient")


class ResolverTests(KotodamaTestCase):
    def setUp(self):
        super().setUp()
        self.arch = Archetypes.load()
        cfg = self.make_config()
        self.lib = Library(self.arch, cfg.path("library.file"), cfg.path("library.packs_dir"),
                           cfg.path("library.overrides"), save_delay=0.05)
        self.res = ProfileResolver(self.arch, self.lib)
        self.custom = Speaker(role="custom", narrator=True)

    def test_untuned_custom_is_exactly_the_narrator(self):
        c = self.res.resolve(self.custom, 230)
        n = self.res.resolve(Speaker(narrator=True), 230)
        self.assertEqual((c.profile, c.key, c.archetype), (n.profile, n.key, n.archetype))
        self.assertEqual(c.key, "@narrator")

    def test_role_alone_is_enough(self):
        """An addon that sends only role=custom still gets the narrator archetype."""
        c = self.res.resolve(Speaker(role="custom"), None)
        self.assertEqual((c.archetype, c.key), ("narrator", "@narrator"))

    def test_narrator_overrides_carry_to_custom_lines(self):
        self.lib.set_override("@narrator", {"tone": "hushed", "voice": {"openai": "sage"}})
        c = self.res.resolve(self.custom, None)
        self.assertEqual((c.profile["tone"], c.profile["voice"], c.key), ("hushed", "sage", "@narrator"))

    def test_custom_override_wins_and_affects_only_custom_lines(self):
        self.lib.set_override("@narrator", {"tone": "hushed", "pace": "slow"})
        self.lib.set_override("@custom", {"tone": "rousing", "voice": {"openai": "onyx"}})
        c = self.res.resolve(self.custom, None)
        n = self.res.resolve(Speaker(narrator=True), None)
        self.assertEqual((c.profile["tone"], c.profile["pace"], c.profile["voice"]), ("rousing", "slow", "onyx"))
        self.assertEqual(c.key, "@custom", "a tuned @custom is cast as itself")
        self.assertEqual(n.profile["tone"], "hushed")
        self.assertNotEqual(n.profile["voice"], "onyx")
        self.assertEqual(c.sources[-1], "override")

    def test_reset_returns_to_the_narrator(self):
        self.lib.set_override("@custom", {"tone": "rousing"})
        self.lib.reset_override("@custom")
        self.assertEqual(self.res.resolve(self.custom, None).key, "@narrator")

    def test_roles_are_never_cast_by_the_model(self):
        self.assertFalse(self.res.needs_inference(self.custom))
        self.assertFalse(self.res.needs_inference(Speaker(name="Someone", role="custom")))

    def test_custom_counts_as_narration_for_voice_choice(self):
        self.assertTrue(is_narration("@custom") and is_narration("@Narrator"))
        self.assertFalse(is_narration("Ayame") or is_narration(None))
        self.assertEqual(role_for({}, "@custom"), "narrator")


class EngineTests(KotodamaTestCase):
    def test_custom_line_uses_the_narrator_voice_and_shares_its_cache(self):
        tts = FakeTTS()
        eng = self.make_engine(tts)
        eng.handle(narrator_msg("The wind howls."))
        eng.wait_idle()
        eng.handle(custom_msg("The wind howls.", seq=2))
        eng.wait_idle()
        self.assertEqual(len(tts.calls), 1, "untuned custom line is the narrator's audio, from cache")

    def test_custom_line_is_cached_and_not_remembered_as_an_npc(self):
        tts = FakeTTS()
        eng = self.make_engine(tts)
        j1 = eng.speak(protocol.parse_speak(custom_msg("Ready!")))
        eng.wait_idle()
        j2 = eng.speak(protocol.parse_speak(custom_msg("Ready!", seq=2)))
        eng.wait_idle()
        self.assertEqual((j1.source, j2.source, len(tts.calls)), ("tts", "cache", 1))
        self.assertEqual(eng._seen, {})
        self.assertFalse(eng.lib.local, "nothing about a custom line goes into the library")

    def test_voice_set_on_custom_changes_custom_lines_only(self):
        tts = FakeTTS()
        eng = self.make_engine(tts)
        eng.handle({"t": "cmd", "name": "voice_set", "npc": "@custom",
                    "params": {"tone": "rousing", "voice": "onyx"}})
        shown = "\n".join(self.say_lines())
        self.assertIn("@custom:", shown)
        self.assertIn("layers: archetype:narrator > override", shown)
        eng.handle(custom_msg("Charge!"))
        eng.wait_idle()
        eng.handle(narrator_msg("Charge!", seq=2))
        eng.wait_idle()
        self.assertEqual(len(tts.calls), 2)
        self.assertEqual(tts.calls[0]["voice"], "onyx")
        self.assertIn("rousing", tts.calls[0]["instructions"])
        self.assertNotIn("rousing", tts.calls[1]["instructions"])
        self.assertEqual(eng.lib.override("@custom")["tone"], "rousing")

        eng.handle({"t": "cmd", "name": "voice_reset", "npc": "@custom"})
        self.assertIsNone(eng.lib.override("@custom"))

    def test_voice_show_uses_the_role_speakers(self):
        eng = self.make_engine()
        for key, archetype in (("@custom", "narrator"), ("@narrator", "narrator")):
            self.replies.clear()
            eng.handle({"t": "cmd", "name": "voice_show", "npc": key})
            self.assertIn(f"archetype:{archetype}", "\n".join(self.say_lines()))

    def test_reinfer_and_promote_refuse_roles(self):
        eng = self.make_engine()
        for action in ("voice_reinfer", "voice_promote"):
            self.replies.clear()
            eng.handle({"t": "cmd", "name": action, "npc": "@custom"})
            self.assertIn("is a role", "\n".join(self.say_lines()))
        self.assertFalse(eng.lib.local)
