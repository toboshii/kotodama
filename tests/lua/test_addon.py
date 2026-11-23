"""Scene tracking and addon behaviour, run against the real Lua.

Most scenarios here are lifted from a recorded play session (the NPC names,
menu ids and packet orderings are real), so a regression in the scene rules
shows up as one of those scenes being misclassified.
"""

from __future__ import annotations

import json
import sys
import tempfile
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

    def event_lines(self):
        return self.a.lua.eval(b"require('lib/scene').current().lines")

    # -- zone-in scenes that never send an event packet ---------------------

    def test_zone_in_scene_without_an_event_packet_is_event_dialog(self):
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
        self.assertEqual(kinds(a), ["event"] * 5)
        self.assertTrue(a.speaks()[0]["speaker"]["narrator"], "the opening line has no speaker")

    def test_untracked_scene_classification_is_logged_with_a_reason(self):
        a = Addon(debug=True)
        self.addCleanup(a.close)
        a.zone_out()
        a.advance(15)
        a.line(b"Makki-Chebukki : Oho\x7f1")
        self.assertIn("scene: untracked-event -> event", a.debug_log())
        self.assertIn("packets during untracked scene", a.debug_log())

    def test_lines_log_the_players_status(self):
        """Collected to see whether status 4 (in an event) tells real scenes
        without an event packet from the rule misfiring."""
        a = Addon(debug=True)
        self.addCleanup(a.close)
        a.zone_out()
        a.advance(15)
        a.set_status(4)
        a.line(b"Makki-Chebukki : Oho\x7f1")
        line = next(entry for entry in a.debug_log().splitlines() if " line mode=" in entry)
        self.assertIn("kind=event", line)
        self.assertIn(" status=4 ", line)

    # -- one-liners announced by 0x036 are ambient -----------------------------

    def test_shop_greeting_after_npc_chat_is_ambient(self):
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
        self.assertEqual(kinds(a), ["event"], "a line long after the bark is not the bark's")

    def test_other_chat_modes_outside_an_event_are_ambient(self):
        a = self.a
        a.line(b"Ayame : A system-mode line.", mode=151)
        self.assertEqual(kinds(a), ["ambient"])

    # -- lines that straggle in after the release ------------------------------

    def test_line_arriving_just_after_release_stays_with_its_event(self):
        """Igsli: begin, automated 0x05B, release, THEN the greeting."""
        a = self.a
        a.talk_to(IGSLI)
        a.event_begin(IGSLI, 598, packet=0x034)
        a.event_option(10, automated=True)
        a.release()
        a.advance(0.2)
        a.line(b"Igsli : A fair day to you, ma'am! How may I help you?", mode=151)
        self.assertEqual(kinds(a), ["event"])

    def test_late_line_window_closes(self):
        a = self.a
        a.talk_to(IGSLI)
        a.event_begin(IGSLI, 598, packet=0x034)
        a.release()
        a.advance(10)
        a.line(b"Igsli : Another line much later", mode=151)
        self.assertEqual(kinds(a), ["ambient"], "no longer attributable to the released event")

    # -- events -------------------------------------------------------------

    def test_every_line_of_an_event_is_event_dialog(self):
        """Whoever speaks and however the lines advance, an event is an event."""
        a = self.a
        a.talk_to(FOCHACHA)
        a.event_begin(FOCHACHA, 39)
        a.line(b"Fochacha : You're an adventurer, right?\x7f1")
        a.advance(8)
        a.line(b"Topok-Hippok : Mom! Someone ran off with the ingredients!\x7f1")   # a second speaker
        a.line(b"Fochacha : An auto-advancing line.\x7f4\x01")                    # timed
        a.line(b"Adventurer : I'll find them.\x7f1")                              # the player speaks
        a.line(b"Fochacha bows.", mode=15)                                         # an emote
        a.release()
        a.event_begin(MARIN, 361, packet=0x034)                                    # not started by the player
        a.line(b"Marin : Are you sure you'll be all right?\x7f1")
        self.assertEqual(kinds(a), ["event"] * len(a.speaks()))
        self.assertGreaterEqual(len(a.speaks()), 5)

    def test_nothing_is_written_to_the_library(self):
        a = self.a
        a.talk_to(MARIN)
        a.event_begin(MARIN, 361, packet=0x034)
        a.line(b"Marin : Are you sure?\x7f1")
        a.line(b"Ken : Get off my case.\x7f1")
        a.release()
        self.assertEqual(list((a.dir / "library").iterdir()), [])

    def test_repeated_begin_packet_does_not_reset_the_event(self):
        """38 of 109 begins in the log were an immediate repeat of the previous one."""
        a = self.a
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"Ayame : A first line.\x7f1")
        a.advance(1)
        a.event_begin(100, 90)                                   # repeat, after a line
        a.advance(1)
        a.line(b"Ayame : A line after the repeat.\x7f1")
        self.assertEqual(self.event_lines(), 2, "the repeat kept the event's count")

    def test_a_new_event_for_the_same_npc_after_the_window_starts_fresh(self):
        a = self.a
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"Ayame : A first line.\x7f1")
        a.advance(10)                                            # well past the duplicate window
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"Ayame : Fresh conversation.\x7f1")
        self.assertEqual(self.event_lines(), 1)

    def test_battle_chatter_is_never_reclassified(self):
        a = self.a
        a.command("battle", "on")
        a.clear_sent()
        a.line(b"Prishe : Get behind me!", mode=142)
        self.assertEqual(kinds(a), ["battle"])


@unittest.skipIf(LuaRuntime is None, "lupa is not installed (pip install lupa)")
class ModelTableTests(unittest.TestCase):
    def test_model_table_names_real_archetypes(self):
        a = Addon()
        self.addCleanup(a.close)
        table = a.lua.eval(b"require('data/creature_models')")
        mapped = {int(k): (v.decode() if isinstance(v, bytes) else v) for k, v in table.items()}
        known = set(json.loads((ROOT / "server" / "archetypes.json").read_text(encoding="utf-8"))["archetypes"])
        for model, arch in mapped.items():
            self.assertIn(arch, known, f"model {model} maps to {arch!r}, which the sidecar doesn't know")
        self.assertEqual(mapped[182], "mithra_child")


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

    def test_an_unintroduced_character_is_a_speaker(self):
        self.assertEqual(self.split("??? : Hmph!"), ("???", "Hmph!"))
        self.assertEqual(self.split("?? : Hm.")[0], "??")
        self.assertEqual(self.split("What?? : said he")[0], None)


@unittest.skipIf(LuaRuntime is None, "lupa is not installed (pip install lupa)")
class UnnamedSpeakerTests(unittest.TestCase):
    """A character the game hasn't introduced yet speaks as "???". Recorded in
    Ru'Lude Gardens (243): a Nomad Moogle scene where Atori-Tutori argues with
    a Tarutaru who has no name yet."""

    NOMAD, ATORI, MARKER = 17772661, 17772700, 17772800
    HMPH = (b"??? : Hmph! And give me justaru one good reason why your associates won't help sell my "
            b"scrumptious \x1e\x02pickled rarab tails\x1e\x01!\x7f1")

    def setUp(self):
        self.a = Addon()
        self.addCleanup(self.a.close)
        self.a.add_mob(self.NOMAD, "Nomad Moogle", 0, [981])
        self.a.add_mob(self.ATORI, "Atori-Tutori", 5, [2])
        self.a.add_mob(self.MARKER, "???", 0, [1234])      # a quest marker in the same zone
        self.a.set_zone(243)
        self.a.clock(1)

    def unnamed(self):
        return [s for s in self.a.speaks() if s["speaker"].get("name", "").startswith("???")]

    def test_voiced_as_a_character_of_its_scene_with_narration_off(self):
        a = self.a
        a.talk_to(self.NOMAD)
        a.event_begin(self.NOMAD, 10191)
        a.line(b"Nomad Moogle : Curses and criminy, late again!\x7f1")
        a.line(b"Maat : Heh, speak o' the devil.\x7f1")
        a.line(self.HMPH)
        a.line(b"Atori-Tutori : I could give you a thousand-wousand reasons, my dear.\x7f1")
        a.line(b"??? : And would it kill you to try to negotiataru!?\x7f1")
        first, second = self.unnamed()
        for s in (first, second):
            self.assertEqual(s["speaker"]["name"], "??? (243:10191)", "one identity for the whole scene")
            self.assertFalse(s["speaker"]["narrator"])
            self.assertEqual(s["kind"], "event")
            self.assertNotIn("race", s["speaker"], "the zone's ??? quest marker is not this character")
            self.assertNotIn("model", s["speaker"])
        self.assertEqual(first["text"], "Hmph! And give me justaru one good reason why your associates won't help "
                                        "sell my scrumptious pickled rarab tails!")
        self.assertEqual(second["text"], "And would it kill you to try to negotiataru!?")

        a.clear_sent()
        a.command("voice", "last", "gender=female")
        (cmd,) = a.sent("cmd")
        self.assertEqual((cmd["name"], cmd["npc"]), ("voice_set", "??? (243:10191)"),
                         "//koto voice last fixes this character, not every ???")

    def test_another_scene_is_another_character(self):
        a = self.a
        a.talk_to(self.NOMAD)
        a.event_begin(self.NOMAD, 10191)
        a.line(self.HMPH)
        a.release()
        a.advance(60)
        a.talk_to(self.NOMAD)
        a.event_begin(self.NOMAD, 10192)
        a.line(b"??? : Who goes there?\x7f1")
        a.release()
        a.advance(60)
        a.line(b"??? : You there!\x7f1")                   # no event packet: a zone-in style scene
        self.assertEqual([s["speaker"]["name"] for s in self.unnamed()],
                         ["??? (243:10191)", "??? (243:10192)", "??? (243)"])

    # -- who the "???" is, and who they turn out to be ------------------------

    CHAMAMA_LOOK = [30, 4107, 8203, 12299, 16395, 20491, 24576, 28672, 0]
    ACTOR, CHAMAMA = 17772900, 17772901
    REVEAL = b"Chamama : That's Chamama! And I'll have you know that I run a perfectly reputable restaurantaru!\x7f1"

    def scene_until_reveal(self, reveal=REVEAL):
        a = self.a
        a.talk_to(self.NOMAD)
        a.event_begin(self.NOMAD, 10191)
        a.line(b"Maat : Heh, speak o' the devil. And don't look now, but he's brought a friend...\x7f1")
        a.line(self.HMPH)
        a.line(b"Atori-Tutori : I could give you a thousand-wousand reasons, my dear.\x7f1")
        a.line(b"Atori-Tutori : Ah, forgive me. This is Chumbawumba, a purveyor of pickle-wickles.\x7f1")
        a.line(reveal)
        return a.speaks()

    def line_of(self, name):
        (s,) = [s for s in self.a.speaks() if s["speaker"].get("name") == name][:1]
        return s

    def test_the_actor_shown_as_unnamed_says_who_they_are(self):
        self.a.add_mob(self.ACTOR, "???", 6, self.CHAMAMA_LOOK, distance=6)
        self.scene_until_reveal()
        speaker = self.line_of("??? (243:10191)")["speaker"]
        self.assertEqual((speaker["race"], speaker["model"]), (6, 30), "a Tarutaru woman, not a guess from one line")

    def test_actors_out_of_range_or_too_many_say_nothing(self):
        a = self.a
        a.add_mob(self.ACTOR, "???", 6, self.CHAMAMA_LOOK, distance=80)
        a.talk_to(self.NOMAD)
        a.event_begin(self.NOMAD, 10191)
        a.line(self.HMPH)
        self.assertNotIn("race", self.unnamed()[0]["speaker"], "80 yalms away: not in this scene")

        b = Addon()
        self.addCleanup(b.close)
        b.set_zone(243)
        b.add_mob(self.NOMAD, "Nomad Moogle", 0, [981])
        b.add_mob(self.ACTOR, "???", 6, self.CHAMAMA_LOOK, distance=5)
        b.add_mob(self.ACTOR + 50, "???", 8, [1, 2, 3], distance=7)
        b.talk_to(self.NOMAD)
        b.event_begin(self.NOMAD, 10191)
        b.line(self.HMPH)
        (s,) = [s for s in b.speaks() if s["speaker"]["name"].startswith("???")]
        self.assertNotIn("race", s["speaker"], "two unnamed actors: nothing says which one is talking")

    def test_what_the_actor_said_holds_for_the_whole_scene(self):
        a = self.a
        a.add_mob(self.ACTOR, "???", 6, self.CHAMAMA_LOOK, distance=6)
        a.talk_to(self.NOMAD)
        a.event_begin(self.NOMAD, 10191)
        a.line(self.HMPH)
        a.remove_mob(self.ACTOR)
        a.line(b"??? : And would it kill you to try to negotiataru!?\x7f1")
        self.assertEqual([s["speaker"].get("race") for s in self.unnamed()], [6, 6])

    def test_introduced_with_the_same_look_keeps_the_voice(self):
        self.a.add_mob(self.ACTOR, "???", 6, self.CHAMAMA_LOOK, distance=6)
        self.a.add_mob(self.CHAMAMA, "Chamama", 6, self.CHAMAMA_LOOK, distance=6)
        self.scene_until_reveal(b"Chamama : Hmph!\x7f1")          # no name said: the look is enough
        self.assertEqual(self.line_of("Chamama")["speaker"]["was"], "??? (243:10191)")
        self.assertNotIn("was", self.line_of("Atori-Tutori")["speaker"])

    def test_introduced_by_name_when_looks_are_unknown(self):
        self.scene_until_reveal()                                   # only the quest marker is "???"
        self.assertEqual(self.line_of("Chamama")["speaker"]["was"], "??? (243:10191)", "That's Chamama!")
        self.assertNotIn("was", self.line_of("Atori-Tutori")["speaker"], "he never says his own name")

    def test_named_in_the_line_before(self):
        a = self.a
        a.talk_to(self.NOMAD)
        a.event_begin(self.NOMAD, 10191)
        a.line(self.HMPH)
        a.line(b"Atori-Tutori : Why, Chamama, what a surprise-wise!\x7f1")
        a.line(b"Chamama : Hmph!\x7f1")
        self.assertEqual(self.line_of("Chamama")["speaker"]["was"], "??? (243:10191)")

    def test_different_looks_are_different_characters(self):
        self.a.add_mob(self.ACTOR, "???", 6, self.CHAMAMA_LOOK, distance=6)
        self.a.add_mob(self.CHAMAMA, "Chamama", 6, [31, 1, 2, 3, 4, 5, 6, 7, 0], distance=6)
        self.scene_until_reveal()
        self.assertNotIn("was", self.line_of("Chamama")["speaker"], "says her name, but looks nothing alike")

    def test_only_a_first_line_can_be_a_reveal(self):
        a = self.a
        a.talk_to(self.NOMAD)
        a.event_begin(self.NOMAD, 10191)
        a.line(b"Maat : Heh, speak o' the devil.\x7f1")
        a.line(self.HMPH)
        a.line(b"Maat : I'm Maat, and I've seen it all.\x7f1")   # spoke before the ??? appeared
        self.assertEqual([s["speaker"].get("was") for s in a.speaks() if s["speaker"].get("name") == "Maat"],
                         [None, None])

    def test_a_new_zone_forgets_who_was_who(self):
        a = self.a
        a.add_mob(self.ACTOR, "???", 6, self.CHAMAMA_LOOK, distance=6)
        a.talk_to(self.NOMAD)
        a.event_begin(self.NOMAD, 10191)
        a.line(self.HMPH)
        a.release()
        a.zone_out()
        a.remove_mob(self.ACTOR)
        a.set_zone(230)
        a.add_mob(self.CHAMAMA, "Chamama", 6, self.CHAMAMA_LOOK, distance=6)   # same look, another zone
        a.advance(60)
        a.line(b"Chamama : That's Chamama!\x7f1")
        self.assertNotIn("was", self.line_of("Chamama")["speaker"])


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
        a.command("mode", "events")
        a.npc_chat(100)
        a.line(b"Ayame : Welcome!", mode=144)
        self.assertEqual(a.speaks(), [], "events mode drops a bark")
        a.advance(5)
        a.talk_to(100)
        a.event_begin(100, 90)
        a.line(b"Ayame : Ordinary dialog.\x7f1")
        self.assertEqual(kinds(a), ["event"])
        a.release()
        a.command("mode", "all")
        a.advance(5)
        a.npc_chat(100)
        a.line(b"Ayame : Welcome back!", mode=144)
        self.assertEqual(kinds(a), ["event", "ambient"])

    def test_saved_cutscenes_mode_loads_as_events(self):
        a = self.a
        a.lua.execute(b"""
            local real = config.load
            config.load = function(d) local s = real(d); s.mode = 'cutscenes'; return s end
        """)
        a.fire("load")
        a.command("mode")
        self.assertIn("mode is events", a.chat()[-1])

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
        a.command("mode", "events")
        a.command("test", "Prishe", ":", "hello", "there.")
        self.assertEqual(a.speaks(), [], "a plain test line is ambient and events mode filters it")
        a.command("test", "event", "Prishe", ":", "hello", "there.")
        self.assertEqual(kinds(a), ["event"])

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

    def test_norm_command(self):
        a = self.a
        a.clear_sent()
        a.command("status")
        a.command("norm")
        self.assertEqual(a.sent("config")[-1]["normalize"], "", "untouched: the server's config decides")
        a.command("norm", "off")
        self.assertIs(a.sent("config")[-1]["normalize"], False)
        a.command("normalize", "ON")
        self.assertIs(a.sent("config")[-1]["normalize"], True)
        a.clear_sent()
        a.queue_from_sidecar({"t": "pong"})     # a sidecar restart carries it again
        a.advance(1)
        a.prerender()
        self.assertIs(a.sent("config")[-1]["normalize"], True)
        a.command("norm", "default")
        self.assertEqual(a.sent("config")[-1]["normalize"], "")

    def test_provider_command_is_sent_with_config_and_survives_reconnects(self):
        a = self.a
        a.clear_sent()
        a.command("provider", "elevenlabs")
        self.assertEqual(a.sent("config")[-1]["provider"], "elevenlabs")
        # A sidecar restart: the next hello/config exchange carries it again.
        a.clear_sent()
        a.queue_from_sidecar({"t": "pong"})
        a.advance(1)
        a.prerender()
        self.assertEqual(a.sent("config")[-1]["provider"], "elevenlabs")
        a.command("provider", "default")
        self.assertEqual(a.sent("config")[-1]["provider"], "", "empty = use the server's configured provider")
        a.clear_sent()
        a.command("provider", "bad/name!")
        self.assertEqual(a.sent("config"), [], "only the name's shape is checked here")
        self.assertIn("usage: //koto provider", a.chat()[-1])

    def test_model_adapter_providers_go_to_the_sidecar(self):
        a = self.a
        a.clear_sent()
        a.command("provider", "MiniMax")              # case-insensitive
        self.assertEqual(a.sent("config")[-1]["provider"], "minimax")
        a.command("provider", "chatterbox-turbo")
        self.assertEqual(a.sent("config")[-1]["provider"], "chatterbox-turbo",
                         "the sidecar decides which names exist")
        a.command("providers")
        self.assertEqual(a.sent("cmd")[-1]["name"], "providers_info")

    def test_quick_sidecar_restart_gets_hello_and_settings_again(self):
        a = self.a
        a.command("volume", "250")
        a.command("provider", "elevenlabs")
        a.queue_from_sidecar({"t": "pong"})                 # link up: the addon greets on connect
        a.advance(1)
        a.prerender()
        a.clear_sent()
        a.queue_from_sidecar({"t": "pong", "want_hello": True})
        a.advance(0.5)
        a.prerender()
        self.assertEqual(a.sent("hello"), [], "just greeted on connect; no second hello")

        # Restarted (R in the sidecar terminal) well inside the 15 s link timeout.
        a.advance(5)
        a.queue_from_sidecar({"t": "pong", "want_hello": True})
        a.advance(0.2)
        a.prerender()
        self.assertEqual(len(a.sent("hello")), 1)
        cfg = a.sent("config")[-1]
        self.assertEqual((cfg["volume"], cfg["provider"]), (250, "elevenlabs"))
        self.assertTrue(any("voice server restarted" in m for m in a.chat()))

        a.clear_sent()
        a.advance(5)
        a.queue_from_sidecar({"t": "pong"})
        a.advance(0.2)
        a.prerender()
        self.assertEqual(a.sent("hello"), [], "plain pongs change nothing")

    def test_provider_defaults_to_the_server_config(self):
        a = self.a
        a.clear_sent()
        a.command("volume", "90")
        self.assertEqual(a.sent("config")[-1]["provider"], "")

    def test_voices_commands(self):
        a = self.a
        a.clear_sent()
        a.command("voices")
        a.command("voices", "sync")
        self.assertEqual([c["name"] for c in a.sent("cmd")], ["voices_info", "voices_sync"])

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


class ReplayLogTests(unittest.TestCase):
    """replay_log.py reads logs from before and after status= was added, and from
    before the cutscene kind was removed."""

    def test_line_records_parse_with_and_without_the_status_field(self):
        import replay_log
        before = ("12:00:01 line mode=150 kind=cutscene timed=true speaker=Makki-Chebukki race=0 "
                  "model=1445 text=[Oho] raw=[4F 68 6F 7F 31 ]")
        after = before.replace("model=1445 ", "model=1445 status=4 ")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "debug.log"
            path.write_text(before + "\n" + after + "\n", encoding="utf-8")
            records = list(replay_log.parse(path))
        self.assertEqual(len(records), 2, records)
        self.assertEqual(records[0], records[1])
        self.assertEqual(records[0][2]["raw"], b"Oho\x7f1")
        self.assertEqual(replay_log.OLD_KINDS[records[0][2]["old"]], "event")


if __name__ == "__main__":
    unittest.main()
