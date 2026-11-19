from __future__ import annotations

import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from gpu_game_guard.ui import qt, singleton  # noqa: E402

if qt.available():
    from gpu_game_guard.ui import theme, tray


def status(**fields):
    base = dict(state="watching", paused=False, paused_until=None, game=None, stopped=[], config_ok=True)
    base.update(fields)
    return types.SimpleNamespace(**base)


class FakeTray:
    def __init__(self):
        self.icons, self.tooltips, self.menu = [], [], None
        self.activated = types.SimpleNamespace(connect=lambda f: setattr(self, "on_activated", f))

    def setIcon(self, icon):
        self.icons.append(icon)

    def setToolTip(self, text):
        self.tooltips.append(text)

    def setContextMenu(self, menu):
        self.menu = menu

    def show(self):
        raise AssertionError("must not be shown")


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class TrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()
        theme.apply_theme(cls.app, dark=False)

    def make(self, current=None):
        self.current = current
        self.calls = []
        actions = tray.TrayActions(lambda s: self.calls.append(("pause", s)), lambda: self.calls.append("resume"),
                                   lambda: self.calls.append("settings"), lambda: self.calls.append("quit"))
        self.fake = FakeTray()
        self.controller = tray.TrayController(lambda: self.current, actions, self.fake)
        return self.controller

    def test_not_running(self):
        c = self.make(None)
        self.assertEqual(c.state, "off")
        self.assertEqual(c.tooltip, "GPU Game Guard is not running")
        self.assertEqual(c.status_action.text(), "GPU Game Guard is not running")

    def test_state_transitions(self):
        c = self.make(status())
        self.assertEqual(c.state, "ok")
        self.assertEqual(c.tooltip, "GPU Game Guard: watching for games")
        steps = [
            (status(state="game-running", game={"id": "steam:1", "title": "Half-Life"}), "ok",
             "Half-Life is running"),
            (status(state="game-running", game={"title": "Half-Life"}, stopped=[{}, {}]), "ok",
             "Half-Life is running; 2 stopped"),
            (status(state="asking", game={"title": "Half-Life"}), "attention",
             "Waiting for your answer about Half-Life"),
            (status(state="restart-pending", stopped=[{}]), "attention", "1 stopped, waiting to be restarted"),
            (status(state="restart-pending"), "attention", "Waiting to restart what was stopped"),
            (status(paused=True, state="paused"), "paused", "Paused until resumed"),
            (status(paused=True, paused_until=float("inf")), "paused", "Paused until resumed"),
            (status(state="disabled"), "off", "Switched off in the settings"),
            (status(config_ok=False), "attention", "Settings file has a problem"),
            (None, "off", "GPU Game Guard is not running"),
            (status(), "ok", "Watching for games"),
        ]
        for current, state, line in steps:
            self.current = current
            self.assertEqual(c.refresh(), state)
            self.assertEqual(c.status_action.text(), line)
            self.assertTrue(c.tooltip.startswith("GPU Game Guard"))

    def test_paused_until_shows_the_clock_time(self):
        until = time.time() + 3600
        c = self.make(status(paused=True, paused_until=until))
        self.assertEqual(c.status_action.text(), "Paused until " + time.strftime("%H:%M", time.localtime(until)))
        self.assertIn("paused until", c.tooltip)

    def test_icon_changes_only_with_the_state(self):
        c = self.make(status())
        before = len(self.fake.icons)
        c.refresh()
        c.refresh()
        self.assertEqual(len(self.fake.icons), before)
        self.current = None
        c.refresh()
        self.assertEqual(len(self.fake.icons), before + 1)
        self.assertEqual(self.fake.tooltips[-1], "GPU Game Guard is not running")

    def test_menu_items_by_state(self):
        c = self.make(status())
        self.assertEqual(c.menu_summary(), [
            ("Watching for games", False), ("Pause for 1 hour", True), ("Pause until resumed", True),
            ("-", False), ("Open settings", True), ("About GPU Game Guard", True),
            ("Quit", True)])
        self.current = status(paused=True)
        c.refresh()
        self.assertEqual([t for t, _ in c.menu_summary()], [
            "Paused until resumed", "Pause for 1 hour", "Pause until resumed", "Resume", "-", "Open settings",
            "About GPU Game Guard", "Quit"])
        self.assertIs(self.fake.menu, c.menu)

    def test_menu_actions_call_the_callbacks(self):
        c = self.make(status())
        c.pause_hour_action.trigger()
        c.pause_until_action.trigger()
        c.resume_action.trigger()
        with mock.patch("gpu_game_guard.ui.tray.settings_running", return_value=False):
            c.settings_action.trigger()
        c.quit_action.trigger()
        self.assertEqual(self.calls, [("pause", 3600.0), ("pause", None), "resume", "settings", "quit"])

    def test_the_about_entry_sits_before_quit_and_shows_the_dialog(self):
        c = self.make(status())
        entries = [t for t, _ in c.menu_summary()]
        self.assertEqual(entries.index("About GPU Game Guard") + 1, entries.index("Quit"))
        with mock.patch("gpu_game_guard.ui.about.show_about") as show:
            c.about_action.trigger()
        show.assert_called_once_with()
        self.assertEqual(self.calls, [])

    def test_left_click_opens_the_settings(self):
        c = self.make(status())
        with mock.patch("gpu_game_guard.ui.tray.settings_running", return_value=False):
            self.fake.on_activated(qt.QtWidgets.QSystemTrayIcon.Trigger)
            self.fake.on_activated(qt.QtWidgets.QSystemTrayIcon.Context)
        self.assertEqual(self.calls, ["settings"])
        del c

    def test_reader_failure_counts_as_not_running_and_logs_once(self):
        def broken():
            raise RuntimeError("bad file")
        c = self.make(status())
        c._reader = broken
        with mock.patch("gpu_game_guard.ui.tray.log") as log:
            c.refresh()
            c.refresh()
        self.assertEqual(c.state, "off")
        self.assertEqual(log.call_count, 1)

    def test_timer_is_not_started_by_construction(self):
        self.assertFalse(self.make(status())._timer.isActive())

    def test_describe_is_total(self):
        for state in ("watching", "paused", "disabled", "asking", "game-running", "restart-pending", "weird"):
            icon, line, tip = tray.describe(status(state=state))
            self.assertIn(icon, theme.ICON_STATES)
            self.assertTrue(line and tip)

    def test_real_status_overlays_the_pause_file(self):
        from gpu_game_guard import status as status_module
        stored = status_module.Status(paused=False)
        with mock.patch.object(status_module, "read_status", return_value=stored), \
                mock.patch.object(status_module, "paused", return_value=(True, 123.0)):
            result = tray.real_status()
        self.assertTrue(result.paused)
        self.assertEqual(result.paused_until, 123.0)
        with mock.patch.object(status_module, "read_status", return_value=None):
            self.assertIsNone(tray.real_status())

    def test_open_settings_starts_a_detached_process(self):
        with mock.patch("gpu_game_guard.ui.tray.subprocess.Popen") as popen, \
                mock.patch("gpu_game_guard.ui.tray.self_command", return_value=["py", "-m", "gpu_game_guard"]):
            tray.open_settings()
        self.assertEqual(popen.call_args[0][0], ["py", "-m", "gpu_game_guard", "settings"])
        self.assertTrue(popen.call_args[1]["start_new_session"])
        with mock.patch("gpu_game_guard.ui.tray.subprocess.Popen", side_effect=OSError("x")), \
                mock.patch("gpu_game_guard.ui.tray.log") as log:
            tray.open_settings()
        self.assertEqual(log.call_count, 1)

    def test_run_without_a_system_tray_waits_then_returns_zero(self):
        sleeps = []
        with mock.patch.object(qt.QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False), \
                mock.patch("gpu_game_guard.ui.tray.log") as log:
            self.assertEqual(tray.run(sleep=sleeps.append), 0)
        self.assertEqual(sleeps, [30.0] * 20)
        self.assertEqual(log.call_count, 2)

    def test_wait_for_tray_stops_when_the_tray_appears(self):
        answers = iter([False, False, True])
        sleeps = []
        with mock.patch("gpu_game_guard.ui.tray.log"):
            self.assertTrue(tray.wait_for_tray(lambda: next(answers), sleeps.append))
        self.assertEqual(sleeps, [30.0, 30.0])
        self.assertTrue(tray.wait_for_tray(lambda: True, sleeps.append))

    def test_absurd_pause_times_read_as_until_resumed(self):
        for until in (1e300, 2 ** 70, -1e18, float("nan"), float("-inf"), "soon", 253402300800.0, -1.0):
            icon, line, tip = tray.describe(status(paused=True, paused_until=until))
            self.assertEqual((icon, line), ("paused", "Paused until resumed"), until)
            self.assertIn("until resumed", tip)
        with mock.patch("gpu_game_guard.ui.tray.time.localtime", side_effect=OSError("range")):
            self.assertEqual(tray.describe(status(paused=True, paused_until=5.0))[1], "Paused until resumed")

    def test_asking_is_shown_while_the_file_is_valid(self):
        now = time.time()
        fresh = status(state="asking", game={"title": "Doom"}, updated=now - 20, valid_for=60.0)
        self.assertEqual(tray.describe(fresh)[1], "Waiting for your answer about Doom")
        stale = status(state="asking", game={"title": "Doom"}, updated=now - 200, valid_for=60.0)
        self.assertEqual(tray.describe(stale)[1], "Watching for games")
        default = status(state="asking", game={"title": "Doom"}, updated=now - 5)
        self.assertEqual(tray.describe(default)[1], "Waiting for your answer about Doom")

    def test_tooltip_is_set_only_when_it_changes(self):
        c = self.make(status())
        before = len(self.fake.tooltips)
        c.refresh()
        c.refresh()
        self.assertEqual(len(self.fake.tooltips), before)
        self.current = None
        c.refresh()
        self.assertEqual(len(self.fake.tooltips), before + 1)

    def test_hostile_title_is_clean_in_the_menu_and_the_tooltip(self):
        title = "A & B <b>x</b>\u202e\n\x00"
        c = self.make(status(state="asking", game={"title": title}))
        self.assertEqual(c.status_action.text(), "Waiting for your answer about A && B <b>x</b>")
        self.assertEqual(c.menu_summary()[0][0], "Waiting for your answer about A & B <b>x</b>")
        self.assertEqual(self.fake.tooltips[-1],
                         "GPU Game Guard: waiting for your answer (A &amp; B &lt;b&gt;x&lt;/b&gt;)")
        self.assertNotIn("\u202e", c.tooltip)

    def test_the_menu_entry_does_not_start_a_second_settings_window(self):
        c = self.make(status())
        with mock.patch("gpu_game_guard.ui.tray.settings_running", return_value=True):
            c.settings_action.trigger()
        self.assertEqual(self.calls, [])
        with tempfile.TemporaryDirectory() as tmp:
            held = singleton.acquire("settings", Path(tmp))
            real = singleton.acquire
            with mock.patch("gpu_game_guard.ui.tray.singleton.acquire", lambda n: real(n, Path(tmp))):
                c.settings_action.trigger()
                self.assertEqual(self.calls, [])
                held.release()
                c.settings_action.trigger()
                self.assertEqual(self.calls, ["settings"])

    def test_click_does_not_start_a_second_settings_window(self):
        c = self.make(status())
        with tempfile.TemporaryDirectory() as tmp:
            held = singleton.acquire("settings", Path(tmp))
            real = singleton.acquire
            with mock.patch("gpu_game_guard.ui.tray.singleton.acquire", lambda n: real(n, Path(tmp))):
                self.fake.on_activated(qt.QtWidgets.QSystemTrayIcon.Trigger)
                self.assertEqual(self.calls, [])
                held.release()
                self.fake.on_activated(qt.QtWidgets.QSystemTrayIcon.Trigger)
                self.assertEqual(self.calls, ["settings"])
                self.assertIsNotNone(real("settings", Path(tmp)))
        del c


if __name__ == "__main__":
    unittest.main()
