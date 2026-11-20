"""Custom voice lines: //koto say, //koto line, and placeholder filling."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Addon, LuaRuntime  # noqa: E402

AYAME, PRISHE = 100, 200


@unittest.skipIf(LuaRuntime is None, "lupa is not installed (pip install lupa)")
class SayTests(unittest.TestCase):
    def setUp(self):
        self.a = Addon()
        self.addCleanup(self.a.close)

    def last_chat(self) -> str:
        return self.a.chat()[-1]

    def test_say_sends_one_custom_line_in_the_custom_role(self):
        self.a.command("say", "For", "the", "Crystal!")
        (msg,) = self.a.speaks()
        self.assertEqual((msg["kind"], msg["text"], msg["mode"]), ("custom", "For the Crystal!", 0))
        self.assertEqual(msg["speaker"], {"role": "custom", "narrator": True})
        self.assertEqual(msg["player"], "Adventurer")

    def test_dialog_filters_do_not_apply(self):
        # narrator is off by default; cutscenes-only mode would drop any NPC line.
        self.a.command("mode", "cutscenes")
        self.a.command("narrator", "off")
        self.a.command("say", "Still", "spoken.")
        self.assertEqual([s["text"] for s in self.a.speaks()], ["Still spoken."])

    def test_voiceover_off_silences_it(self):
        self.a.command("off")
        self.a.command("say", "Hello.")
        self.assertEqual(self.a.speaks(), [])
        self.assertIn("voiceover is off", self.last_chat())

    def test_blank_or_unspeakable_text_sends_nothing(self):
        self.a.command("say")
        self.assertIn("usage", self.last_chat())
        self.a.command("say", "...")
        self.assertIn("nothing to say", self.last_chat())
        self.assertEqual(self.a.speaks(), [])

    def test_last_speaker_is_untouched(self):
        self.a.line(b"Ayame : Hello.")
        self.a.command("say", "Hi!")
        self.a.command("voice", "last")
        cmds = self.a.sent("cmd")
        self.assertEqual(cmds[-1]["npc"], "Ayame", "voice last still means the last NPC")


@unittest.skipIf(LuaRuntime is None, "lupa is not installed (pip install lupa)")
class PlaceholderTests(unittest.TestCase):
    def setUp(self):
        self.a = Addon()
        self.addCleanup(self.a.close)

    def said(self) -> list[str]:
        return [s["text"] for s in self.a.speaks()]

    def test_each_placeholder_is_filled_in(self):
        self.a.set_target(AYAME)
        self.a.set_target_of("bt", PRISHE)
        self.a.set_target_of("pet", AYAME)
        self.a.set_target_of("ft", PRISHE)
        self.a.set_target_of("lastst", AYAME)
        self.a.command("say", "<me>", "<t>", "<bt>", "<pet>", "<ft>", "<lastst>")
        self.assertEqual(self.said(), ["Adventurer Ayame Prishe Ayame Prishe Ayame"])

    def test_placeholders_ignore_case(self):
        self.a.set_target(AYAME)
        self.a.command("say", "Hello,", "<T>!", "I'm", "<Me>.")
        self.assertEqual(self.said(), ["Hello, Ayame! I'm Adventurer."])

    def test_missing_target_sends_nothing_and_says_why(self):
        self.a.command("say", "Go,", "<t>!")
        self.assertEqual(self.said(), [])
        self.assertIn("nothing targeted for <t>", self.a.chat()[-1])
        self.a.command("say", "Heel,", "<pet>.")
        self.assertIn("no pet for <pet>", self.a.chat()[-1])
        self.assertEqual(self.said(), [])

    def test_unknown_placeholder_is_refused(self):
        self.a.command("say", "Hi", "<foo>")
        self.assertEqual(self.said(), [])
        self.assertIn("unknown placeholder <foo>", self.a.chat()[-1])
        self.assertIn("<lastst>", self.a.chat()[-1], "the message lists what is supported")

    def test_st_is_explained(self):
        self.a.command("say", "<st>")
        self.assertEqual(self.said(), [])
        self.assertIn("<st>", self.a.chat()[-1])

    def test_angle_brackets_that_are_not_placeholders_are_left_alone(self):
        self.a.command("say", "I", "<3", "you,", "a>b.")
        self.assertEqual(self.said(), ["I <3 you, a>b."])


@unittest.skipIf(LuaRuntime is None, "lupa is not installed (pip install lupa)")
class SavedLineTests(unittest.TestCase):
    def setUp(self):
        self.a = Addon()
        self.addCleanup(self.a.close)
        self.file = self.a.dir / "data" / "voicelines.json"

    def said(self) -> list[str]:
        return [s["text"] for s in self.a.speaks()]

    def test_add_play_list_delete(self):
        self.a.command("line", "add", "Rally", "To", "me,", "everyone!")
        self.assertIn("/con koto line rally", self.a.chat()[-1])
        self.a.command("line", "rally")
        self.assertEqual(self.said(), ["To me, everyone!"])
        self.assertEqual(self.a.speaks()[0]["speaker"]["role"], "custom")

        self.a.command("line", "list")
        self.assertIn("Kotodama:   rally: To me, everyone!", self.a.chat())

        self.a.command("line", "del", "rally")
        self.a.command("line", "rally")
        self.assertIn("no saved line called rally", self.a.chat()[-1])
        self.assertEqual(len(self.said()), 1)

    def test_placeholders_are_kept_when_saved_and_filled_when_spoken(self):
        self.a.command("line", "add", "go", "Go,", "<t>!")
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8"))["lines"]["go"], "Go, <t>!")
        self.a.set_target(AYAME)
        self.a.command("line", "go")
        self.a.set_target(PRISHE)
        self.a.command("line", "go")
        self.assertEqual(self.said(), ["Go, Ayame!", "Go, Prishe!"])
        self.a.set_target(None)
        self.a.command("line", "go")
        self.assertIn("nothing targeted for <t>", self.a.chat()[-1])
        self.assertEqual(len(self.said()), 2)

    def test_saved_lines_survive_a_reload(self):
        self.a.command("line", "add", "thanks", "Thank", "you!")
        self.a.fire("load")
        self.a.command("line", "thanks")
        self.assertEqual(self.said(), ["Thank you!"])

    def test_file_is_sorted_and_round_trips_byte_for_byte(self):
        for name in ("zeta", "alpha", "mid"):
            self.a.command("line", "add", name, "Line", name)
        first = self.file.read_bytes()
        self.assertNotIn(b"\r\n", first)
        self.assertEqual(list(json.loads(first)["lines"]), ["alpha", "mid", "zeta"])
        self.a.fire("load")
        self.a.command("line", "add", "extra", "x")
        self.a.command("line", "del", "extra")
        self.assertEqual(self.file.read_bytes(), first)

    def test_bad_names_and_empty_text_are_refused(self):
        for name in ("add", "list", "del", "has space?", "x" * 40):
            self.a.command("line", "add", name, "Hello.")
            self.assertIn("line add:", self.a.chat()[-1], name)
        self.a.command("line", "add", "empty")
        self.assertIn("no text given", self.a.chat()[-1])
        self.assertFalse(self.file.exists())

    def test_corrupt_file_is_ignored(self):
        self.file.write_text("{ nope", encoding="utf-8")
        self.a.fire("load")                      # must not raise
        self.a.command("line", "list")
        self.assertIn("no saved lines", self.a.chat()[-1])

    def test_del_without_a_name_shows_usage(self):
        self.a.command("line", "del")
        self.assertIn("usage: //koto line del <name>", self.a.chat()[-1])

    def test_voice_custom_reaches_the_sidecar_as_the_role_key(self):
        self.a.command("voice", "@custom", "tone=rousing_and_bold")
        self.a.command("voice", "@custom", "reset")
        cmds = self.a.sent("cmd")
        self.assertEqual([(c["name"], c["npc"]) for c in cmds], [("voice_set", "@custom"), ("voice_reset", "@custom")])
        self.assertEqual(cmds[0]["params"], {"tone": "rousing and bold"})

    def test_line_honours_voiceover_off(self):
        self.a.command("line", "add", "hi", "Hi!")
        self.a.command("off")
        self.a.command("line", "hi")
        self.assertEqual(self.said(), [])
        self.assertIn("voiceover is off", self.a.chat()[-1])


if __name__ == "__main__":
    unittest.main()
