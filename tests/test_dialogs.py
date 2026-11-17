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


class FormatTests(unittest.TestCase):
    def test_format_mib(self):
        self.assertEqual(dialogs.format_mib(512), "512 MB")
        self.assertEqual(dialogs.format_mib(1536), "1.5 GB")


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.targets = [target("a&b<c>", "img&<x>", 1536), target("whisper", "w", None)]

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

    def test_content(self):
        for markup in ("html", "pango", "plain"):
            message = dialogs.launch_message(launch("Game"), self.targets, (5120, 16311), markup)
            self.assertIn("GPU Game Guard", message)
            self.assertIn("1.5 GB of GPU memory", message)
            self.assertIn("no GPU memory in use right now", message)
            self.assertIn("by all programs: 5.0 GB of 15.9 GB", message)
            self.assertNotIn("by all programs", dialogs.launch_message(launch("g"), [], None, markup))
            self.assertNotIn("severely bottlenecked", message)

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

    def test_impact_grades(self):
        idle = dialogs.impact_sentence([target(vram=None), target(vram=0)], "plain")
        some = dialogs.impact_sentence([target(vram=1100)], "plain")
        heavy = dialogs.impact_sentence([target(vram=3000), target(vram=1500)], "html")
        self.assertIn("idle", idle)
        self.assertIn("modest", some)
        self.assertIn("<b>4.4 GB</b>", heavy)
        self.assertIn("run out of memory", heavy)
        self.assertNotIn("<b>", dialogs.impact_sentence([target(vram=5000)], "plain"))
        for sentence in (idle, some, heavy):
            self.assertNotIn("bottleneck", sentence)

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

    def test_exit_code_mapping(self):
        self.assertEqual(self.ask(done(0))[0], "stop")
        self.assertEqual(self.ask(done(1, "Launch anyway\n"))[0], "anyway")
        self.assertEqual(self.ask(done(1, ""))[0], "cancel")
        self.assertEqual(self.ask(done(1, "something else\n"))[0], "cancel")
        self.assertEqual(self.ask(done(5))[0], "cancel")
        self.assertEqual(self.ask(done(-15))[0], "cancel")

    def test_command_line(self):
        command = self.ask(done(0))[1].calls[0]
        self.assertEqual(command[:2], ["zenity", "--question"])
        for flag, value in (("--title", "GPU Game Guard"), ("--ok-label", "Stop containers and launch"),
                            ("--cancel-label", "Cancel"), ("--extra-button", "Launch anyway")):
            self.assertEqual(command[command.index(flag) + 1], value)
        text = command[command.index("--text") + 1]
        well_formed(text)
        self.assertIn("a&amp;b&lt;c&gt;", text)

    def test_restart(self):
        for result, expected in ((done(0), True), (done(1), False), (done(1, ""), False), (done(5), None)):
            run = FakeRun(result)
            with patched(run):
                self.assertEqual(dialogs.ZenityPrompter().restart([{"name": "a&b"}]), expected)
        command = run.calls[0]
        self.assertEqual(command[command.index("--ok-label") + 1], "Restart")
        self.assertEqual(command[command.index("--cancel-label") + 1], "Leave stopped")
        well_formed(command[command.index("--text") + 1])

    def test_blocking_questions_have_no_timeout(self):
        self.assertEqual(self.ask(done(0))[1].timeouts, [None])

    def test_oserror_fails_safe(self):
        with patched(FakeRun(error=FileNotFoundError("zenity"))):
            p = dialogs.ZenityPrompter()
            self.assertEqual(p.launch(launch(), [target()], None), "anyway")
            self.assertIsNone(p.restart([{"name": "a"}]))


class KdialogTests(unittest.TestCase):
    def test_launch_mapping(self):
        for code, expected in ((0, "stop"), (1, "anyway"), (2, "cancel"), (-9, "cancel")):
            with patched(FakeRun(done(code))):
                self.assertEqual(dialogs.KdialogPrompter().launch(launch(), [target()], None), expected)

    def test_launch_command(self):
        run = FakeRun()
        with patched(run):
            dialogs.KdialogPrompter().launch(launch(), [target("a&b<c>")], None)
        command = run.calls[0]
        self.assertIn("--yesnocancel", command)
        self.assertEqual(command[command.index("--yes-label") + 1], "Stop containers and launch")
        self.assertEqual(command[command.index("--no-label") + 1], "Launch anyway")
        self.assertEqual(command[command.index("--cancel-label") + 1], "Cancel")
        self.assertIn("a&amp;b&lt;c&gt;", command[-1])
        well_formed(command[-1])

    def test_restart_mapping(self):
        for code, expected in ((0, True), (1, False), (2, None)):
            with patched(FakeRun(done(code))):
                self.assertEqual(dialogs.KdialogPrompter().restart([{"name": "a"}]), expected)

    def test_oserror_fails_safe(self):
        with patched(FakeRun(error=OSError("gone"))):
            p = dialogs.KdialogPrompter()
            self.assertEqual(p.launch(launch(), [target()], None), "anyway")
            self.assertIsNone(p.restart([{"name": "a"}]))

    def test_notify_uses_passivepopup(self):
        run = FakeRun()
        with patched(run):
            dialogs.KdialogPrompter().notify("t", "b")
        self.assertIn("--passivepopup", run.calls[0])


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
            self.assertEqual(p.launch(launch(), [target()], None), "anyway")
            self.assertIsNone(p.restart([{"name": "a"}]))
        self.assertFalse(any("--wait" in c for c in run.calls))

    def test_with_actions_the_choice_is_returned(self):
        for answer, expected in (("stop\n", "stop"), ("anyway\n", "anyway"), ("cancel\n", "cancel"), ("", "anyway")):
            p, run = self.prompter("  -A, --action=[NAME=]Text...", answer)
            with patched(run):
                self.assertEqual(p.launch(launch(), [target()], None), expected)
            ask = run.calls[-1]
            self.assertIn("--wait", ask)
            self.assertIn("--action=stop=Stop containers and launch", ask)
        for answer, expected in (("restart\n", True), ("leave\n", False), ("", None)):
            p, run = self.prompter("--action", answer)
            with patched(run):
                self.assertEqual(p.restart([{"name": "a"}]), expected)

    def test_oserror_fails_safe(self):
        with patched(FakeRun(error=OSError("gone"))):
            p = dialogs.NotifyPrompter(which_of("notify-send"))
            self.assertEqual(p.launch(launch(), [target()], None), "anyway")
            self.assertIsNone(p.restart([{"name": "a"}]))
            p.notify("t", "b")

    def test_support_check_is_cached(self):
        p, run = self.prompter("--action")
        with patched(run):
            p.supports_actions()
            p.supports_actions()
        self.assertEqual(sum("--help" in c for c in run.calls), 1)


class NullTests(unittest.TestCase):
    def test_behaviour(self):
        p = dialogs.NullPrompter()
        self.assertTrue(p.available())
        self.assertEqual(p.launch(launch(), [target()], None), "anyway")
        self.assertIsNone(p.restart([{"name": "a"}]))
        with mock.patch("gpu_game_guard.dialogs.log") as log:
            p.notify("t", "b")
        log.assert_called()


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

    def test_never_calls_the_real_system(self):
        with patched(FakeRun(error=AssertionError("must not run"))):
            dialogs.choose("auto", {}, which_of("zenity", "kdialog", "notify-send"))
            dialogs.choose("notify", {}, which_of("notify-send"))


if __name__ == "__main__":
    unittest.main()
