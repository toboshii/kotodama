"""Scene / cutscene heuristics and addon behaviour, run against the real Lua.

Most scenarios here are lifted from a recorded play session (the NPC names,
menu ids and packet orderings are real), so a regression in the heuristics
shows up as one of those scenes being misclassified.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import ROOT, Addon, LuaRuntime  # noqa: E402

FOCHACHA, TOPOK, MAUDADT = 16982270, 16982300, 16982301
MARIN, KEN, SHAMARHAAN = 17739892, 17739893, 17739926
MAKKI, CHERUKIKI, KUKKI = 90001, 90002, 90003
IGSLI, OLWYN, MOOGLE = 17739958, 17739805, 17752545


def kinds(addon):
    return [s["kind"] for s in addon.speaks()]


@unittest.skipIf(LuaRuntime is None, "lupa is not installed (pip install lupa)")
class SceneTests(unittest.TestCase):
    def setUp(self):
        self.a = Addon()
        self.addCleanup(self.a.close)
        a = self.a
        for mob_id, name, race, model in [
            (FOCHACHA, "Fochacha", 6, 0), (TOPOK, "Topok-Hippok", 5, 5), (MAUDADT, "Maudadt", 8, 0),
            (MARIN, "Marin", 2, 2), (KEN, "Ken", 1, 6), (SHAMARHAAN, "Shamarhaan", 3, 14),
            (IGSLI, "Igsli", 1, 14), (OLWYN, "Olwyn", 0, 368), (MOOGLE, "Moogle", 0, 82),
            (MAKKI, "Makki-Chebukki", 0, 1445), (CHERUKIKI, "Cherukiki", 0, 1443), (KUKKI, "Kukki-Chebukki", 0, 1444),
        ]:
            a.add_mob(mob_id, name, race, [model])
        a.command("narrator", "on")      # so narration is reported too
        a.clear_sent()
        a.clock(1000)

    # -- zone-in scenes that never send an event packet ---------------------

    def test_zone_in_scene_without_an_event_packet_is_a_cutscene_from_the_start(self):
        """Makki-Chebukki: 31 lines, no 0x032/33/34 and no 0x036 anywhere."""
        a = self.a
        a.zone_out()
        a.advance(15)
        a.line(b"Windu\x81\x60rst!")                              # narration (Shift-JIS wave dash), no speaker
        for name, text in [("Makki-Chebukki", "Oho"), ("Makki-Chebukki", "So this is our homeland?"),
                           ("Cherukiki", "Nope, this is our daddy's homeland!"),
                           ("Kukki-Chebukki", "Nah, you're wrong, it's our mom's homeland!")]:
            a.advance(4)
            a.line(f"{name} : {text}\x7f1")
        self.assertEqual(kinds(a), ["cutscene"] * 5)
        self.assertTrue(a.speaks()[0]["speaker"]["narrator"], "the opening line has no speaker")

    def test_untracked_scene_classification_is_logged_with_a_reason(self):
        a = Addon(debug=True)
        self.addCleanup(a.close)
        a.zone_out()
        a.advance(15)
        a.line(b"Makki-Chebukki : Oho\x7f1")
        self.assertIn("scene: untracked-event -> cutscene", a.debug_log())
        self.assertIn("packets during untracked scene", a.debug_log())

    # -- one-liners announced by 0x036 are not cutscenes ----------------------

    def test_shop_greeting_after_npc_chat_is_ambient_not_a_cutscene(self):
        """Olwyn / Moogle: a timed mode-144 line right after a 0x036 packet."""
        a = self.a
        a.npc_chat(OLWYN)
        a.line(b"Olwyn : Welcome to Mjoll's Goods! What can I do for you?", mode=144)
        a.advance(1)
        a.release(0)     # the stray type-0 release the client sends
        self.assertEqual(kinds(a), ["ambient"])

    def test_bark_exemption_expires(self):
        a = self.a
        a.npc_chat(OLWYN)
        a.advance(10)
        a.line(b"Makki-Chebukki : Oho\x7f1")
        self.assertEqual(kinds(a), ["cutscene"], "a line long after the bark is not the bark's")

    # -- lines that straggle in after the release ------------------------------

    def test_line_arriving_just_after_release_stays_with_its_event(self):
        """Igsli: begin, automated 0x05B, release, THEN the greeting."""
        a = self.a
        a.talk_to(IGSLI)
        a.event_begin(IGSLI, 598, packet=0x034)
        a.event_option(10, automated=True)
        a.release()
        a.advance(0.2)
        a.line(b"Igsli : A fair day to you, ma'am! How may I help you?")
        self.assertEqual(kinds(a), ["event"])

    def test_late_line_window_closes(self):
        a = self.a
        a.talk_to(IGSLI)
        a.event_begin(IGSLI, 598, packet=0x034)
        a.release()
        a.advance(10)
        a.line(b"Igsli : Another line much later")
        self.assertEqual(kinds(a), ["cutscene"], "no longer attributable to the released event")

    # -- events -------------------------------------------------------------

    def test_ordinary_conversation_stays_an_event(self):
        a = self.a
        a.talk_to(100)
        a.event_begin(100, 90)
        for i in range(5):
            a.advance(8)
            a.line(f"Ayame : Line number {i} of ordinary dialog.\x7f1")
        a.release()
        self.assertEqual(kinds(a), ["event"] * 5)

    def test_second_speaker_promotes_and_is_remembered(self):
        """Marin / Ken."""
        a = self.a
        a.talk_to(MARIN)
        a.event_begin(MARIN, 361, packet=0x034)
        a.line(b"Marin : Are you sure you'll be all right?\x7f1")
        a.advance(8)
        a.line(b"Ken : Would you just get off my case?\x7f1")
        self.assertEqual(kinds(a), ["event", "cutscene"])
        a.release()
        saved = a.registry_file()
        self.assertEqual(saved["events"]["230:361"]["reason"], "multi-speaker")
        self.assertEqual(saved["events"]["230:361"]["source"], "learned")

    def test_remembered_cutscene_is_a_cutscene_from_the_first_line(self):
        a = self.a
        a.talk_to(MARIN)
        a.event_begin(MARIN, 361, packet=0x034)
        a.line(b"Marin : Are you sure?\x7f1")
        a.line(b"Ken : Get off my case.\x7f1")
        a.release()
        a.advance(60)
        a.clear_sent()

        a.talk_to(MARIN)
        a.event_begin(MARIN, 361, packet=0x034)
        a.line(b"Marin : Are you sure?\x7f1")
        self.assertEqual(kinds(a), ["cutscene"], "second viewing: correct from line one")

    def test_registry_survives_a_restart(self):
        a = self.a
        a.talk_to(MARIN)
        a.event_begin(MARIN, 361, packet=0x034)
        a.line(b"Marin : Hi.\x7f1")
        a.line(b"Ken : Hi.\x7f1")
        a.release()
        b = Addon()                                   # fresh Lua state, shared nothing
        self.addCleanup(b.close)
        b.add_mob(MARIN, "Marin", 2, [2])
        (b.dir / "library" / "cutscenes.json").write_text(
            json.dumps(a.registry_file()), encoding="utf-8")
        b.fire("load")                                 # re-initialise: reads the file
        b.command("narrator", "on")
        b.clear_sent()
        b.clock(1000)
        b.talk_to(MARIN)
        b.event_begin(MARIN, 361, packet=0x034)
        b.line(b"Marin : Hi.\x7f1")
        self.assertEqual(kinds(b), ["cutscene"])

    def test_shipped_seed_makes_fochacha_a_cutscene_from_line_one(self):
        """The first four Fochacha lines look exactly like ordinary dialog."""
        a = Addon(seed_cutscenes=True)
        self.addCleanup(a.close)
        a.set_zone(50)
        a.add_mob(FOCHACHA, "Fochacha", 6, [0])
        a.command("narrator", "on")
        a.clear_sent()
        a.clock(1000)
        a.talk_to(FOCHACHA)
        a.event_begin(FOCHACHA, 39)
        for text in ["You're an adventurer, right?", "The name's Fochacha.", "My son is at the port now.",
                     "Oh, speaking of ingredients..."]:
            a.advance(8)
            a.line(f"Fochacha : {text}\x7f1")
        self.assertEqual(kinds(a), ["cutscene"] * 4)

    def test_without_the_seed_fochacha_is_only_caught_when_the_son_appears(self):
        a = self.a
        a.set_zone(50)
        a.talk_to(FOCHACHA)
        a.event_begin(FOCHACHA, 39)
        a.line(b"Fochacha : You're an adventurer, right?\x7f1")
        a.advance(8)
        a.line(b"Fochacha : My son is at the port.\x7f1")
        a.advance(8)
        a.line(b"Topok-Hippok : Mom! Someone ran off with the ingredients!\x7f1")
        self.assertEqual(kinds(a), ["event", "event", "cutscene"])
        # ...and that is remembered for next time:
        self.assertIn("50:39", a.registry_file()["events"])

    def test_the_repeat_conversation_after_the_scene_is_not_a_cutscene(self):
        """Fochacha menu 46 is a one-line reminder, a different event id."""
        a = Addon(seed_cutscenes=True)
        self.addCleanup(a.close)
        a.set_zone(50)
        a.add_mob(FOCHACHA, "Fochacha", 6, [0])
        a.command("narrator", "on")
        a.clear_sent()
        a.clock(1000)
        a.talk_to(FOCHACHA)
        a.event_begin(FOCHACHA, 46)
        a.line(b"Fochacha : If you want to know where my son is, he's at the teahouse.\x7f1")
        self.assertEqual(kinds(a), ["event"])

    def test_repeated_begin_packet_does_not_reset_the_event(self):
        """38 of 109 begins in the log were an immediate repeat of the previous one.
        A timed line promotes the event without touching the registry, so only the
        event object itself can remember it across the repeat."""
        a = self.a
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"Ayame : An auto-advancing line.\x7f4\x01")
        a.advance(1)
        a.event_begin(100, 90)                                   # repeat, after a line
        a.advance(1)
        a.line(b"Ayame : A normal line after the repeat.\x7f1")
        self.assertEqual(kinds(a), ["cutscene", "cutscene"], "the promotion survived the repeated packet")

    def test_a_new_event_for_the_same_npc_after_the_window_starts_fresh(self):
        a = self.a
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"Ayame : An auto-advancing line.\x7f4\x01")
        a.advance(10)                                            # well past the duplicate window
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"Ayame : Fresh conversation.\x7f1")
        self.assertEqual(kinds(a), ["cutscene", "event"])

    def test_event_not_started_by_the_player_is_a_cutscene_but_not_remembered(self):
        a = self.a
        a.event_begin(FOCHACHA, 777)                            # no 0x01A talk before it
        a.line(b"Fochacha : Something happens.\x7f1")
        self.assertEqual(kinds(a), ["cutscene"])
        a.release()
        self.assertIsNone(a.registry_file(), "weak signals never write to the registry")

    def test_timed_lines_promote_but_are_not_remembered(self):
        a = self.a
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"Ayame : Auto advancing line.\x7f4\x01")
        self.assertEqual(kinds(a), ["cutscene"])
        a.release()
        self.assertIsNone(a.registry_file())

    def test_battle_chatter_is_never_reclassified(self):
        a = self.a
        a.command("battle", "on")
        a.clear_sent()
        a.line(b"Prishe : Get behind me!", mode=142)
        self.assertEqual(kinds(a), ["battle"])

    # -- manual correction -----------------------------------------------------

    def test_manual_cutscene_command_marks_and_remembers_the_event(self):
        a = self.a
        a.talk_to(100)
        a.event_begin(100, 90)
        a.command("cutscene")
        a.line(b"Ayame : Now a cutscene.\x7f1")
        self.assertEqual(kinds(a), ["cutscene"])
        self.assertEqual(a.registry_file()["events"]["230:90"]["source"], "manual")

    def test_manual_mark_works_just_after_the_event_ended(self):
        a = self.a
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"Ayame : Hello.\x7f1")
        a.release()
        a.command("cutscene")
        self.assertIn("230:90", a.registry_file()["events"])
        a.command("cutscene", "forget")
        self.assertNotIn("230:90", a.registry_file()["events"])

    def test_cutscene_command_without_an_event_explains_itself(self):
        a = self.a
        a.command("cutscene")
        self.assertIn("no event to mark", a.chat()[-1])


@unittest.skipIf(LuaRuntime is None, "lupa is not installed (pip install lupa)")
class RegistryFileTests(unittest.TestCase):
    def test_serialization_is_sorted_and_stable(self):
        a = Addon()
        self.addCleanup(a.close)
        lua = a.lua
        lua.execute(b"""
            local c = require('lib/cutscenes')
            c.load()
            c.learn(235, 434, 'Shamarhaan', 'speaker-differs', 'learned')
            c.learn(50, 39, 'Fochacha', 'multi-speaker', 'learned')
            c.learn(235, 361, 'Marin', 'multi-speaker', 'learned')
        """)
        first = (a.dir / "library" / "cutscenes.json").read_text(encoding="utf-8")
        self.assertEqual(list(json.loads(first)["events"]), ["50:39", "235:361", "235:434"])
        # load -> add -> remove forces two more saves; the result must match byte for byte.
        lua.execute(b"local c = require('lib/cutscenes'); c.load(); c.learn(1, 1, 'x', 'y'); c.forget(1, 1)")
        self.assertEqual((a.dir / "library" / "cutscenes.json").read_text(encoding="utf-8"), first,
                         "a load/save round trip is byte-identical")

    def test_shipped_seed_is_valid(self):
        seed = json.loads((ROOT / "library" / "cutscenes.json").read_text(encoding="utf-8"))
        self.assertEqual(seed["format"], 1)
        for key, e in seed["events"].items():
            self.assertEqual(key, f"{e['zone']}:{e['menu']}")
        self.assertEqual(list(seed["events"]), sorted(seed["events"], key=lambda k: tuple(map(int, k.split(":")))))

    def test_corrupt_file_is_ignored(self):
        a = Addon()
        self.addCleanup(a.close)
        (a.dir / "library" / "cutscenes.json").write_text("{ nope", encoding="utf-8")
        a.fire("load")                      # must not raise
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"Ayame : Hello.\x7f1")
        self.assertEqual(kinds(a), ["event"])


@unittest.skipIf(LuaRuntime is None, "lupa is not installed (pip install lupa)")
class DialogTests(unittest.TestCase):
    def setUp(self):
        self.a = Addon()
        self.addCleanup(self.a.close)
        self.lua = self.a.lua

    def split(self, text: str):
        res = self.lua.eval(b"function(t) local n, r = require('lib/dialog').split_speaker(t); return n, r end")(text.encode())
        return tuple(None if v is None else v.decode() for v in res)

    def test_names_with_initials_after_a_comma(self):
        self.assertEqual(self.split("Rabid Wolf, I.M. : I am Rabid Wolf, I.M., an Iron Musketeer."),
                         ("Rabid Wolf, I.M.", "I am Rabid Wolf, I.M., an Iron Musketeer."))
        self.assertEqual(self.split("Leonoyne, T.K. : Halt!")[0], "Leonoyne, T.K.")

    def test_other_commas_still_mean_narration(self):
        self.assertEqual(self.split("Hello, friend : said the guard")[0], None)
        self.assertEqual(self.split("Well, I never : said he")[0], None)

    def test_ordinary_names_and_punctuation(self):
        self.assertEqual(self.split("Prishe : Hello."), ("Prishe", "Hello."))
        self.assertEqual(self.split("Home Point #1 : Welcome.")[0], "Home Point #1")
        self.assertEqual(self.split("Who goes there? : friend")[0], None)
        self.assertEqual(self.split("The wind howls.")[0], None)


@unittest.skipIf(LuaRuntime is None, "lupa is not installed (pip install lupa)")
class AddonSmokeTests(unittest.TestCase):
    """Filters, deny list, toggles, commands and the link: end to end."""

    def setUp(self):
        self.a = Addon()
        self.addCleanup(self.a.close)
        self.a.clock(1)

    def test_hello_and_config_on_link_up(self):
        a = self.a
        self.assertEqual(a.sent("hello")[0]["player"], "Adventurer")
        a.queue_from_sidecar({"t": "pong"})
        a.advance(1)
        a.prerender()
        self.assertTrue(a.sent("config"))

    def test_voiced_line_carries_speaker_zone_and_player(self):
        a = self.a
        a.clear_sent()
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"\x1e\x01Ayame : Welcome to Norg, Adventurer!\x7f1")
        sp = a.speaks()[0]
        self.assertEqual((sp["text"], sp["kind"], sp["zone"], sp["player"]),
                         ("Welcome to Norg, Adventurer!", "event", 230, "Adventurer"))
        self.assertEqual((sp["speaker"]["name"], sp["speaker"]["race"], sp["speaker"]["race_name"]),
                         ("Ayame", 2, "Hume F"))

    def test_mode_filters(self):
        a = self.a
        a.clear_sent()
        a.command("mode", "cutscenes")
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"Ayame : Ordinary dialog.\x7f1")
        self.assertEqual(a.speaks(), [])
        a.command("mode", "all")
        a.advance(5)
        a.line(b"Ayame : Ordinary dialog again.\x7f1")
        self.assertEqual(len(a.speaks()), 1)

    def test_deny_list_with_wildcards_and_stop(self):
        a = self.a
        a.clear_sent()
        a.command("deny", "Ayame")
        self.assertTrue(a.sent("stop"))
        a.line(b"Ayame : Denied.")
        self.assertEqual(a.speaks(), [])
        a.command("allow", "Ayame")
        a.command("deny", "Moog*")
        a.add_mob(9, "Moogle Guide", 0, [82])
        a.advance(5)
        a.line(b"Moogle Guide : Kupo!")
        self.assertEqual(a.speaks(), [])

    def test_test_command_defaults_to_ambient(self):
        a = self.a
        a.clear_sent()
        a.command("mode", "cutscenes")
        a.command("test", "Prishe", ":", "hello", "there.")
        self.assertEqual(a.speaks(), [], "a plain test line is ambient and cutscenes mode filters it")
        a.command("test", "cutscene", "Prishe", ":", "hello", "there.")
        self.assertEqual(kinds(a), ["cutscene"])

    def test_volume_duck_and_voice_commands(self):
        a = self.a
        a.clear_sent()
        a.command("volume", "250")
        self.assertEqual(a.sent("config")[-1]["volume"], 250)
        a.command("volume", "9999")
        self.assertEqual(a.sent("config")[-1]["volume"], 400)
        a.command("duck", "25")
        self.assertEqual((a.sent("config")[-1]["duck"], a.sent("config")[-1]["duck_level"]), (True, 25))
        a.command("duck", "off")
        self.assertFalse(a.sent("config")[-1]["duck"])
        a.clear_sent()
        a.command("voice", "Ayame", "age=elderly", "tone=dry_wit")
        cmd = a.sent("cmd")[0]
        self.assertEqual((cmd["name"], cmd["params"]["tone"]), ("voice_set", "dry wit"))

    def test_link_loss_warns_once(self):
        a = self.a
        a.queue_from_sidecar({"t": "pong"})
        a.advance(1)
        a.prerender()
        a.advance(100)
        a.prerender()
        a.prerender()
        self.assertEqual(sum("lost the voice server" in m for m in a.chat()), 1)

    def test_target_hints_are_deduplicated_and_town_npcs_only(self):
        a = self.a
        a.add_mob(400, "Naji", 3, [14], spawn_type=2)
        a.add_mob(300, "Goblin Thug", 0, [1], spawn_type=16)
        a.clear_sent()
        a.set_target(400)
        a.fire("target change", 400)
        a.fire("target change", 400)
        a.set_target(300)
        a.fire("target change", 300)
        hints = a.sent("hint")
        self.assertEqual([h["npc"] for h in hints], ["Naji"])

    def test_the_debug_log_records_event_start_details(self):
        a = Addon(debug=True)
        self.addCleanup(a.close)
        a.talk_to(100)
        a.event_begin(100, 90)
        a.npc_chat(100)
        a.release()
        log = a.debug_log()
        self.assertIn("event begin packet=0x032", log)
        self.assertIn("begin packet bytes:", log)
        self.assertIn("player_status=0", log)
        self.assertIn("packets during event", log)
        self.assertIn("npc chat 0x036", log)


if __name__ == "__main__":
    unittest.main()
