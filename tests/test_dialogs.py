from __future__ import annotations

import subprocess
import unittest
from unittest import mock
from xml.dom import minidom

from gpu_game_guard import dialogs
from gpu_game_guard.model import Launch, Target


def target(name="ollama", image="ollama/ollama:0.4", vram=None):
    return Target("container", "docker", name, image, vram)


def launch(title="Half-Life <2>"):
    return Launch("steam", (1, "2"), "steam:70", title)


def done(code=0, out=""):
    return subprocess.CompletedProcess([], code, stdout=out)


def well_formed(text):
    return minidom.parseString(f"<root>{text.replace('<br>', '<br/>')}</root>")


class FakeRun:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls, self.timeouts = result or done(), error, [], []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        self.timeouts.append(kwargs.get("timeout"))
        if self.error:
            raise self.error
        return self.result


def patched(run):
    return mock.patch("gpu_game_guard.dialogs.subprocess.run", run)


def which_of(*present):
    return lambda cmd: f"/usr/bin/{cmd}" if cmd in present else None


BAD = "a\x00b\x01c\x08d\x0be\x0cf\x0eg\x1fh\x7fi\ud800j&<>"
CONTROLS = "".join(chr(c) for c in list(range(0, 9)) + [11, 12] + list(range(14, 32)) + [127])


def argument_of(command, flag):
    return command[command.index(flag) + 1]


def assert_clean(case, text):
    case.assertNotIn("\x00", text)
    text.encode("utf-8")  # raises on a lone surrogate
    for char in CONTROLS:
        case.assertNotIn(char, text)


class FormatTests(unittest.TestCase):
    def test_format_mib_uses_binary_units(self):
        self.assertEqual(dialogs.format_mib(512), "512 MiB")
        self.assertEqual(dialogs.format_mib(1023), "1023 MiB")
        self.assertEqual(dialogs.format_mib(1536), "1.5 GiB")
        self.assertEqual(dialogs.format_mib(16311), "15.9 GiB")


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.targets = [target("a&b<c>", "img&<x>", 1536), target("whisper", "w", 0)]

    def test_escaping_in_every_markup(self):
        for markup in ("html", "pango"):
            message = dialogs.launch_message(launch(), self.targets, (5120, 16311), markup)
            well_formed(message)
            self.assertIn("a&amp;b&lt;c&gt;", message)
            self.assertIn("Half-Life &lt;2&gt;", message)
            self.assertIn("img&amp;&lt;x&gt;", message)
            self.assertNotIn("a&b<c>", message)
            well_formed(dialogs.restart_message(["a&b<c>"], markup))
            self.assertIn("a&amp;b&lt;c&gt;", dialogs.restart_message(["a&b<c>"], markup))
        plain = dialogs.launch_message(launch(), self.targets, None, "plain")
        self.assertIn("a&b<c>", plain)
        self.assertIn("Half-Life <2>", plain)
        self.assertNotIn("<b>", plain)

    def test_control_characters_and_surrogates_are_replaced(self):
        hostile = [target(BAD, BAD, 100)]
        for markup in ("html", "pango", "plain"):
            message = dialogs.launch_message(launch(BAD), hostile, None, markup)
            assert_clean(self, message)
            restart = dialogs.restart_message([BAD], markup)
            assert_clean(self, restart)
            if markup != "plain":
                well_formed(message)
                well_formed(restart)
        self.assertIn("a b c d e f g h i j", dialogs.launch_message(launch(BAD), hostile, None, "plain"))

    def test_every_control_character_alone(self):
        for char in CONTROLS + "\ud800\udfff":
            for markup in ("html", "pango"):
                well_formed(dialogs.launch_message(launch(f"x{char}y"), [target(f"n{char}", f"i{char}", 1)], None,
                                                   markup))

    def test_content(self):
        for markup in ("html", "pango", "plain"):
            message = dialogs.launch_message(launch("Game"), self.targets, (5120, 16311), markup)
            self.assertIn("GPU Game Guard", message)
            self.assertIn("1.5 GiB of GPU memory", message)
            self.assertIn("no GPU memory measured right now", message)
            self.assertIn("by all programs: 5.0 GiB of 15.9 GiB", message)
            self.assertNotIn("by all programs", dialogs.launch_message(launch("g"), [], None, markup))
            self.assertNotIn("idle", message)
            self.assertNotIn("GB", message.replace("GiB", ""))

    def test_unmeasured_and_zero_are_reported_differently(self):
        unknown = dialogs.launch_message(launch(), [target("u", "", None)], None, "plain")
        zero = dialogs.launch_message(launch(), [target("z", "", 0)], None, "plain")
        self.assertIn("GPU memory use unknown", unknown)
        self.assertNotIn("no GPU memory in use", unknown)
        self.assertIn("no GPU memory measured right now", zero)
        self.assertNotIn("unknown", zero)

    def test_wording_follows_the_kinds_of_the_targets(self):
        def intro(*kinds):
            targets = [Target(k, "x", f"n{i}", "", 1) for i, k in enumerate(kinds)]
            return dialogs.launch_message(launch("G"), targets, None, "plain")
        self.assertIn("because this container has access", intro("container"))
        self.assertIn("because this service has access", intro("service"))
        self.assertIn("because this program has access", intro("process"))
        self.assertIn("because these services have access", intro("service", "service"))
        self.assertIn("because these programs have access", intro("process", "command"))
        self.assertIn("because these containers, services or programs have access",
                      intro("process", "container", "service"))
        self.assertIn("because these containers or services have access", intro("service", "container"))
        self.assertIn("this program has", intro("mystery"))
        self.assertNotIn("using the GPU", intro("container", "service"))
        self.assertEqual(dialogs.subject_phrase(["container"]), ("this container", "has"))
        self.assertEqual(dialogs.subject_phrase(["container", "service"]), ("these containers or services", "have"))
        self.assertEqual(dialogs.subject_phrase(), ("these programs and services", "have"))
        self.assertEqual(dialogs.subject_phrase([]), ("these programs and services", "have"))
        self.assertNotIn("containers", intro("service"))
        self.assertEqual(dialogs.kind_phrase(), "programs and services")
        self.assertIn("stopped these services or programs earlier",
                      dialogs.restart_message(["a", "b"], "plain", ["service", "process"]))

    def test_restart_dialogs_name_the_kind_of_the_refs(self):
        run = FakeRun(done(0))
        with patched(run):
            dialogs.ZenityPrompter().restart([{"name": "a", "kind": "service"}])
        self.assertIn("stopped these services earlier", argument_of(run.calls[0], "--text"))

    def test_notification_that_cannot_ask_is_kind_neutral(self):
        p = dialogs.NotifyPrompter(which_of("notify-send"))
        p._actions = False
        with mock.patch.object(p, "notify") as notify:
            p.launch(launch("G"), [Target("service", "service", "s", "", 1)], None)
        self.assertIn("GPU services running", notify.call_args[0][1])
        self.assertNotIn("containers", notify.call_args[0][1])

    def test_bidirectional_controls_are_removed(self):
        text = dialogs.launch_message(launch("a\u202eb\u2066c\u200fd"), [target()], None, "plain")
        self.assertIn("a b c\u200fd", text)

    def test_marks_of_right_to_left_text_stay_and_overrides_go(self):
        title = "\u0645\u0631\u062d\u0628\u0627\u061c \u05e9\u05dc\u05d5\u05dd\u200f abc\u200e"
        text = dialogs.launch_message(launch(title + "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"),
                                      [target()], None, "plain")
        self.assertIn(title, text)
        for char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069":
            self.assertNotIn(char, text)

    def test_line_separators_become_a_space_and_joiners_stay(self):
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        text = dialogs.launch_message(launch(f"a\u2028b\u2029c\x0bd\x0ce\x85f{family}\u200c"), [target()], None,
                                      "plain")
        self.assertIn(f"a b c d e f{family}\u200c", text)

    def test_restart_message_is_neutral(self):
        for markup in ("html", "pango", "plain"):
            message = dialogs.restart_message(["a"], markup)
            self.assertIn("GPU Game Guard", message)
            self.assertIn("stopped these programs and services earlier. Restart them?", message)
            self.assertIn("stopped these containers earlier", dialogs.restart_message(["a"], markup, ["container"]))
            self.assertNotIn("closed", message)

    def test_pango_has_no_list_markup_and_uses_bullets(self):
        message = dialogs.launch_message(launch(), self.targets, None, "pango")
        for tag in ("<ul>", "<li>", "<br>", "<p>"):
            self.assertNotIn(tag, message)
        self.assertIn("•", message)
        self.assertIn("\n", message)
        self.assertIn("<small>", message)

    def test_html_uses_a_list(self):
        message = dialogs.launch_message(launch(), self.targets, None, "html")
        self.assertIn("<ul>", message)
        self.assertEqual(message.count("<li>"), 2)

    def test_impact_none_measured(self):
        for targets in ([target(vram=None)], [target(vram=None), target("b", vram=None)]):
            sentence = dialogs.impact_sentence(targets, "plain")
            self.assertEqual(sentence, "GPU memory use could not be measured, so the impact on the game is unknown.")

    def test_impact_some_unmeasured(self):
        modest = dialogs.impact_sentence([target(vram=1100), target("b", vram=None)], "plain")
        self.assertIn("at least 1.1 GiB held by the ones that could be measured; the rest could not be measured.",
                      modest)
        self.assertNotIn("run out of memory", modest)
        heavy = dialogs.impact_sentence([target(vram=5000), target("b", vram=None)], "html")
        self.assertIn("at least <b>4.9 GiB</b> held by the ones that could be measured", heavy)
        self.assertIn("run out of memory", heavy)
        none_held = dialogs.impact_sentence([target(vram=0), target("b", vram=None)], "plain")
        self.assertIn("the rest could not be measured", none_held)
        self.assertNotIn("not holding", none_held)
        self.assertNotIn("should be small", none_held)

    def test_impact_heavy(self):
        several = dialogs.impact_sentence([target(vram=3000), target("b", vram=1500)], "html")
        self.assertIn("Together they hold <b>4.4 GiB</b> of GPU memory and compete for GPU time", several)
        self.assertIn("could stutter, load slowly or run out of memory", several)
        single = dialogs.impact_sentence([target(vram=5000)], "plain")
        self.assertIn("It holds 4.9 GiB of GPU memory and competes for GPU time", single)
        self.assertNotIn("Together", single)
        self.assertNotIn("<b>", single)

    def test_impact_modest(self):
        several = dialogs.impact_sentence([target(vram=1000), target("b", vram=100)], "plain")
        self.assertIn("Together they hold 1.1 GiB of GPU memory", several)
        self.assertIn("modest", several)
        single = dialogs.impact_sentence([target(vram=1100)], "plain")
        self.assertIn("It holds 1.1 GiB", single)
        self.assertIn("modest", single)
        self.assertNotIn("Together", single)

    def test_impact_none_held(self):
        sentence = dialogs.impact_sentence([target(vram=0), target("b", vram=0)], "plain")
        self.assertEqual(sentence, "No GPU memory is measured as held by them right now, so the impact on the game "
                                   "should be small, though they can claim the GPU as soon as they get work.")
        self.assertIn("No GPU memory is measured as held by it", dialogs.impact_sentence([target(vram=0)], "plain"))

    def test_a_zero_heavy_threshold_does_not_make_nothing_heavy(self):
        original = dialogs.HEAVY_VRAM_MIB
        self.addCleanup(setattr, dialogs, "HEAVY_VRAM_MIB", original)
        dialogs.HEAVY_VRAM_MIB = 0
        sentence = dialogs.impact_sentence([target(vram=0)], "plain")
        self.assertNotIn("stutter", sentence)
        self.assertIn("stutter", dialogs.impact_sentence([target(vram=5)], "plain"))

    def test_impact_never_says_idle_or_bottleneck(self):
        cases = ([target(vram=None)], [target(vram=0)], [target(vram=10)], [target(vram=9000)],
                 [target(vram=9000), target("b", vram=None)], [target(vram=0), target("b", vram=None)])
        for targets in cases:
            sentence = dialogs.impact_sentence(targets, "plain")
            self.assertNotIn("idle", sentence)
            self.assertNotIn("bottleneck", sentence)

    def test_empty_targets_have_no_impact(self):
        self.assertEqual(dialogs.impact_sentence([], "plain"), "")
        for markup in ("html", "pango", "plain"):
            dialogs.launch_message(launch(), [], None, markup)

    def test_heavy_threshold_is_overridable(self):
        with mock.patch.object(dialogs, "HEAVY_VRAM_MIB", 1000):
            self.assertIn("run out of memory", dialogs.impact_sentence([target(vram=1100)], "plain"))
        self.assertIn("modest", dialogs.impact_sentence([target(vram=4095)], "plain"))
        self.assertIn("run out of memory", dialogs.impact_sentence([target(vram=4096)], "plain"))


class ZenityTests(unittest.TestCase):
    def ask(self, result):
        run = FakeRun(result)
        with patched(run):
            return dialogs.ZenityPrompter().launch(launch(), [target("a&b<c>")], None), run

    def test_launch_exit_code_mapping(self):
        self.assertEqual(self.ask(done(0))[0], "stop")
        self.assertEqual(self.ask(done(1, "Cancel\n"))[0], "cancel")
        self.assertEqual(self.ask(done(1, ""))[0], "anyway")
        self.assertEqual(self.ask(done(1, "Launch anyway\n"))[0], "anyway")
        self.assertEqual(self.ask(done(1, "something else\n"))[0], "anyway")
        for code in (5, 255, -1, -15):
            self.assertEqual(self.ask(done(code))[0], "anyway")
            self.assertEqual(self.ask(done(code, "Cancel\n"))[0], "anyway")
        self.assertEqual(self.ask(done(1, None))[0], "anyway")

    def test_restart_exit_code_mapping(self):
        cases = ((done(0), True), (done(1, "Leave stopped\n"), False), (done(1, ""), None),
                 (done(1, "x\n"), None), (done(1, None), None), (done(5), None), (done(255), None),
                 (done(-15), None), (done(5, "Leave stopped\n"), None))
        for result, expected in cases:
            with patched(FakeRun(result)):
                self.assertEqual(dialogs.ZenityPrompter().restart([{"name": "a&b"}]), expected)

    def test_launch_command_line(self):
        command = self.ask(done(0))[1].calls[0]
        self.assertEqual(command[:2], ["zenity", "--question"])
        for flag, value in (("--title", "GPU Game Guard"), ("--ok-label", "Stop and launch"),
                            ("--cancel-label", "Launch anyway"), ("--extra-button", "Cancel")):
            self.assertEqual(argument_of(command, flag), value)
        text = argument_of(command, "--text")
        well_formed(text)
        self.assertIn("a&amp;b&lt;c&gt;", text)

    def test_restart_command_line(self):
        run = FakeRun(done(0))
        with patched(run):
            dialogs.ZenityPrompter().restart([{"name": "a&b"}])
        command = run.calls[0]
        self.assertEqual(argument_of(command, "--ok-label"), "Restart")
        self.assertEqual(argument_of(command, "--cancel-label"), "Ask me later")
        self.assertEqual(argument_of(command, "--extra-button"), "Leave stopped")
        text = argument_of(command, "--text")
        well_formed(text)
        self.assertIn("stopped these containers earlier", text)

    def test_hostile_text_never_reaches_the_command_line(self):
        run = FakeRun(done(0))
        with patched(run):
            p = dialogs.ZenityPrompter()
            p.launch(launch(BAD), [target(BAD, BAD, 1)], None)
            p.restart([{"name": BAD}])
        for command in run.calls:
            for argument in command:
                assert_clean(self, argument)
            well_formed(argument_of(command, "--text"))

    def test_empty_restart_shows_nothing(self):
        run = FakeRun(done(0))
        with patched(run):
            self.assertIsNone(dialogs.ZenityPrompter().restart([]))
            self.assertIsNone(dialogs.ZenityPrompter().restart([{"name": ""}, {}]))
        self.assertEqual(run.calls, [])

    def test_blocking_questions_have_no_timeout(self):
        self.assertEqual(self.ask(done(0))[1].timeouts, [None])

    def test_failures_fail_safe(self):
        errors = (FileNotFoundError("zenity"), OSError("gone"), ValueError("embedded null byte"),
                  subprocess.TimeoutExpired("zenity", 1), subprocess.SubprocessError("x"))
        for error in errors:
            with patched(FakeRun(error=error)):
                p = dialogs.ZenityPrompter()
                self.assertEqual(p.launch(launch(), [target()], None), "anyway")
                self.assertIsNone(p.restart([{"name": "a"}]))

    def test_can_ask(self):
        self.assertTrue(dialogs.ZenityPrompter().can_ask)


class KdialogTests(unittest.TestCase):
    def test_launch_mapping(self):
        for code, expected in ((0, "stop"), (1, "anyway"), (2, "cancel"), (3, "anyway"), (255, "anyway"),
                               (-9, "anyway"), (-15, "anyway")):
            with patched(FakeRun(done(code))):
                self.assertEqual(dialogs.KdialogPrompter().launch(launch(), [target()], None), expected)

    def test_launch_command(self):
        run = FakeRun()
        with patched(run):
            dialogs.KdialogPrompter().launch(launch(), [target("a&b<c>")], None)
        command = run.calls[0]
        self.assertIn("--yesnocancel", command)
        self.assertEqual(argument_of(command, "--yes-label"), "Stop and launch")
        self.assertEqual(argument_of(command, "--no-label"), "Launch anyway")
        self.assertEqual(argument_of(command, "--cancel-label"), "Cancel")
        self.assertIn("a&amp;b&lt;c&gt;", command[-1])
        well_formed(command[-1])

    def test_restart_mapping(self):
        for code, expected in ((0, True), (1, False), (2, None), (3, None), (134, None), (255, None), (-9, None), (-15, None)):
            with patched(FakeRun(done(code))):
                self.assertEqual(dialogs.KdialogPrompter().restart([{"name": "a"}]), expected)

    def test_restart_command(self):
        run = FakeRun()
        with patched(run):
            dialogs.KdialogPrompter().restart([{"name": "a&b"}])
        command = run.calls[0]
        self.assertIn("--yesnocancel", command)
        self.assertEqual(argument_of(command, "--yes-label"), "Restart")
        self.assertEqual(argument_of(command, "--no-label"), "Leave stopped")
        self.assertEqual(argument_of(command, "--cancel-label"), "Ask me later")
        self.assertIn("a&amp;b", command[-1])
        well_formed(command[-1])

    def test_hostile_text_never_reaches_the_command_line(self):
        run = FakeRun()
        with patched(run):
            p = dialogs.KdialogPrompter()
            p.launch(launch(BAD), [target(BAD, BAD, 1)], None)
            p.restart([{"name": BAD}])
            p.notify(BAD, BAD)
        self.assertEqual(len(run.calls), 3)
        for command in run.calls:
            for argument in command:
                assert_clean(self, argument)
        well_formed(run.calls[0][-1])
        well_formed(run.calls[1][-1])
        well_formed(argument_of(run.calls[2], "--passivepopup"))

    def test_empty_restart_shows_nothing(self):
        run = FakeRun()
        with patched(run):
            self.assertIsNone(dialogs.KdialogPrompter().restart([]))
        self.assertEqual(run.calls, [])

    def test_failures_fail_safe(self):
        errors = (OSError("gone"), ValueError("embedded null byte"), subprocess.TimeoutExpired("kdialog", 1))
        for error in errors:
            with patched(FakeRun(error=error)):
                p = dialogs.KdialogPrompter()
                self.assertEqual(p.launch(launch(), [target()], None), "anyway")
                self.assertIsNone(p.restart([{"name": "a"}]))
                p.notify("t", "b")

    def test_notify_uses_passivepopup_with_escaped_body(self):
        run = FakeRun()
        with patched(run):
            dialogs.KdialogPrompter().notify("t", "a&b <c>")
        self.assertIn("--passivepopup", run.calls[0])
        self.assertEqual(argument_of(run.calls[0], "--passivepopup"), "a&amp;b &lt;c&gt;")

    def test_can_ask(self):
        self.assertTrue(dialogs.KdialogPrompter().can_ask)


class NotifyTests(unittest.TestCase):
    def prompter(self, help_text, answer=""):
        def run(command, **kwargs):
            run.calls.append(command)
            if "--help" in command:
                return done(0, help_text)
            return done(0, answer)
        run.calls = []
        return dialogs.NotifyPrompter(which_of("notify-send")), run

    def test_without_actions_it_only_informs(self):
        p, run = self.prompter("Usage: notify-send [OPTION...]")
        with patched(run):
            self.assertFalse(p.can_ask)
            self.assertEqual(p.launch(launch(), [target()], None), "anyway")
            self.assertIsNone(p.restart([{"name": "a"}]))
        self.assertFalse(any("--wait" in c for c in run.calls))

    def test_can_ask_follows_action_support_and_is_cached(self):
        p, run = self.prompter("  -A, --action=[NAME=]Text...")
        with patched(run):
            self.assertTrue(p.can_ask)
            self.assertTrue(p.can_ask)
        self.assertEqual(sum("--help" in c for c in run.calls), 1)
        with patched(FakeRun(error=OSError("gone"))):
            self.assertFalse(dialogs.NotifyPrompter(which_of("notify-send")).can_ask)

    def test_with_actions_the_choice_is_returned(self):
        for answer, expected in (("stop\n", "stop"), ("anyway\n", "anyway"), ("cancel\n", "cancel"), ("", "anyway"),
                                 ("bogus\n", "anyway")):
            p, run = self.prompter("  -A, --action=[NAME=]Text...", answer)
            with patched(run):
                self.assertEqual(p.launch(launch(), [target()], None), expected)
            ask = run.calls[-1]
            self.assertIn("--wait", ask)
            self.assertIn("--action=stop=Stop and launch", ask)
        for answer, expected in (("restart\n", True), ("leave\n", False), ("", None), ("bogus\n", None)):
            p, run = self.prompter("--action", answer)
            with patched(run):
                self.assertEqual(p.restart([{"name": "a"}]), expected)

    def test_restart_offers_the_neutral_wording(self):
        p, run = self.prompter("--action", "restart\n")
        with patched(run):
            p.restart([{"name": "a"}])
        self.assertIn("stopped these containers earlier. Restart them?", run.calls[-1][-1])

    def test_notification_text_is_escaped_and_clean(self):
        p, run = self.prompter("--action", "stop\n")
        with patched(run):
            p.launch(launch(BAD), [target(BAD, BAD, 1)], None)
            p.restart([{"name": BAD}])
            p.notify(BAD, BAD)
        self.assertEqual(len(run.calls), 4)
        for command in run.calls[1:]:
            for argument in command:
                assert_clean(self, argument)
            well_formed(command[-1])
            well_formed(command[-2])
            self.assertIn("&amp;&lt;&gt;", command[-1])

    def test_launch_body_names_with_markup_characters_are_escaped(self):
        p, run = self.prompter("--action", "stop\n")
        with patched(run):
            p.launch(launch(), [target("a&b<c>")], None)
        body = run.calls[-1][-1]
        well_formed(body)
        self.assertIn("a&amp;b&lt;c&gt;", body)
        self.assertIn("Half-Life &lt;2&gt;", body)

    def test_empty_restart_shows_nothing(self):
        p, run = self.prompter("--action", "restart\n")
        with patched(run):
            self.assertIsNone(p.restart([]))
        self.assertFalse(any("--wait" in c for c in run.calls))

    def test_failures_fail_safe(self):
        for error in (OSError("gone"), ValueError("embedded null byte"), subprocess.TimeoutExpired("notify-send", 1)):
            with patched(FakeRun(error=error)):
                p = dialogs.NotifyPrompter(which_of("notify-send"))
                self.assertEqual(p.launch(launch(), [target()], None), "anyway")
                self.assertIsNone(p.restart([{"name": "a"}]))
                p.notify("t", "b")

    def test_failure_after_support_check_fails_safe(self):
        def run(command, **kwargs):
            if "--help" in command:
                return done(0, "--action")
            raise subprocess.TimeoutExpired("notify-send", 1)
        p = dialogs.NotifyPrompter(which_of("notify-send"))
        with patched(run):
            self.assertEqual(p.launch(launch(), [target()], None), "anyway")
            self.assertIsNone(p.restart([{"name": "a"}]))

    def test_support_check_is_cached(self):
        p, run = self.prompter("--action")
        with patched(run):
            p.supports_actions()
            p.supports_actions()
        self.assertEqual(sum("--help" in c for c in run.calls), 1)


class BaseNotifyTests(unittest.TestCase):
    def test_zenity_notification_is_escaped_and_clean(self):
        run = FakeRun()
        with patched(run):
            dialogs.ZenityPrompter(which_of("notify-send")).notify(BAD, BAD)
        command = run.calls[0]
        self.assertEqual(command[0], "notify-send")
        for argument in command:
            assert_clean(self, argument)
        well_formed(command[-1])
        well_formed(command[-2])
        self.assertEqual(command[-1], "a b c d e f g h i j&amp;&lt;&gt;")

    def test_without_notify_send_only_logs(self):
        run = FakeRun()
        with patched(run), mock.patch("gpu_game_guard.dialogs.log") as log:
            dialogs.ZenityPrompter(which_of()).notify("t", "b")
        self.assertEqual(run.calls, [])
        log.assert_called()


class NullTests(unittest.TestCase):
    def test_behaviour(self):
        p = dialogs.NullPrompter()
        self.assertTrue(p.available())
        self.assertFalse(p.can_ask)
        self.assertEqual(p.launch(launch(), [target()], None), "anyway")
        self.assertIsNone(p.restart([{"name": "a"}]))
        with mock.patch("gpu_game_guard.dialogs.log") as log:
            p.notify("t\x00", "b\ud800")
        log.assert_called()
        for argument in log.call_args[0]:
            assert_clean(self, argument)


class ChooseTests(unittest.TestCase):
    KDE = {"XDG_CURRENT_DESKTOP": "KDE"}
    GNOME = {"XDG_CURRENT_DESKTOP": "GNOME"}

    def name(self, present, environ, preference="auto"):
        return dialogs.choose(preference, environ, which_of(*present)).name

    def test_kde_order(self):
        self.assertEqual(self.name(["kdialog", "zenity", "notify-send"], self.KDE), "kdialog")
        self.assertEqual(self.name(["zenity", "notify-send"], self.KDE), "zenity")
        self.assertEqual(self.name(["kdialog"], self.KDE), "kdialog")
        self.assertEqual(self.name(["notify-send"], self.KDE), "notify")
        self.assertEqual(self.name([], self.KDE), "none")

    def test_kde_full_session(self):
        self.assertEqual(self.name(["kdialog", "zenity"], {"KDE_FULL_SESSION": "true"}), "kdialog")

    def test_other_desktops_order(self):
        self.assertEqual(self.name(["kdialog", "zenity", "notify-send"], self.GNOME), "zenity")
        self.assertEqual(self.name(["kdialog", "notify-send"], self.GNOME), "kdialog")
        self.assertEqual(self.name(["zenity"], {}), "zenity")
        self.assertEqual(self.name(["notify-send"], {}), "notify")
        self.assertEqual(self.name([], {}), "none")

    def test_explicit_preference(self):
        self.assertEqual(self.name(["kdialog", "zenity"], self.KDE, "zenity"), "zenity")
        self.assertEqual(self.name(["kdialog", "zenity"], self.GNOME, "kdialog"), "kdialog")
        self.assertEqual(self.name(["zenity"], self.GNOME, "notify"), "zenity")
        self.assertEqual(self.name(["kdialog", "zenity"], self.GNOME, "none"), "none")

    def test_unavailable_preference_falls_back_with_one_log_line(self):
        with mock.patch("gpu_game_guard.dialogs.log") as log:
            self.assertEqual(self.name(["zenity"], self.KDE, "kdialog"), "zenity")
        self.assertEqual(log.call_count, 1)

    def fake_qt(self, available=True):
        fake = mock.Mock()
        fake.return_value.available.return_value = available
        fake.return_value.name = "qt"
        return mock.patch("gpu_game_guard.ui.prompt.QtPrompter", fake), fake

    def test_auto_prefers_qt_on_every_desktop(self):
        patch, fake = self.fake_qt()
        with patch:
            for env in (self.KDE, self.GNOME, {}):
                self.assertEqual(self.name(["kdialog", "zenity", "notify-send"], env), "qt")
            self.assertEqual(self.name([], {}), "qt")
        self.assertEqual(fake.call_args[1]["environ"], {})

    def test_unavailable_qt_keeps_the_existing_order(self):
        patch, _ = self.fake_qt(False)
        with patch:
            self.assertEqual(self.name(["kdialog", "zenity"], self.KDE), "kdialog")
            self.assertEqual(self.name(["kdialog", "zenity"], self.GNOME), "zenity")
            self.assertEqual(self.name([], {}), "none")

    def test_qt_import_error_means_unavailable(self):
        with mock.patch.dict("sys.modules", {"gpu_game_guard.ui.prompt": None}):
            self.assertEqual(self.name(["zenity"], self.GNOME), "zenity")

    def test_qt_constructor_error_means_unavailable(self):
        with mock.patch("gpu_game_guard.ui.prompt.QtPrompter", side_effect=RuntimeError("x")):
            self.assertEqual(self.name(["zenity"], self.GNOME), "zenity")

    def test_explicit_qt_preference(self):
        patch, _ = self.fake_qt()
        with patch:
            self.assertEqual(self.name(["zenity"], self.GNOME, "qt"), "qt")
        patch, _ = self.fake_qt(False)
        with patch, mock.patch("gpu_game_guard.dialogs.log") as log:
            self.assertEqual(self.name(["zenity"], self.GNOME, "qt"), "zenity")
        self.assertEqual(log.call_count, 1)

    def test_real_qt_prompter_needs_a_display(self):
        self.assertNotEqual(self.name(["zenity"], {}), "qt")

    def test_never_calls_the_real_system(self):
        with patched(FakeRun(error=AssertionError("must not run"))):
            dialogs.choose("auto", {}, which_of("zenity", "kdialog", "notify-send"))
            dialogs.choose("notify", {}, which_of("notify-send"))


if __name__ == "__main__":
    unittest.main()
