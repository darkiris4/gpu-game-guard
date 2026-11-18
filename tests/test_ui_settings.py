from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import List

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from gpu_game_guard import config as configmod  # noqa: E402
from gpu_game_guard import status as statusmod  # noqa: E402
from gpu_game_guard.config import Config, TargetSpec  # noqa: E402
from gpu_game_guard.discovery import Candidate  # noqa: E402
from gpu_game_guard.model import Target  # noqa: E402
from gpu_game_guard.ui import qt  # noqa: E402

if qt.available():
    from gpu_game_guard.ui import settings, settings_model, theme
    from gpu_game_guard.ui.settings_model import SettingsModel

    QtCore, QtGui, QtWidgets = qt.QtCore, qt.QtGui, qt.QtWidgets


def candidate(name="ollama", kind="container", vram=2048) -> "Candidate":
    return Candidate(f"{kind}:{name}", kind, name, name, "", vram, True, "low", None, "")


class Runner:
    def __init__(self, code=0, output=""):
        self.code, self.output, self.calls = code, output, []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.code, self.output


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "config.json"
        self.status_dir = self.dir / "state"
        self.runner = Runner()
        self.found: List[Candidate] = [candidate()]
        self.discoveries = 0

    def discover(self, config):
        self.discoveries += 1
        return list(self.found)

    def make(self) -> "SettingsModel":
        return SettingsModel(self.path, self.status_dir, discover=self.discover, runner=self.runner)


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class ModelTests(Base):
    def test_starts_clean_from_a_missing_file(self):
        model = self.make()
        self.assertFalse(model.dirty)
        self.assertEqual(model.problems, [])
        self.assertEqual(model.load_error, "")
        self.assertEqual(model.config, Config())

    def test_update_marks_dirty_and_emits(self):
        model = self.make()
        emitted = []
        model.changed.connect(lambda: emitted.append(1))
        model.update(on_launch="stop")
        self.assertTrue(model.dirty)
        self.assertEqual(model.config.on_launch, "stop")
        self.assertEqual(model.saved.on_launch, "ask")
        self.assertEqual(len(emitted), 1)

    def test_changing_back_is_clean_again(self):
        model = self.make()
        model.update(on_launch="stop")
        model.update(on_launch="ask")
        self.assertFalse(model.dirty)

    def test_working_copy_is_replaced_not_mutated(self):
        model = self.make()
        before = model.config
        model.update(ignore_containers=["a"])
        self.assertIsNot(model.config, before)
        self.assertEqual(before.ignore_containers, [])
        self.assertEqual(model.saved.ignore_containers, [])

    def test_set_config_copies(self):
        model = self.make()
        cfg = Config(after_game="leave")
        model.set_config(cfg)
        cfg.after_game = "restart"
        self.assertEqual(model.config.after_game, "leave")

    def test_problems_follow_changes(self):
        model = self.make()
        model.update(heavy_vram_mib=-1)
        self.assertTrue(any("heavy_vram_mib" in p for p in model.problems))
        model.update(heavy_vram_mib=100)
        self.assertEqual(model.problems, [])

    def test_apply_writes_only_when_valid(self):
        model = self.make()
        model.update(poll_seconds=0.0001)
        problems = model.apply()
        self.assertTrue(problems)
        self.assertFalse(self.path.exists())
        self.assertTrue(model.dirty)
        model.update(poll_seconds=2.0)
        self.assertEqual(model.apply(), [])
        self.assertFalse(model.dirty)
        self.assertEqual(configmod.load(self.path).poll_seconds, 2.0)
        self.assertEqual(model.saved.poll_seconds, 2.0)

    def test_apply_reports_a_write_failure(self):
        model = self.make()
        blocker = self.dir / "blocker"
        blocker.write_text("x")
        model.path = blocker / "config.json"
        model.update(on_launch="stop")
        problems = model.apply()
        self.assertEqual(len(problems), 1)
        self.assertIn("could not be saved", problems[0])
        self.assertTrue(model.dirty)

    def test_revert(self):
        model = self.make()
        model.update(on_launch="ignore")
        model.set_game("steam:1", "One", "stop")
        model.revert()
        self.assertFalse(model.dirty)
        self.assertEqual(model.config, Config())

    def test_broken_file_blocks_apply_until_defaults_are_accepted(self):
        self.path.write_text("{ not json")
        model = self.make()
        self.assertIn("not valid JSON", model.load_error)
        self.assertEqual(model.config, Config())
        model.update(on_launch="stop")
        problems = model.apply()
        self.assertIn("not valid JSON", problems[0])
        self.assertEqual(self.path.read_text(), "{ not json")
        model.accept_defaults()
        self.assertEqual(model.load_error, "")
        self.assertTrue(model.dirty)
        self.assertEqual(model.apply(), [])
        self.assertEqual(configmod.load(self.path).on_launch, "stop")
        self.assertFalse(model.dirty)

    def test_invalid_values_in_the_file_count_as_a_broken_file(self):
        self.path.write_text(json.dumps({"on_launch": "sometimes"}))
        model = self.make()
        self.assertIn("on_launch", model.load_error)

    def test_revert_after_accepting_defaults_is_clean(self):
        self.path.write_text("[")
        model = self.make()
        model.accept_defaults()
        model.revert()
        self.assertFalse(model.dirty)

    def test_target_helpers(self):
        model = self.make()
        spec = TargetSpec(id="ollama", kind="service", label="Ollama", unit="ollama.service")
        model.set_target(spec)
        self.assertEqual([t.id for t in model.config.targets], ["ollama"])
        self.assertEqual(model.saved.targets, [])
        model.set_target(TargetSpec(id="ollama", kind="service", label="Renamed", unit="ollama.service"))
        self.assertEqual([t.label for t in model.config.targets], ["Renamed"])
        model.remove_target("ollama")
        self.assertEqual(model.config.targets, [])
        self.assertFalse(model.dirty)

    def test_game_helpers(self):
        model = self.make()
        model.set_game("steam:730", "Counter-Strike 2", "stop")
        self.assertEqual(model.effective_action_for_game("steam:730"), "stop")
        self.assertEqual(model.effective_action_for_game("steam:1"), "ask")
        model.update(on_launch="ignore")
        self.assertEqual(model.effective_action_for_game("steam:1"), "ignore")
        model.remove_game("steam:730")
        self.assertEqual(model.effective_action_for_game("steam:730"), "ignore")
        model.remove_game("steam:missing")
        self.assertEqual(model.config.games, {})

    def test_game_helper_rejects_a_bad_action(self):
        with self.assertRaises(ValueError):
            self.make().set_game("steam:1", "x", "explode")

    def test_restart_needed_only_for_saved_restart_settings(self):
        model = self.make()
        self.assertFalse(model.restart_needed())
        model.update(runtimes=["docker"])
        self.assertFalse(model.restart_needed())  # not saved yet
        model.apply()
        self.assertTrue(model.restart_needed())
        model.update(on_launch="stop")
        model.apply()
        self.assertTrue(model.restart_needed())

    def test_restart_not_needed_for_other_settings(self):
        model = self.make()
        model.update(on_launch="stop", after_game="leave", ignore_containers=["x"])
        model.apply()
        self.assertFalse(model.restart_needed())

    def test_triggers_and_dialog_need_a_restart(self):
        for field, value in (("triggers", []), ("dialog", "none")):
            model = self.make()
            model.update(**{field: value})
            model.apply()
            self.assertTrue(model.restart_needed(), field)

    def test_changing_back_removes_the_restart_need(self):
        model = self.make()
        model.update(dialog="none")
        model.apply()
        model.update(dialog="auto")
        model.apply()
        self.assertFalse(model.restart_needed())

    def test_restart_service_runs_systemctl_and_clears_the_need(self):
        model = self.make()
        model.update(dialog="none")
        model.apply()
        ok, message = model.restart_service()
        self.assertTrue(ok)
        self.assertTrue(message)
        self.assertEqual(self.runner.calls, [["systemctl", "--user", "restart", "gpu-game-guard.service"]])
        self.assertFalse(model.restart_needed())

    def test_failed_restart_keeps_the_need_and_reports_output(self):
        self.runner.code, self.runner.output = 5, "Unit not found."
        model = self.make()
        model.update(dialog="none")
        model.apply()
        ok, message = model.restart_service()
        self.assertFalse(ok)
        self.assertEqual(message, "Unit not found.")
        self.assertTrue(model.restart_needed())

    def test_runner_exception_is_a_failure(self):
        def boom(argv):
            raise OSError("no systemctl")

        model = SettingsModel(self.path, self.status_dir, discover=self.discover, runner=boom)
        ok, message = model.restart_service()
        self.assertFalse(ok)
        self.assertIn("no systemctl", message)

    def test_pause_and_resume_use_the_status_dir(self):
        model = self.make()
        self.assertEqual(model.paused(), (False, None))
        model.pause(3600)
        is_paused, until = model.paused()
        self.assertTrue(is_paused)
        self.assertGreater(until, time.time() + 3000)
        self.assertTrue((self.status_dir / statusmod.PAUSE_FILE).exists())
        model.resume()
        self.assertEqual(model.paused(), (False, None))
        model.pause()
        self.assertEqual(model.paused(), (True, None))

    def test_status_and_events_come_from_the_status_dir(self):
        model = self.make()
        self.assertIsNone(model.status())
        self.assertEqual(model.events(), [])
        statusmod.write_status(statusmod.Status(updated=time.time(), state="watching"), self.status_dir)
        statusmod.append_event("config", "hello", self.status_dir)
        self.assertEqual(model.status().state, "watching")
        self.assertEqual([e["message"] for e in model.events()], ["hello"])

    def test_candidates_are_cached_until_refreshed(self):
        model = self.make()
        self.assertEqual([c.name for c in model.candidates()], ["ollama"])
        model.candidates()
        self.assertEqual(self.discoveries, 1)
        self.found = [candidate("comfy")]
        self.assertEqual([c.name for c in model.candidates()], ["ollama"])
        self.assertEqual([c.name for c in model.candidates(refresh=True)], ["comfy"])
        self.assertEqual(self.discoveries, 2)

    def test_a_failing_discovery_gives_an_empty_list(self):
        def broken(config):
            raise RuntimeError("docker is down")

        model = SettingsModel(self.path, self.status_dir, discover=broken, runner=self.runner)
        self.assertEqual(model.candidates(), [])

    def test_discovery_receives_the_working_config(self):
        seen = []
        model = SettingsModel(self.path, self.status_dir, discover=lambda c: seen.append(c) or [], runner=self.runner)
        model.update(on_launch="stop")
        model.candidates()
        self.assertEqual(seen[0].on_launch, "stop")

    def test_recent_games_newest_first_and_unique(self):
        model = self.make()
        statusmod.append_event("intercept", "Portal started while ollama used the GPU", self.status_dir, game="steam:400")
        statusmod.append_event("answer", "Portal: stop", self.status_dir, game="steam:400")
        statusmod.append_event("intercept", "Half-Life started while ollama used the GPU", self.status_dir, game="steam:70")
        statusmod.append_event("intercept", "Portal started while ollama used the GPU", self.status_dir, game="steam:400")
        statusmod.append_event("intercept", "no title", self.status_dir, game="steam:9", title="Explicit")
        statusmod.append_event("intercept", "no game id here", self.status_dir)
        self.assertEqual(model.recent_games(), [("steam:9", "Explicit"), ("steam:400", "Portal"),
                                                ("steam:70", "Half-Life")])

    def test_recent_games_falls_back_to_the_rule_name_then_the_id(self):
        model = self.make()
        statusmod.append_event("intercept", "odd message", self.status_dir, game="steam:5")
        statusmod.append_event("intercept", "odd message", self.status_dir, game="steam:6")
        model.set_game("steam:5", "Five", "ask")
        self.assertEqual(dict(model.recent_games()), {"steam:5": "Five", "steam:6": "steam:6"})

    def test_plan_for_uses_the_working_config_and_pause_state(self):
        model = self.make()
        targets = [Target("container", "docker", "ollama", vram_mib=1)]
        self.assertEqual(model.plan_for("steam:1", targets).action, "ask")
        model.update(on_launch="stop")
        plan = model.plan_for("steam:1", targets)
        self.assertEqual(plan.action, "stop")
        self.assertEqual([t.name for t in plan.will_stop], ["ollama"])
        model.update(ignore_containers=["ollama"])
        self.assertEqual(model.plan_for("steam:1", targets).action, "nothing-running")
        model.update(ignore_containers=[])
        model.pause()
        self.assertEqual(model.plan_for("steam:1", targets).action, "paused")
        self.assertEqual(model.plan_for("steam:1", targets, can_ask=False).can_ask, False)

    def test_service_state(self):
        model = self.make()
        state = settings_model.service_state
        self.assertEqual(state(model)[0], settings_model.STATE_OFF)
        statusmod.write_status(statusmod.Status(updated=time.time()), self.status_dir)
        self.assertEqual(state(model), (settings_model.STATE_WATCHING, None))
        model.pause()
        self.assertEqual(state(model), (settings_model.STATE_PAUSED, None))
        model.pause(600)
        self.assertEqual(state(model)[0], settings_model.STATE_PAUSED)
        self.assertIsNotNone(state(model)[1])
        model.resume()
        model.update(dialog="none")
        model.apply()
        self.assertEqual(state(model)[0], settings_model.STATE_RESTART)

    def test_service_state_disabled(self):
        model = self.make()
        statusmod.write_status(statusmod.Status(updated=time.time(), state="disabled"), self.status_dir)
        self.assertEqual(settings_model.service_state(model)[0], settings_model.STATE_DISABLED)


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class WindowTests(Base):
    def setUp(self):
        super().setUp()
        theme.apply_theme(self.app, dark=False)
        self.prefs = self.dir / "ui.json"
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.confirm_unsaved = lambda w: "discard"
            window.close()
            window.deleteLater()

    def window(self, model=None) -> "settings.SettingsWindow":
        window = settings.SettingsWindow(model or self.make(), self.prefs)
        window.basic.load()
        self.windows.append(window)
        return window

    def watching(self):
        statusmod.write_status(statusmod.Status(updated=time.time()), self.status_dir)

    def test_basic_is_the_default_page(self):
        window = self.window()
        self.assertEqual(window.mode, "basic")
        self.assertIs(window.stack.currentWidget(), window.basic)
        self.assertEqual(window.switcher.value(), "basic")
        self.assertGreaterEqual(window.minimumWidth(), 780)
        self.assertGreaterEqual(window.minimumHeight(), 600)

    def test_pill_follows_the_service(self):
        window = self.window()
        self.assertEqual(window.pill.text(), "Not running")
        self.assertFalse(window.not_running.isHidden())
        self.watching()
        window.refresh()
        self.assertEqual(window.pill.text(), "Watching")
        self.assertTrue(window.not_running.isHidden())
        window.model.pause()
        self.assertEqual(window.pill.text(), "Paused")
        window.model.resume()
        window.model.update(dialog="none")
        window.model.apply()
        self.assertEqual(window.pill.text(), "Restart pending")

    def test_not_running_card_names_the_command(self):
        window = self.window()
        self.assertEqual(window.command_label.text(), "systemctl --user start gpu-game-guard")

    def test_apply_and_revert_follow_dirty(self):
        window = self.window()
        self.assertFalse(window.apply_button.isEnabled())
        self.assertFalse(window.revert_button.isEnabled())
        window.model.update(on_launch="stop")
        self.assertTrue(window.apply_button.isEnabled())
        self.assertTrue(window.revert_button.isEnabled())
        window.revert_button.click()
        self.assertFalse(window.apply_button.isEnabled())
        self.assertEqual(window.model.config.on_launch, "ask")

    def test_apply_saves(self):
        window = self.window()
        window.model.update(on_launch="stop")
        window.apply_button.click()
        self.assertEqual(configmod.load(self.path).on_launch, "stop")
        self.assertFalse(window.apply_button.isEnabled())
        self.assertTrue(window.problems_callout.isHidden())

    def test_apply_shows_problems_and_keeps_the_file_untouched(self):
        window = self.window()
        window.model.update(heavy_vram_mib=-4)
        self.assertFalse(window.apply())
        self.assertFalse(window.problems_callout.isHidden())
        self.assertIn("heavy_vram_mib", window.problems_callout.label.text())
        self.assertFalse(self.path.exists())
        window.revert()
        self.assertTrue(window.problems_callout.isHidden())

    def test_restart_banner_and_button(self):
        window = self.window()
        self.assertTrue(window.restart_banner.isHidden())
        window.model.update(dialog="none")
        window.model.apply()
        self.assertFalse(window.restart_banner.isHidden())
        window.restart_banner.button.click()
        self.assertTrue(window.restart_banner.isHidden())
        self.assertEqual(self.runner.calls[0][-1], "gpu-game-guard.service")

    def test_failed_restart_is_reported(self):
        self.runner.code, self.runner.output = 1, "Failed to connect to bus"
        window = self.window()
        window.model.update(dialog="none")
        window.model.apply()
        window.restart_banner.button.click()
        self.assertFalse(window.restart_banner.isHidden())
        self.assertIn("Failed to connect to bus", window.problems_callout.label.text())

    def test_unreadable_config_offers_the_defaults(self):
        self.path.write_text("nope")
        window = self.window()
        self.assertFalse(window.load_callout.isHidden())
        window.load_callout.button.click()
        self.assertTrue(window.load_callout.isHidden())
        self.assertTrue(window.apply_button.isEnabled())

    def test_advanced_unavailable_shows_a_callout_and_basic_keeps_working(self):
        window = self.window()
        real = settings.SettingsWindow._make_advanced

        def broken(self):
            import builtins
            original = builtins.__import__

            def fail(name, *args, **kwargs):
                if name == "advanced" or name.endswith(".advanced"):
                    raise ImportError("missing")
                return original(name, *args, **kwargs)

            builtins.__import__ = fail
            try:
                return real(self)
            finally:
                builtins.__import__ = original

        window._make_advanced = broken.__get__(window)
        window.set_mode("advanced", save=False)
        self.assertEqual(window.mode, "advanced")
        self.assertTrue(window.advanced_error)
        callouts = window.advanced.findChildren(theme.Callout)
        self.assertIn("unavailable", callouts[0].label.text())
        window.set_mode("basic", save=False)
        self.assertIs(window.stack.currentWidget(), window.basic)

    def test_footnote_link_switches_to_advanced(self):
        window = self.window()
        window.basic.advancedRequested.emit()
        self.assertEqual(window.mode, "advanced")
        self.assertEqual(window.switcher.value(), "advanced")

    def test_mode_and_size_persist(self):
        window = self.window()
        window.set_mode("advanced")
        window.resize(1000, 700)
        window.close()
        saved = json.loads(self.prefs.read_text())
        self.assertEqual(saved["mode"], "advanced")
        self.assertEqual(saved["size"], [1000, 700])
        again = self.window()
        self.assertEqual(again.mode, "advanced")
        self.assertEqual((again.width(), again.height()), (1000, 700))

    def test_bad_prefs_are_ignored(self):
        self.prefs.write_text(json.dumps({"mode": "weird", "size": [5, "x"]}))
        window = self.window()
        self.assertEqual(window.mode, "basic")
        self.assertGreaterEqual(window.width(), 780)
        self.prefs.write_text("not json")
        self.assertEqual(self.window().mode, "basic")

    def test_small_saved_size_is_raised_to_the_minimum(self):
        self.prefs.write_text(json.dumps({"size": [100, 100]}))
        window = self.window()
        self.assertEqual((window.width(), window.height()), (780, 600))

    def test_prefs_default_next_to_the_config(self):
        window = settings.SettingsWindow(self.make())
        self.windows.append(window)
        self.assertEqual(window.prefs_path, self.dir / "ui.json")

    def test_closing_with_changes_asks(self):
        window = self.window()
        window.model.update(on_launch="stop")
        answers = ["cancel", "save"]
        window.confirm_unsaved = lambda w: answers.pop(0)
        window.close()
        self.assertTrue(window.model.dirty)
        window.close()
        self.assertFalse(window.model.dirty)
        self.assertEqual(configmod.load(self.path).on_launch, "stop")

    def test_closing_and_discarding(self):
        window = self.window()
        window.model.update(on_launch="stop")
        window.confirm_unsaved = lambda w: "discard"
        window.close()
        self.assertFalse(self.path.exists())

    def test_saving_on_close_with_problems_keeps_the_window(self):
        window = self.window()
        window.model.update(heavy_vram_mib=-1)
        window.confirm_unsaved = lambda w: "save"
        self.assertFalse(window.close())
        self.assertFalse(window.problems_callout.isHidden())

    def test_segmented_switch_drives_the_mode(self):
        window = self.window()
        window.switcher.segments["advanced"].click()
        self.assertEqual(window.mode, "advanced")
        window.switcher.segments["basic"].click()
        self.assertEqual(window.mode, "basic")

    def test_renders_in_both_themes(self):
        window = self.window()
        self.watching()
        for dark in (False, True):
            theme.apply_theme(self.app, dark=dark)
            out = self.dir / f"window-{dark}.png"
            self.assertTrue(theme.render(window, str(out)))
            self.assertGreater(out.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
