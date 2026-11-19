from __future__ import annotations

import io
import json
import os
import subprocess
import types
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from gpu_game_guard import dialogs  # noqa: E402
from gpu_game_guard.model import Launch, Target  # noqa: E402
from gpu_game_guard.ui import prompt, qt  # noqa: E402

if qt.available():
    QtCore, QtGui, QtWidgets = qt.QtCore, qt.QtGui, qt.QtWidgets

DISPLAY = {"DISPLAY": ":0"}


def target(name="ollama", image="ollama/ollama", vram=6144, kind="container", extra=None):
    return Target(kind, "docker", name, image, vram, extra=extra)


def launch(title="Half-Life"):
    return Launch("steam", (1, "2"), "steam:70", title)


def done(code=0, out=""):
    return subprocess.CompletedProcess([], code, stdout=out)


def payload(kind="launch", remember=True, targets=None):
    return {"kind": kind, "title": "Half-Life", "remember": remember, "totals": [1024, 8192],
            "heavy_vram_mib": 4096,
            "targets": targets if targets is not None else [
                {"kind": "container", "name": "ollama", "image": "ollama/ollama", "vram_mib": 6144, "label": ""}]}


class SelfCommandTests(unittest.TestCase):
    def test_module_run(self):
        main = types.SimpleNamespace(__spec__=types.SimpleNamespace(name="gpu_game_guard.__main__"))
        self.assertEqual(prompt.self_command(main, ["/x/gpu_game_guard/__main__.py"], "/usr/bin/python3"),
                         ["/usr/bin/python3", "-m", "gpu_game_guard"])

    def test_zipapp_run(self):
        for spec in (None, types.SimpleNamespace(name="__main__")):
            main = types.SimpleNamespace(__spec__=spec)
            command = prompt.self_command(main, ["/usr/bin/gpu-game-guard", "tray"], "/usr/bin/python3")
            self.assertEqual(command, ["/usr/bin/python3", "/usr/bin/gpu-game-guard"])

    def test_zipapp_path_is_made_absolute(self):
        main = types.SimpleNamespace(__spec__=None)
        command = prompt.self_command(main, ["gpu-game-guard"], "py")
        self.assertTrue(os.path.isabs(command[1]))

    def test_without_arguments_falls_back_to_module(self):
        main = types.SimpleNamespace(__spec__=None)
        self.assertEqual(prompt.self_command(main, [], "py"), ["py", "-m", "gpu_game_guard"])
        with mock.patch.object(prompt.sys, "argv", []):
            self.assertEqual(prompt.self_command(main, None, "py"), ["py", "-m", "gpu_game_guard"])

    def test_dash_c_and_stdin_fall_back_to_module(self):
        main = types.SimpleNamespace(__spec__=None)
        for first in ("-c", "-", "-m", ""):
            self.assertEqual(prompt.self_command(main, [first, "x"], "py"), ["py", "-m", "gpu_game_guard"], first)


class PayloadTests(unittest.TestCase):
    def test_launch_payload_round_trips_through_json(self):
        service = target("Ollama", "", None, "service", {"unit": "ollama.service"})
        data = prompt.launch_payload(launch(), [target(), service], (100, 200), True, 4096)
        data = json.loads(json.dumps(data))
        self.assertEqual(data["kind"], "launch")
        self.assertEqual(data["title"], "Half-Life")
        self.assertEqual(data["totals"], [100, 200])
        self.assertTrue(data["remember"])
        self.assertEqual(data["heavy_vram_mib"], 4096)
        self.assertEqual(data["targets"][0], {"kind": "container", "name": "ollama", "image": "ollama/ollama",
                                              "vram_mib": 6144, "label": ""})
        self.assertEqual(data["targets"][1]["label"], "ollama.service")
        self.assertIsNone(data["targets"][1]["vram_mib"])
        self.assertIsNone(prompt.launch_payload(launch(), [], None, False, 1)["totals"])

    def test_restart_payload(self):
        data = prompt.restart_payload([{"kind": "service", "name": "Ollama", "unit": "ollama.service"}])
        self.assertEqual(data["kind"], "restart")
        self.assertEqual(data["targets"][0]["name"], "Ollama")
        self.assertEqual(data["targets"][0]["label"], "ollama.service")

    def test_parse_answer(self):
        self.assertEqual(prompt.parse_answer("launch", '{"choice": "stop", "remember": "stop"}'),
                         {"choice": "stop", "remember": "stop"})
        self.assertEqual(prompt.parse_answer("launch", '{"choice": "anyway", "remember": "junk"}'),
                         {"choice": "anyway", "remember": None})
        self.assertEqual(prompt.parse_answer("restart", '{"choice": "later"}'), {"choice": "later"})
        for kind, text in [("launch", "nope"), ("launch", "[]"), ("launch", '{"choice": "restart"}'),
                           ("restart", '{"choice": "stop"}'), ("launch", "{}"), ("launch", "")]:
            self.assertIsNone(prompt.parse_answer(kind, text), (kind, text))


class FakeRun:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result or done(), error, []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error:
            raise self.error
        return self.result


def prompter(run, environ=DISPLAY):
    return prompt.QtPrompter(environ=environ, run=run, command=lambda: ["py", "-m", "gpu_game_guard"])


class PrompterTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("gpu_game_guard.ui.prompt.log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, result=None, error=None):
        run = FakeRun(result, error)
        return prompter(run).launch(launch(), [target()], (1, 2)), run

    def test_launch_runs_the_subprocess_with_the_payload(self):
        choice, run = self.ask(done(0, '{"choice": "stop", "remember": null}'))
        self.assertEqual(choice, "stop")
        command, kwargs = run.calls[0]
        self.assertEqual(command, ["py", "-m", "gpu_game_guard", "prompt", "launch"])
        self.assertEqual(json.loads(kwargs["input"])["title"], "Half-Life")
        self.assertEqual(kwargs["timeout"], 600)

    def test_every_launch_answer(self):
        for choice in ("stop", "anyway", "cancel"):
            self.assertEqual(self.ask(done(0, json.dumps({"choice": choice})))[0], choice)

    def test_launch_fails_safe(self):
        for result, error in [(None, OSError("no python")), (None, subprocess.TimeoutExpired("x", 600)),
                              (done(1, '{"choice": "stop"}'), None), (done(134, ""), None),
                              (done(0, "not json"), None), (done(0, '{"choice": "explode"}'), None),
                              (done(0, ""), None)]:
            self.log.reset_mock()
            self.assertEqual(self.ask(result, error)[0], "anyway", (result, error))
            self.assertEqual(self.log.call_count, 1)

    def test_restart_answers(self):
        refs = [{"kind": "container", "runtime": "docker", "name": "ollama"}]
        for choice, expected in (("restart", True), ("leave", False), ("later", None)):
            p = prompter(FakeRun(done(0, json.dumps({"choice": choice}))))
            self.assertIs(p.restart(refs), expected)

    def test_restart_fails_safe(self):
        refs = [{"kind": "container", "runtime": "docker", "name": "ollama"}]
        for result, error in [(None, OSError()), (done(2, ""), None), (done(0, "x"), None),
                              (done(0, '{"choice": "stop"}'), None)]:
            self.assertIsNone(prompter(FakeRun(result, error)).restart(refs))
        run = FakeRun()
        self.assertIsNone(prompter(run).restart([{"kind": "container", "runtime": "docker"}]))
        self.assertEqual(run.calls, [])

    def test_remember_semantics(self):
        cases = [("stop", "stop", "stop"), ("anyway", "ignore", "ignore"), ("stop", "ignore", None),
                 ("anyway", "stop", None), ("cancel", "stop", None), ("cancel", "ignore", None),
                 ("stop", None, None)]
        for choice, remember, expected in cases:
            p = prompter(FakeRun(done(0, json.dumps({"choice": choice, "remember": remember}))))
            p.launch(launch(), [target()], None)
            self.assertEqual(p.consume_remember(), expected, (choice, remember))
            self.assertIsNone(p.consume_remember())

    def test_remember_is_cleared_by_the_next_launch_and_by_failures(self):
        p = prompter(FakeRun(done(0, '{"choice": "stop", "remember": "stop"}')))
        p.launch(launch(), [target()], None)
        p._run = FakeRun(error=OSError())
        p.launch(launch(), [target()], None)
        self.assertIsNone(p.consume_remember())

    def test_payload_follows_module_defaults(self):
        run = FakeRun(done(0, '{"choice": "anyway"}'))
        with mock.patch.object(dialogs, "REMEMBER_CHOICES", False), mock.patch.object(dialogs, "HEAVY_VRAM_MIB", 999):
            prompter(run).launch(launch(), [target()], None)
        sent = json.loads(run.calls[0][1]["input"])
        self.assertFalse(sent["remember"])
        self.assertEqual(sent["heavy_vram_mib"], 999)

    def test_can_ask_needs_qt_and_a_display(self):
        with mock.patch("gpu_game_guard.ui.prompt.qt.available", return_value=True):
            self.assertTrue(prompter(FakeRun()).can_ask)
            self.assertTrue(prompter(FakeRun(), {"WAYLAND_DISPLAY": "wayland-0"}).can_ask)
            self.assertFalse(prompter(FakeRun(), {}).can_ask)
            self.assertFalse(prompter(FakeRun(), {}).available())
        with mock.patch("gpu_game_guard.ui.prompt.qt.available", return_value=False):
            self.assertFalse(prompter(FakeRun()).can_ask)

    def test_notify_uses_notify_send_only_when_present(self):
        run = FakeRun()
        p = prompt.QtPrompter(environ=DISPLAY, which=lambda c: None, run=run)
        with mock.patch("gpu_game_guard.dialogs.log"), mock.patch("gpu_game_guard.dialogs.subprocess.run") as sent:
            p.notify("t", "b")
        sent.assert_not_called()
        self.assertEqual(run.calls, [])


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()
        from gpu_game_guard.ui import theme
        theme.apply_theme(cls.app, dark=False)

    def run_dialog(self, data, act):
        out = io.StringIO()
        with mock.patch.object(prompt, "test_platform", "offscreen"):
            code = prompt.run_prompt([data["kind"]], io.StringIO(json.dumps(data)), out, execute=act,
                                     environ={"QT_QPA_PLATFORM": "offscreen"})
        return code, (json.loads(out.getvalue()) if out.getvalue() else None)

    def press(self, dialog, key):
        for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
            QtWidgets.QApplication.sendEvent(dialog, QtGui.QKeyEvent(kind, key, QtCore.Qt.NoModifier))

    def test_launch_buttons(self):
        for button, expected in (("stop", "stop"), ("anyway", "anyway"), ("cancel", "cancel")):
            code, answer = self.run_dialog(payload(), lambda d, b=button: d.buttons[b].click())
            self.assertEqual((code, answer), (0, {"choice": expected, "remember": None}))

    def test_window_close_and_escape_mean_cancel(self):
        for act in (lambda d: d.close(), lambda d: self.press(d, QtCore.Qt.Key_Escape), lambda d: d.reject()):
            self.assertEqual(self.run_dialog(payload(), act), (0, {"choice": "cancel", "remember": None}))

    def test_primary_button_is_stop_and_default(self):
        dialog = prompt.build_dialog(payload())
        self.assertEqual(dialog.buttons["stop"].text(), "&Stop and launch")
        self.assertTrue(dialog.buttons["stop"].isDefault())
        self.assertEqual(dialog.buttons["stop"].property("role"), "primary")
        self.assertEqual(dialog.buttons["anyway"].text(), "Launch &anyway")
        self.assertEqual(dialog.buttons["cancel"].text(), "&Cancel")

    def test_remember_checkbox(self):
        def tick_then(button):
            def act(dialog):
                dialog.remember_box.setChecked(True)
                dialog.buttons[button].click()
            return act
        self.assertEqual(self.run_dialog(payload(), tick_then("stop"))[1], {"choice": "stop", "remember": "stop"})
        self.assertEqual(self.run_dialog(payload(), tick_then("anyway"))[1],
                         {"choice": "anyway", "remember": "ignore"})
        self.assertEqual(self.run_dialog(payload(), tick_then("cancel"))[1], {"choice": "cancel", "remember": None})
        self.assertEqual(self.run_dialog(payload(), lambda d: d.buttons["stop"].click())[1]["remember"], None)

    def test_checkbox_only_when_the_payload_asks(self):
        self.assertIsNotNone(prompt.build_dialog(payload(remember=True)).remember_box)
        self.assertIsNone(prompt.build_dialog(payload(remember=False)).remember_box)

    def test_dialog_names_the_game_and_the_targets(self):
        dialog = prompt.build_dialog(payload())
        texts = [w.text() for w in dialog.findChildren(QtWidgets.QLabel)]
        self.assertTrue(any("Half-Life" in t for t in texts))
        self.assertTrue(any("ollama/ollama" in t and "6.0 GiB" in t for t in texts))
        self.assertTrue(any("competes for GPU time" in t for t in texts))
        self.assertTrue(any("1.0 GiB of 8.0 GiB" in t for t in texts))

    def test_the_sentence_uses_the_kinds_and_agrees_in_number(self):
        def sentence(*kinds):
            targets = [{"kind": k, "name": f"n{i}", "image": "", "vram_mib": None, "label": ""}
                       for i, k in enumerate(kinds)]
            texts = [w.text() for w in prompt.build_dialog(payload(targets=targets)).findChildren(QtWidgets.QLabel)]
            return " ".join(t for t in texts if t.startswith("GPU Game Guard paused"))
        self.assertEqual(sentence("container"),
                         "GPU Game Guard paused this launch because this container has access to the GPU.")
        self.assertIn("because these services have access", sentence("service", "service"))
        self.assertIn("because these containers or services have access", sentence("container", "service"))
        self.assertNotIn("using the GPU", sentence("container", "service"))

    def test_unknown_memory_is_said_plainly(self):
        data = payload(targets=[{"kind": "container", "name": "a", "image": "", "vram_mib": None, "label": ""}])
        texts = [w.text() for w in prompt.build_dialog(data).findChildren(QtWidgets.QLabel)]
        self.assertTrue(any("GPU memory use unknown" in t for t in texts))

    def test_heavy_threshold_comes_from_the_payload(self):
        data = payload()
        data["heavy_vram_mib"] = 10000
        texts = [w.text() for w in prompt.build_dialog(data).findChildren(QtWidgets.QLabel)]
        self.assertTrue(any("probably modest" in t for t in texts))
        self.assertEqual(dialogs.HEAVY_VRAM_MIB, 4096)

    def test_hostile_text_is_shown_literally(self):
        data = payload(targets=[{"kind": "container", "name": "<b>x</b>\x00", "image": "", "vram_mib": 1,
                                 "label": ""}])
        dialog = prompt.build_dialog(data)
        names = [w.text() for w in dialog.findChildren(QtWidgets.QLabel)]
        self.assertIn("<b>x</b>", names)

    def test_the_target_list_is_a_scroll_area(self):
        dialog = prompt.build_dialog(payload())
        self.assertIs(dialog.list_area.widget().parent().parent(), dialog.list_area)
        self.assertEqual(dialog.list_area.verticalScrollBarPolicy(), QtCore.Qt.ScrollBarAsNeeded)

    def test_restart_buttons(self):
        data = payload("restart", False)
        for button, expected in (("restart", "restart"), ("later", "later"), ("leave", "leave")):
            code, answer = self.run_dialog(data, lambda d, b=button: d.buttons[b].click())
            self.assertEqual((code, answer), (0, {"choice": expected}))

    def test_restart_close_and_escape_mean_later(self):
        data = payload("restart", False)
        for act in (lambda d: d.close(), lambda d: self.press(d, QtCore.Qt.Key_Escape)):
            self.assertEqual(self.run_dialog(data, act), (0, {"choice": "later"}))

    def test_restart_labels(self):
        dialog = prompt.build_dialog(payload("restart", False))
        self.assertEqual([dialog.buttons[k].text() for k in ("restart", "later", "leave")],
                         ["&Restart", "Ask me &later", "Leave &stopped"])
        self.assertTrue(dialog.buttons["restart"].isDefault())
        self.assertIsNone(dialog.remember_box)

    def test_answer_is_accepted_by_the_parent_parser(self):
        _, answer = self.run_dialog(payload(), lambda d: d.buttons["stop"].click())
        self.assertEqual(prompt.parse_answer("launch", json.dumps(answer)), answer)


def many(count):
    return [{"kind": "process", "name": f"p{i}", "image": "", "vram_mib": i, "label": ""} for i in range(count)]


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class HostileTitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()
        from gpu_game_guard.ui import theme
        theme.apply_theme(cls.app, dark=False)
        cls.theme = theme

    def test_title_is_literal_and_nothing_is_rich_text(self):
        hostile = '<img src="file:///etc/passwd"><a href="file:///etc/passwd">x</a><b>B</b>'
        data = payload(targets=[{"kind": "container", "name": hostile, "image": hostile, "vram_mib": 1,
                                 "label": hostile}])
        data["title"] = hostile
        dialog = prompt.build_dialog(data)
        labels = dialog.findChildren(QtWidgets.QLabel)
        self.assertEqual(dialog.findChild(QtWidgets.QLabel, "Headline").text(), "Launching " + hostile)
        self.assertTrue(any(w.text() == hostile for w in labels))
        for label in labels:
            self.assertNotEqual(label.textFormat(), QtCore.Qt.RichText, label.text())
            self.assertNotEqual(label.textFormat(), QtCore.Qt.AutoText, label.text())
        for label in labels:
            self.assertFalse(label.openExternalLinks())
            if label.toolTip():
                self.assertNotIn("<img", label.toolTip())
        self.assertFalse(dialog.grab().isNull())

    def test_control_characters_never_reach_a_label(self):
        data = payload(targets=[{"kind": "container", "name": "a\nb\tc\u202ed\x00", "image": "i\r\nj",
                                 "vram_mib": 1, "label": ""}])
        data["title"] = "x\ny\u202e"
        dialog = prompt.build_dialog(data)
        for label in dialog.findChildren(QtWidgets.QLabel):
            for char in ("\n", "\t", "\u202e", "\x00", "\r"):
                self.assertNotIn(char, label.text())
        self.assertEqual(dialog.findChild(QtWidgets.QLabel, "TargetName").text(), "a b cd")

    def test_unbreakable_title_does_not_widen_the_dialog(self):
        data = payload()
        data["title"] = "W" * 400
        dialog = prompt.build_dialog(data)
        dialog.place_in(QtCore.QRect(0, 0, 3000, 2000))
        self.assertLessEqual(dialog.width(), int(prompt.MAX_WIDTH * self.theme.font_scale()))
        self.assertLessEqual(dialog.sizeHint().width(), dialog.maximumWidth())
        headline = dialog.findChild(QtWidgets.QLabel, "Headline")
        self.assertIn("…", headline.text())
        self.assertIn("W" * 400, headline.toolTip())
        self.assertLessEqual(headline.sizeHint().width(), dialog.maximumWidth())

    def test_long_title_with_spaces_is_capped_and_wraps(self):
        data = payload()
        data["title"] = "word " * 100
        dialog = prompt.build_dialog(data)
        text = dialog.findChild(QtWidgets.QLabel, "Headline").text()
        self.assertLessEqual(len(text), len("Launching ") + prompt.HEADLINE_TITLE_LIMIT)


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class WindowBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()
        from gpu_game_guard.ui import theme
        theme.apply_theme(cls.app, dark=False)

    def press(self, dialog, key):
        QtWidgets.QApplication.sendEvent(dialog.focusWidget() or dialog,
                                         QtGui.QKeyEvent(QtCore.QEvent.KeyPress, key, QtCore.Qt.NoModifier))

    @staticmethod
    def on_screen(dialog, area):
        """Make the dialog treat `area` as the screen under the pointer."""
        dialog.target_screen = lambda: types.SimpleNamespace(availableGeometry=lambda: area)

    def test_many_targets_are_capped_to_the_screen_and_scroll(self):
        dialog = prompt.build_dialog(payload(targets=many(40)))
        self.on_screen(dialog, QtCore.QRect(0, 0, 1280, 720))
        dialog.show()
        self.app.processEvents()
        self.assertLessEqual(dialog.height(), int(720 * 0.8))
        self.assertLessEqual(dialog.maximumHeight(), int(720 * 0.8))
        bar = dialog.list_area.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        dialog.close()

    def test_few_targets_do_not_scroll(self):
        dialog = prompt.build_dialog(payload(targets=many(2)))
        self.on_screen(dialog, QtCore.QRect(0, 0, 1920, 1080))
        dialog.show()
        self.app.processEvents()
        self.assertEqual(dialog.list_area.verticalScrollBar().maximum(), 0)
        dialog.close()

    def test_window_is_placed_fully_inside_the_available_area(self):
        for area in (QtCore.QRect(0, 0, 1920, 1080), QtCore.QRect(1920, 40, 1280, 700),
                     QtCore.QRect(-1600, 30, 1600, 870), QtCore.QRect(0, 0, 700, 520)):
            dialog = prompt.build_dialog(payload(targets=many(6)))
            dialog.place_in(area)
            frame = dialog.frameGeometry()
            self.assertTrue(area.contains(frame.topLeft()), (area, frame))
            self.assertTrue(area.contains(frame.bottomRight()), (area, frame))
        self.assertLessEqual(frame.width(), 700)

    def test_the_screen_under_the_pointer_is_chosen(self):
        dialog = prompt.build_dialog(payload())
        with mock.patch.object(QtWidgets.QApplication, "screenAt", return_value=None):
            self.assertIs(dialog.target_screen(), QtWidgets.QApplication.primaryScreen())
        other = object()
        with mock.patch.object(QtWidgets.QApplication, "screenAt", return_value=other):
            self.assertIs(dialog.target_screen(), other)

    def test_initial_focus_is_the_stop_button(self):
        for kind, button in (("launch", "stop"), ("restart", "restart")):
            dialog = prompt.build_dialog(payload(kind, kind == "launch"))
            dialog.show()
            self.app.processEvents()
            self.assertIs(dialog.focusWidget(), dialog.buttons[button], kind)
            dialog.close()

    def test_enter_is_ignored_for_the_first_700_ms(self):
        now = [100.0]
        dialog = prompt.build_dialog(payload(), clock=lambda: now[0])
        self.press(dialog, QtCore.Qt.Key_Return)  # not shown yet
        self.assertEqual(dialog.choice, "cancel")
        dialog.show()
        for key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            now[0] = 100.0 + 0.5
            self.press(dialog, key)
            self.assertEqual(dialog.choice, "cancel")
            self.assertTrue(dialog.isVisible())
        now[0] = 100.0 + 0.8
        self.assertTrue(dialog.enter_allowed())
        self.press(dialog, QtCore.Qt.Key_Return)
        self.assertEqual(dialog.choice, "stop")
        dialog.close()

    def test_space_is_ignored_for_the_first_700_ms_on_every_focusable_control(self):
        now = [10.0]
        dialog = prompt.build_dialog(payload(), clock=lambda: now[0])
        dialog.show()
        controls = [*dialog.buttons.values(), dialog.remember_box]
        for control in controls:
            control.setFocus()
            for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
                QtWidgets.QApplication.sendEvent(
                    control, QtGui.QKeyEvent(kind, QtCore.Qt.Key_Space, QtCore.Qt.NoModifier, " "))
            self.assertEqual(dialog.choice, "cancel")
            self.assertTrue(dialog.isVisible())
        self.assertFalse(dialog.remember_box.isChecked())
        now[0] = 10.8
        dialog.remember_box.setFocus()
        for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
            QtWidgets.QApplication.sendEvent(
                dialog.remember_box, QtGui.QKeyEvent(kind, QtCore.Qt.Key_Space, QtCore.Qt.NoModifier, " "))
        self.assertTrue(dialog.remember_box.isChecked())
        dialog.buttons["anyway"].setFocus()
        for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
            QtWidgets.QApplication.sendEvent(
                dialog.buttons["anyway"], QtGui.QKeyEvent(kind, QtCore.Qt.Key_Space, QtCore.Qt.NoModifier, " "))
        self.assertEqual(dialog.choice, "anyway")
        dialog.close()

    def test_mouse_clicks_work_during_the_guard(self):
        dialog = prompt.build_dialog(payload(), clock=lambda: 0.0)
        dialog.show()
        QtTest = __import__("PyQt5.QtTest", fromlist=["QTest"])
        QtTest.QTest.mouseClick(dialog.remember_box, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
                                QtCore.QPoint(8, dialog.remember_box.height() // 2))
        self.assertTrue(dialog.remember_box.isChecked())
        QtTest.QTest.mouseClick(dialog.buttons["anyway"], QtCore.Qt.LeftButton)
        self.assertEqual(dialog.choice, "anyway")
        dialog.close()

    def test_long_headline_does_not_clip_on_a_small_screen(self):
        from gpu_game_guard.ui import theme
        original = self.app.font()
        try:
            font = QtGui.QFont(original)
            font.setPointSizeF(16)
            self.app.setFont(font)
            theme.apply_theme(self.app, dark=False)
            data = payload(targets=many(6))
            data["title"] = " ".join(["Adventure"] * 14)[:120]
            dialog = prompt.build_dialog(data)
            dialog.place_in(QtCore.QRect(0, 0, 1366, 728))
            needed = dialog.layout().totalHeightForWidth(dialog.width())
            headline = dialog.findChild(QtWidgets.QLabel, "Headline")
            self.assertGreater(headline.heightForWidth(headline.width()), 3 * headline.fontMetrics().height())
            self.assertGreaterEqual(dialog.height(), needed)
            self.assertLessEqual(dialog.height(), 728)
            self.assertGreaterEqual(headline.height(), headline.heightForWidth(headline.width()))
        finally:
            self.app.setFont(original)
            theme.apply_theme(self.app, dark=False)

    def test_escape_works_immediately(self):
        dialog = prompt.build_dialog(payload(), clock=lambda: 0.0)
        dialog.show()
        self.press(dialog, QtCore.Qt.Key_Escape)
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.choice, "cancel")

    def test_enter_in_the_checkbox_is_ignored_too(self):
        now = [0.0]
        dialog = prompt.build_dialog(payload(), clock=lambda: now[0])
        dialog.show()
        dialog.remember_box.setFocus()
        self.press(dialog, QtCore.Qt.Key_Return)
        self.assertEqual(dialog.choice, "cancel")
        dialog.close()

    def test_every_button_has_a_distinct_mnemonic(self):
        for kind in ("launch", "restart"):
            dialog = prompt.build_dialog(payload(kind, kind == "launch"))
            widgets = list(dialog.buttons.values()) + ([dialog.remember_box] if dialog.remember_box else [])
            keys = [w.text().split("&")[1][0].lower() for w in widgets]
            self.assertEqual(len(keys), len(set(keys)), kind)

    def test_dialog_renders_at_a_larger_font_in_both_schemes(self):
        from gpu_game_guard.ui import theme
        original = self.app.font()
        try:
            font = QtGui.QFont(original)
            font.setPointSizeF(16)
            self.app.setFont(font)
            for dark in (False, True):
                theme.apply_theme(self.app, dark=dark)
                dialog = prompt.build_dialog(payload(targets=many(3)))
                dialog.place_in(QtCore.QRect(0, 0, 1920, 1080))
                self.assertFalse(dialog.grab().isNull())
                self.assertLessEqual(dialog.width(), int(prompt.MAX_WIDTH * theme.font_scale()))
                self.assertGreater(dialog.minimumWidth(), prompt.MIN_WIDTH)
        finally:
            self.app.setFont(original)
            theme.apply_theme(self.app, dark=False)


class PlatformTests(unittest.TestCase):
    def test_wayland_is_preferred_when_nothing_is_chosen(self):
        self.assertEqual(prompt.platform_changes({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}),
                         {"QT_QPA_PLATFORM": "wayland;xcb"})
        self.assertEqual(prompt.platform_changes({"DISPLAY": ":0"}), {})
        self.assertEqual(prompt.platform_changes({}), {})

    def test_an_explicit_real_platform_is_kept(self):
        for value in ("xcb", "wayland", "wayland;xcb", "eglfs", "linuxfb"):
            self.assertEqual(prompt.platform_changes({"QT_QPA_PLATFORM": value, "WAYLAND_DISPLAY": "w"}), {})

    def test_inherited_headless_platforms_are_removed(self):
        for value in ("offscreen", "minimal", "minimalegl", "vnc", "Offscreen", "vnc:size=800x600"):
            self.assertEqual(prompt.platform_changes({"QT_QPA_PLATFORM": value, "DISPLAY": ":0"}),
                             {"QT_QPA_PLATFORM": None}, value)
        self.assertEqual(prompt.platform_changes({"QT_QPA_PLATFORM": "offscreen", "WAYLAND_DISPLAY": "w"}),
                         {"QT_QPA_PLATFORM": "wayland;xcb"})

    def test_effective_environment_and_the_test_hook(self):
        env = {"QT_QPA_PLATFORM": "offscreen", "DISPLAY": ":0"}
        self.assertNotIn("QT_QPA_PLATFORM", prompt.effective_environment(env))
        self.assertEqual(env["QT_QPA_PLATFORM"], "offscreen")
        with mock.patch.object(prompt, "test_platform", "offscreen"):
            self.assertEqual(prompt.effective_environment({})["QT_QPA_PLATFORM"], "offscreen")

    def test_run_prompt_without_a_display_and_only_a_headless_platform_fails(self):
        out = io.StringIO()
        with mock.patch("gpu_game_guard.ui.prompt.qt.available", return_value=True), \
                mock.patch("gpu_game_guard.ui.prompt.log"):
            code = prompt.run_prompt(["launch"], io.StringIO("{}"), out, environ={"QT_QPA_PLATFORM": "offscreen"})
        self.assertEqual((code, out.getvalue()), (1, ""))

    def test_run_prompt_applies_the_choice_to_the_real_environment(self):
        fake = {"QT_QPA_PLATFORM": "offscreen", "WAYLAND_DISPLAY": "w"}
        with mock.patch.object(prompt.os, "environ", fake), \
                mock.patch("gpu_game_guard.ui.prompt.qt.available", return_value=True), \
                mock.patch("gpu_game_guard.ui.prompt.log"):
            code = prompt.run_prompt(["launch"], io.StringIO("not json"), io.StringIO(), environ=fake)
        self.assertEqual(code, 2)
        self.assertEqual(fake["QT_QPA_PLATFORM"], "wayland;xcb")


class RunPromptFailureTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("gpu_game_guard.ui.prompt.log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def run_it(self, argv, text="{}", environ=None, **kwargs):
        out = io.StringIO()
        with mock.patch.object(prompt, "test_platform", "offscreen" if environ else None):
            code = prompt.run_prompt(argv, io.StringIO(text), out, environ=environ if environ is not None else {},
                                     **kwargs)
        return code, out.getvalue()

    def test_bad_usage(self):
        self.assertEqual(self.run_it([]), (2, ""))
        self.assertEqual(self.run_it(["bogus"]), (2, ""))

    def test_no_display_exits_non_zero_without_output(self):
        with mock.patch("gpu_game_guard.ui.prompt.qt.available", return_value=True):
            self.assertEqual(self.run_it(["launch"]), (1, ""))

    def test_missing_qt_exits_non_zero(self):
        with mock.patch("gpu_game_guard.ui.prompt.qt.available", return_value=False):
            self.assertEqual(self.run_it(["launch"], environ=DISPLAY), (1, ""))

    @unittest.skipUnless(qt.available(), "PyQt5 is not installed")
    def test_unreadable_payload(self):
        env = {"QT_QPA_PLATFORM": "offscreen"}
        self.assertEqual(self.run_it(["launch"], "not json", env), (2, ""))
        self.assertEqual(self.run_it(["launch"], "[1]", env), (2, ""))

    @unittest.skipUnless(qt.available(), "PyQt5 is not installed")
    def test_dialog_error_exits_non_zero(self):
        def boom(dialog):
            raise RuntimeError("x")
        env = {"QT_QPA_PLATFORM": "offscreen"}
        self.assertEqual(self.run_it(["launch"], json.dumps(payload()), env, execute=boom), (1, ""))


if __name__ == "__main__":
    unittest.main()
