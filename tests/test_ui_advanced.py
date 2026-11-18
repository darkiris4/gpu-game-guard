from __future__ import annotations

import copy
import dataclasses
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from gpu_game_guard import config as configmod  # noqa: E402
from gpu_game_guard import guard, presets  # noqa: E402
from gpu_game_guard.config import Config, GameRule, TargetSpec  # noqa: E402
from gpu_game_guard.discovery import BUILTIN_PROTECT, Candidate  # noqa: E402
from gpu_game_guard.model import Target  # noqa: E402
from gpu_game_guard.status import Status  # noqa: E402
from gpu_game_guard.ui import qt  # noqa: E402

try:
    from gpu_game_guard.ui import settings_model
except Exception:  # the real model is optional for these tests
    settings_model = None

if qt.available():
    from gpu_game_guard.ui import (advanced, advanced_activity, advanced_common, advanced_dialogs, advanced_games,
                                   advanced_target_dialogs, advanced_testrun, advanced_timing, theme)

    QtCore, QtGui, QtWidgets = qt.QtCore, qt.QtGui, qt.QtWidgets


def sample_candidates():
    return [
        Candidate("container:docker:ollama", "container", "ollama", "ollama", "docker container, image ollama/ollama",
                  6144, True, "low", None, "Stopped before a game and started again afterwards."),
        Candidate("container:docker:comfyui", "container", "comfyui", "comfyui", "docker container, image comfy/ui",
                  3072, True, "low", None, "Stopped before a game and started again afterwards."),
        Candidate("container:podman:backup-db", "container", "backup-db", "backup-db", "podman container",
                  512, False, "low", None, "Ignored through ignore_containers."),
        Candidate("service:system:jellyfin.service", "service", "jellyfin.service", "jellyfin",
                  "system service jellyfin.service, 1 GPU process", 900, False, "low",
                  {"id": "jellyfin", "kind": "service", "label": "jellyfin", "unit": "jellyfin.service",
                   "scope": "system"}, "Stopped with systemctl."),
        Candidate("process:blender", "process", "blender", "blender", "1 process using the GPU", 2048, False, "high",
                  {"id": "blender", "kind": "process", "label": "blender", "match": "blender", "restart": True},
                  "Stopping a process ends it."),
    ]


def sample_config():
    cfg = Config()
    cfg.ignore_containers = ["backup-*"]
    cfg.protect = ["my-editor*"]
    cfg.games = {"steam:730": GameRule("Counter-Strike 2", "ignore"), "steam:1245620": GameRule("Elden Ring", "stop"),
                 "steam:620": GameRule("Portal 2", "ask")}
    cfg.targets = [
        TargetSpec(id="ollama-svc", kind="service", label="Ollama", unit="ollama.service", scope="system"),
        TargetSpec(id="obs", kind="process", label="OBS Studio", match="obs", restart=True),
    ]
    return cfg


def sample_events(now=None):
    now = time.time() if now is None else now
    return [
        {"t": now - 600, "kind": "adopt", "message": "Counter-Strike 2 was already running; left alone"},
        {"t": now - 300, "kind": "intercept", "message": "Elden Ring started while ollama was using the GPU",
         "game": "steam:1245620", "title": "Elden Ring"},
        {"t": now - 290, "kind": "answer", "message": "Elden Ring: stop"},
        {"t": now - 288, "kind": "stop", "message": "Stopped ollama, comfyui"},
        {"t": now - 40, "kind": "failure", "message": "Could not start ollama.service: access denied"},
    ]


def sample_status(**fields):
    base = dict(version="2.0.0", pid=4242, updated=time.time(), state="game-running",
                game={"id": "steam:1245620", "title": "Elden Ring"},
                stopped=[{"kind": "container", "runtime": "docker", "name": "ollama"}], dialog="qt", can_ask=True,
                targets=2)
    base.update(fields)
    return Status(**base)


if qt.available():

    class FakeModel(QtCore.QObject):
        """The settings model's interface over plain values, with no files and no discovery."""

        changed = QtCore.pyqtSignal()

        def __init__(self, config=None, candidates=None, status=None, events=None, recent=None):
            super().__init__()
            self.path = Path("/nonexistent/config.json")
            self._config = copy.deepcopy(config or Config())
            self.saved = copy.deepcopy(self._config)
            self.problems = []
            self.load_error = ""
            self._candidates = sample_candidates() if candidates is None else candidates
            self._status = status
            self._events = events or []
            self._recent = recent or []
            self._paused = (False, None)
            self.restart = False
            self.restart_calls = 0
            self.candidate_calls = 0
            self._announced = False

        @property
        def config(self):
            return self._config

        @property
        def dirty(self):
            return self._config != self.saved

        def _replace(self, cfg):
            self._config = cfg
            self.changed.emit()

        def set_config(self, cfg):
            self._replace(copy.deepcopy(cfg))

        def update(self, **fields):
            self._replace(dataclasses.replace(copy.deepcopy(self._config), **fields))

        def set_target(self, spec):
            self._replace(self._config.with_target(spec))

        def remove_target(self, target_id):
            self._replace(self._config.without_target(target_id))

        def set_game(self, game_id, name, action):
            self._replace(self._config.with_game(game_id, name, action))

        def remove_game(self, game_id):
            cfg = copy.deepcopy(self._config)
            cfg.games.pop(game_id, None)
            self._replace(cfg)

        def apply(self):
            self.saved = copy.deepcopy(self._config)
            return []

        def revert(self):
            self._replace(copy.deepcopy(self.saved))

        def candidates(self, refresh=False):
            self.candidate_calls += 1
            if not self._announced or refresh:
                self._announced = True
                self.changed.emit()
            return list(self._candidates)

        def status(self):
            return self._status

        def events(self, limit=100):
            return list(self._events[-limit:])

        def recent_games(self):
            return list(self._recent)

        def pause(self, seconds=None):
            self._paused = (True, None)
            self.changed.emit()

        def resume(self):
            self._paused = (False, None)
            self.changed.emit()

        def paused(self):
            return self._paused

        def restart_needed(self):
            return self.restart

        def restart_service(self):
            self.restart_calls += 1
            self.restart = False
            self.changed.emit()
            return True, "The service was restarted."

        def plan_for(self, game_id, targets, can_ask=True):
            return guard.plan(self._config, game_id, targets, can_ask, paused=self._paused[0])

        def effective_action_for_game(self, game_id):
            return self._config.action_for(game_id)


def release(*widgets):
    """Delete widgets now: a stylesheet change re-polishes every live widget, so leaked ones slow later tests."""
    for widget in widgets:
        widget.setParent(None)
        widget.deleteLater()
    QtWidgets.QApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)


def fill_and_accept(**fields):
    """A `run_dialog` replacement: fill the named line edits of the dialog, then accept it."""

    def run(dialog):
        for name, value in fields.items():
            getattr(dialog, name).setText(value)
        dialog.accept()
        return dialog.result() == QtWidgets.QDialog.Accepted

    return run


def cell_switch(table, row):
    return table.cellWidget(row, 0).findChild(theme.ToggleSwitch)


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()
        theme.apply_theme(cls.app, dark=False)

    def tearDown(self):
        release(self.view)

    def setUp(self):
        self.model = FakeModel(sample_config(), status=sample_status(), events=sample_events(),
                               recent=[("steam:1245620", "Elden Ring"), ("steam:730", "Counter-Strike 2")])
        self.polkit_calls = []
        self.view = advanced.AdvancedView(
            self.model, which=lambda name: f"/usr/bin/{name}" if name in ("zenity", "notify-send") else None,
            polkit_status=self.fake_polkit, qt_available=lambda: True, defer_discovery=False)

    def fake_polkit(self, unit):
        self.polkit_calls.append(unit)
        return unit == "ollama.service"


class ViewTests(Base):
    def test_tab_names_in_order(self):
        names = ["Targets", "Games", "Detection", "Timing", "Dialogs", "Activity", "Test run", "Raw config"]
        self.assertEqual(advanced.AdvancedView.tab_names(), names)
        self.assertEqual([self.view.tabs.tabText(i) for i in range(self.view.tabs.count())], names)

    def test_every_tab_is_a_page_of_the_view(self):
        self.assertEqual(len(self.view.pages()), 8)
        self.view.show_tab("Timing")
        self.assertEqual(self.view.current_name(), "Timing")

    def test_model_changes_reach_every_tab(self):
        self.model.update(dialog="zenity", poll_seconds=2.5, auto_containers=False)
        self.assertEqual(self.view.dialogs.combo.currentData(), "zenity")
        self.assertEqual(self.view.timing.spins["poll_seconds"].value(), 2.5)
        self.assertFalse(self.view.detection.auto_switch.isChecked())

    def test_discovery_is_deferred_by_default(self):
        model = FakeModel(sample_config())
        view = advanced.AdvancedView(model, which=lambda n: None, polkit_status=lambda u: None)
        self.assertEqual(model.candidate_calls, 0)
        self.assertIn("Detecting", view.targets.caption.text())
        view.source.load()
        self.assertGreater(model.candidate_calls, 0)
        self.assertEqual(view.targets.table.rowCount(), 5)

    def test_renders_in_both_palettes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for dark in (False, True):
                theme.apply_theme(self.app, dark=dark)
                for index in range(self.view.tabs.count()):
                    self.view.tabs.setCurrentIndex(index)
                    path = os.path.join(tmp, f"{dark}-{index}.png")
                    self.assertTrue(theme.render(self.view, path))
                    self.assertGreater(os.path.getsize(path), 1000)
        theme.apply_theme(self.app, dark=False)


class HelperTests(unittest.TestCase):
    @unittest.skipUnless(qt.available(), "PyQt5 is not installed")
    def test_formatting_and_slugs(self):
        self.assertEqual(advanced_common.mib_text(None), "unknown")
        self.assertEqual(advanced_common.mib_text(0), "none")
        self.assertEqual(advanced_common.mib_text(512), "512 MiB")
        self.assertEqual(advanced_common.mib_text(6144), "6.0 GiB")
        self.assertEqual(advanced_common.slug("Jellyfin Media!", set()), "jellyfin-media")
        self.assertEqual(advanced_common.slug("obs", {"obs"}), "obs-2")
        self.assertEqual(advanced_common.slug("", set()), "target")
        now = time.time()
        self.assertRegex(advanced_common.clock_text(now - 5, now), r"^\d\d:\d\d:\d\d$")
        self.assertRegex(advanced_common.clock_text(now - 90000, now), r"^[A-Z][a-z]{2} \d\d \d\d:\d\d:\d\d$")

    @unittest.skipUnless(qt.available(), "PyQt5 is not installed")
    def test_range_tables_come_from_the_config_module(self):
        self.assertIs(advanced_common.NUMBER_RANGES, configmod._NUMBERS)
        self.assertIs(advanced_common.TIMEOUT_RANGE, configmod._TIMEOUT_RANGE)


class TargetsTabTests(Base):
    @property
    def tab(self):
        return self.view.targets

    def test_rows_cover_containers_then_configured_targets(self):
        table = self.tab.table
        self.assertEqual(table.rowCount(), 5)
        self.assertEqual([table.item(r, 1).text() for r in range(5)],
                         ["ollama", "comfyui", "backup-db", "Ollama", "OBS Studio"])
        self.assertEqual([table.item(r, 2).text() for r in range(5)],
                         ["Container", "Container", "Container", "Service", "Process"])
        self.assertEqual(table.item(0, 4).text(), "6.0 GiB")
        self.assertEqual(table.item(3, 3).text(), "ollama.service (system)")
        self.assertIn("(ignored)", table.item(2, 3).text())

    def test_kind_badges_carry_a_risk_colour(self):
        table = self.tab.table
        kinds = [table.item(r, 2).data(advanced_common.KIND_ROLE) for r in range(5)]
        self.assertEqual(kinds, ["ok", "ok", "ok", "ok", "danger"])
        command = TargetSpec(id="unload", kind="command", stop=["true"], start=["true"])
        self.model.set_target(command)
        self.assertEqual(self.tab.table.item(5, 2).data(advanced_common.KIND_ROLE), "warn")

    def test_auto_detected_rows_are_read_only(self):
        table = self.tab.table
        self.assertFalse(cell_switch(table, 0).isEnabled())
        self.assertTrue(cell_switch(table, 0).isChecked())
        self.assertFalse(cell_switch(table, 2).isChecked())
        table.selectRow(0)
        self.assertFalse(self.tab.edit_button.isEnabled())
        self.assertFalse(self.tab.remove_button.isEnabled())
        self.assertIn("Detection", self.tab.note.label.text())

    def test_selecting_a_configured_target_enables_edit_and_remove(self):
        self.tab.table.selectRow(4)
        self.assertTrue(self.tab.edit_button.isEnabled())
        self.assertTrue(self.tab.remove_button.isEnabled())
        self.assertEqual(self.tab.selected_id(), "obs")

    def test_toggle_writes_the_enabled_flag_through_the_model(self):
        cell_switch(self.tab.table, 4).setChecked(False)
        self.assertFalse(self.model.config.target("obs").enabled)
        self.assertTrue(self.model.config.target("ollama-svc").enabled)

    def test_add_service_goes_through_the_model(self):
        self.tab.run_dialog = fill_and_accept(label_edit="Jellyfin", unit_edit="jellyfin.service")
        self.tab.service_button.click()
        spec = self.model.config.target("jellyfin")
        self.assertEqual((spec.kind, spec.unit, spec.scope, spec.label), ("service", "jellyfin.service", "system",
                                                                          "Jellyfin"))
        self.assertEqual(self.tab.table.rowCount(), 6)

    def test_add_command_and_process(self):
        self.tab.run_dialog = fill_and_accept(label_edit="Unload", stop_edit="python3 -c 'print(1)'",
                                              start_edit="true")
        self.tab.command_button.click()
        self.assertEqual(self.model.config.target("unload").stop, ["python3", "-c", "print(1)"])
        self.tab.run_dialog = fill_and_accept(label_edit="Blender", match_edit="blender")
        self.tab.process_button.click()
        self.assertEqual(self.model.config.target("blender").match, "blender")

    def test_a_cancelled_dialog_changes_nothing(self):
        before = copy.deepcopy(self.model.config)
        self.tab.run_dialog = lambda dialog: False
        for button in (self.tab.service_button, self.tab.command_button, self.tab.process_button,
                       self.tab.preset_button):
            button.click()
        self.assertEqual(self.model.config, before)

    def test_edit_replaces_the_target_with_the_same_id(self):
        self.tab.table.selectRow(3)
        self.tab.run_dialog = fill_and_accept(label_edit="Ollama server", unit_edit="ollama2.service")
        self.tab.edit_button.click()
        spec = self.model.config.target("ollama-svc")
        self.assertEqual((spec.label, spec.unit), ("Ollama server", "ollama2.service"))
        self.assertEqual(len(self.model.config.targets), 2)

    def test_remove_asks_first(self):
        self.tab.table.selectRow(4)
        self.tab.confirm = lambda parent, text: False
        self.tab.remove_button.click()
        self.assertIsNotNone(self.model.config.target("obs"))
        asked = []
        self.tab.confirm = lambda parent, text: asked.append(text) or True
        self.tab.remove_button.click()
        self.assertIsNone(self.model.config.target("obs"))
        self.assertIn("OBS Studio", asked[0])

    def test_preset_button_adds_every_ticked_preset(self):
        shown = []

        def run(dialog):
            shown.append(dialog)
            return True

        self.tab.presets_loader = lambda: presets.load_presets()
        self.tab.applicable = lambda preset: preset.id == "ollama-unload"
        self.tab.run_dialog = run
        self.tab.preset_button.click()
        self.assertIsNotNone(self.model.config.target("ollama-unload"))
        self.assertEqual(len(shown), 1)

    def test_memory_of_a_configured_service_comes_from_discovery(self):
        spec = TargetSpec(id="jf", kind="service", unit="jellyfin.service")
        self.model.set_target(spec)
        self.assertEqual(self.tab.table.item(5, 4).text(), "900 MiB")


class TargetDialogTests(Base):
    def service(self, existing=None, polkit=None):
        return advanced_target_dialogs.ServiceDialog(self.model.config, existing, polkit or self.fake_polkit)

    def test_id_is_derived_from_the_name_until_edited(self):
        dialog = self.service()
        dialog.label_edit.setText("Jellyfin Media")
        self.assertEqual(dialog.id_edit.text(), "jellyfin-media")
        dialog.id_edit.textEdited.emit("mine")
        dialog.id_edit.setText("mine")
        dialog.label_edit.setText("Something else")
        self.assertEqual(dialog.id_edit.text(), "mine")

    def test_derived_id_avoids_existing_ids(self):
        dialog = self.service()
        dialog.label_edit.setText("obs")
        self.assertEqual(dialog.id_edit.text(), "obs-2")

    def test_id_rules(self):
        dialog = self.service()
        dialog.unit_edit.setText("a.service")
        for text, fragment in (("", "Enter an id"), ("Bad Id", "lowercase"), ("-x", "lowercase"),
                               ("x" * 41, "40"), ("obs", "already")):
            dialog.id_edit.setText("tmp")
            dialog.id_edit.setText(text)
            self.assertIn(fragment.lower(), dialog.problems()["id"].lower(), text)
            self.assertIn(fragment.lower(), dialog.error_text("id").lower())
        dialog.id_edit.setText("fine-id_1")
        self.assertEqual(dialog.problems(), {})
        self.assertTrue(dialog.save_button.isEnabled())

    def test_save_is_disabled_while_invalid(self):
        dialog = self.service()
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.accept()
        self.assertNotEqual(dialog.result(), QtWidgets.QDialog.Accepted)
        self.assertIsNone(dialog.spec())

    def test_untouched_fields_show_no_error_until_accept(self):
        dialog = self.service()
        self.assertEqual(dialog.error_text("unit"), "")
        dialog.accept()
        self.assertIn(".service", dialog.error_text("unit"))

    def test_unit_rules(self):
        dialog = self.service()
        dialog.id_edit.setText("x")
        for text, fragment in (("", "Enter the unit"), ("ollama", "end in .service"), ("a b.service", "no spaces"),
                               ("a/b.service", "no spaces"), (".service", "end in .service")):
            dialog.unit_edit.setText(text)
            self.assertIn(fragment, dialog.problems()["unit"], text)
        dialog.unit_edit.setText("ollama.service")
        self.assertEqual(dialog.problems(), {})
        self.assertEqual(dialog.spec().unit, "ollama.service")

    def test_system_scope_shows_the_polkit_command_and_copies_it(self):
        dialog = self.service()
        dialog.unit_edit.setText("jellyfin.service")
        self.assertFalse(dialog.access.isHidden())
        self.assertEqual(dialog.command_edit.text(), "sudo gpu-game-guard polkit allow jellyfin.service")
        self.assertEqual(dialog.access_state.text(), "Not allowed yet")
        dialog.copy_button.click()
        self.assertEqual(self.app.clipboard().text(), "sudo gpu-game-guard polkit allow jellyfin.service")
        dialog.unit_edit.setText("ollama.service")
        self.assertEqual(dialog.access_state.text(), "Already allowed")

    def test_polkit_command_uses_a_placeholder_without_a_unit(self):
        self.assertEqual(advanced_target_dialogs.polkit_command(""), "sudo gpu-game-guard polkit allow UNIT.service")

    def test_user_scope_hides_the_polkit_command(self):
        dialog = self.service()
        dialog.scope_combo.setCurrentText("user")
        self.assertTrue(dialog.access.isHidden())
        dialog.label_edit.setText("x")
        dialog.unit_edit.setText("x.service")
        self.assertEqual(dialog.spec().scope, "user")

    def test_the_default_polkit_probe_gives_none_for_an_unusable_unit(self):
        self.assertIsNone(advanced_target_dialogs.default_polkit_status("not a unit"))

    def test_service_edit_keeps_the_id_and_enabled_flag(self):
        existing = TargetSpec(id="ollama-svc", kind="service", label="Ollama", unit="ollama.service", enabled=False)
        dialog = self.service(existing)
        self.assertTrue(dialog.id_edit.isReadOnly())
        self.assertEqual(dialog.unit_edit.text(), "ollama.service")
        self.assertEqual(dialog.problems(), {})
        self.assertFalse(dialog.spec().enabled)
        self.assertEqual(dialog.spec().id, "ollama-svc")

    def command(self, existing=None):
        return advanced_target_dialogs.CommandDialog(self.model.config, existing)

    def test_command_lines_are_split_like_a_shell(self):
        dialog = self.command()
        dialog.label_edit.setText("Unload")
        dialog.stop_edit.setText("python3 -c 'import sys; print(1)'")
        dialog.start_edit.setText('systemctl --user start "my server"')
        dialog.check_edit.setText("pgrep -x ollama")
        spec = dialog.spec()
        self.assertEqual(spec.stop, ["python3", "-c", "import sys; print(1)"])
        self.assertEqual(spec.start, ["systemctl", "--user", "start", "my server"])
        self.assertEqual(spec.check, ["pgrep", "-x", "ollama"])
        self.assertEqual(spec.timeout_seconds, 60.0)

    def test_command_problems(self):
        dialog = self.command()
        dialog.label_edit.setText("Unload")
        self.assertIn("Enter the command", dialog.problems()["stop"])
        dialog.stop_edit.setText("echo 'open")
        self.assertIn("quote", dialog.problems()["stop"])
        dialog.stop_edit.setText("true")
        dialog.start_edit.setText("true")
        self.assertEqual(dialog.problems(), {})
        dialog.check_edit.setText('x "y')
        self.assertIn("quote", dialog.problems()["check"])
        dialog.check_edit.setText("")
        self.assertIsNone(dialog.spec().check)

    def test_command_timeout_range_is_the_configs(self):
        dialog = self.command()
        low, high = configmod._TIMEOUT_RANGE
        self.assertEqual((dialog.timeout_spin.minimum(), dialog.timeout_spin.maximum()), (low, high))

    def test_command_edit_round_trips_quoting(self):
        existing = TargetSpec(id="u", kind="command", label="u", stop=["sh", "-c", "echo a b"], start=["true"],
                              check=["pgrep", "x"], timeout_seconds=30)
        dialog = self.command(existing)
        self.assertEqual(dialog.spec(), existing)

    def process(self, existing=None):
        return advanced_target_dialogs.ProcessDialog(self.model.config, existing)

    def test_process_dialog_carries_the_warning(self):
        dialog = self.process()
        self.assertEqual(dialog.warning.label.text(), "Stopping a program can lose unsaved work. GPU Game Guard "
                         "asks it to close politely and never forces it.")
        self.assertEqual(dialog.warning.property("kind"), "warn")

    def test_process_refuses_protected_names(self):
        dialog = self.process()
        dialog.label_edit.setText("x")
        for name in ("bash", "Xorg", "steam", "my-editor", "gpu-game-guard"):
            dialog.match_edit.setText(name)
            self.assertIn("protected", dialog.problems()["match"], name)
            self.assertIsNone(dialog.spec())
        dialog.match_edit.setText("*")
        self.assertIn("protected", dialog.problems()["match"])
        dialog.match_edit.setText("s*")
        self.assertIn("protected", dialog.problems()["match"])
        dialog.match_edit.setText("obs")
        self.assertEqual(dialog.problems(), {})

    def test_process_glob_rules(self):
        dialog = self.process()
        dialog.label_edit.setText("x")
        dialog.match_edit.setText("")
        self.assertIn("Enter the program", dialog.problems()["match"])
        dialog.match_edit.setText("usr/bin/obs")
        self.assertIn("not a path", dialog.problems()["match"])
        dialog.match_edit.setText("blender*")
        dialog.restart_switch.setChecked(False)
        spec = dialog.spec()
        self.assertEqual((spec.match, spec.restart), ("blender*", False))

    def test_process_edit_loads_fields(self):
        dialog = self.process(self.model.config.target("obs"))
        self.assertEqual(dialog.match_edit.text(), "obs")
        self.assertTrue(dialog.restart_switch.isChecked())
        self.assertEqual(dialog.spec().label, "OBS Studio")

    def test_dialogs_stay_consistent_with_config_validate(self):
        for dialog in (self.service(), self.command(), self.process()):
            dialog.label_edit.setText("thing")
            if isinstance(dialog, advanced_target_dialogs.ServiceDialog):
                dialog.unit_edit.setText("thing.service")
            elif isinstance(dialog, advanced_target_dialogs.CommandDialog):
                dialog.stop_edit.setText("true")
                dialog.start_edit.setText("true")
            else:
                dialog.match_edit.setText("thing")
            self.assertEqual(configmod.validate({"targets": [dialog.entry()]}), [])


class PresetDialogTests(Base):
    def test_applicable_presets_are_ticked_and_described(self):
        dialog = advanced_target_dialogs.PresetDialog(
            self.model.config, applicable=lambda preset: preset.id != "ollama-user-service")
        loaded = presets.load_presets()
        self.assertEqual(len(dialog.checks), len(loaded))
        self.assertEqual({k: c.isChecked() for k, c in dialog.checks.items()},
                         {"ollama-system-service": True, "ollama-user-service": False, "ollama-unload": True})
        for preset in loaded:
            self.assertEqual(dialog.checks[preset.id].toolTip(), preset.description)
        ids = [spec.id for spec in dialog.selected()]
        self.assertEqual(ids, ["ollama", "ollama-unload"])

    def test_already_added_presets_cannot_be_ticked(self):
        self.model.set_target(TargetSpec.from_json(presets.load_presets()[0].spec))
        dialog = advanced_target_dialogs.PresetDialog(self.model.config, applicable=lambda preset: True)
        first = dialog.checks[presets.load_presets()[0].id]
        self.assertFalse(first.isEnabled())
        self.assertFalse(first.isChecked())

    def test_add_is_disabled_without_a_selection(self):
        dialog = advanced_target_dialogs.PresetDialog(self.model.config, applicable=lambda preset: False)
        self.assertFalse(dialog.add_button.isEnabled())
        next(iter(dialog.checks.values())).setChecked(True)
        self.assertTrue(dialog.add_button.isEnabled())

    def test_a_failing_probe_counts_as_not_found(self):
        def boom(preset):
            raise RuntimeError("no systemctl")

        dialog = advanced_target_dialogs.PresetDialog(self.model.config, applicable=boom)
        self.assertEqual(dialog.selected(), [])

    def test_empty_preset_list(self):
        dialog = advanced_target_dialogs.PresetDialog(self.model.config, load=lambda: [])
        self.assertEqual(dialog.selected(), [])
        self.assertFalse(dialog.add_button.isEnabled())


class GamesTabTests(Base):
    @property
    def tab(self):
        return self.view.games

    def test_rows_and_default(self):
        table = self.tab.table
        self.assertEqual([table.item(r, 0).text() for r in range(3)], ["steam:1245620", "steam:620", "steam:730"])
        self.assertEqual([table.item(r, 1).text() for r in range(3)], ["Elden Ring", "Portal 2", "Counter-Strike 2"])
        self.assertEqual(self.tab.default_badge.text(), "ask")
        self.model.update(on_launch="stop")
        self.assertEqual(self.tab.default_badge.text(), "stop")
        self.assertEqual(self.tab.table.rowCount(), 3)

    def test_action_combo_offers_the_three_actions_and_writes(self):
        combo = self.tab.combos["steam:620"]
        self.assertEqual([combo.itemText(i) for i in range(combo.count())], ["ask", "stop", "ignore"])
        self.assertEqual(self.tab.combos["steam:730"].currentText(), "ignore")
        combo.setCurrentText("stop")
        self.assertEqual(self.model.config.games["steam:620"].action, "stop")
        self.assertEqual(self.model.config.games["steam:620"].name, "Portal 2")

    def test_remove(self):
        self.tab.table.cellWidget(0, 3).findChild(QtWidgets.QPushButton).click()
        self.assertNotIn("steam:1245620", self.model.config.games)
        self.assertEqual(self.tab.table.rowCount(), 2)

    def test_empty_state(self):
        self.model.update(games={})
        self.assertFalse(self.tab.empty.isHidden())
        self.assertEqual(self.tab.table.rowCount(), 0)

    def test_add_game(self):
        seen = {}

        def run(dialog):
            seen["recent"] = [dialog.recent_list.item(i).text() for i in range(dialog.recent_list.count())]
            dialog.id_edit.setText("1091500")
            dialog.name_edit.setText("Cyberpunk 2077")
            dialog.action_combo.setCurrentText("stop")
            dialog.accept()
            return dialog.result() == QtWidgets.QDialog.Accepted

        self.tab.run_dialog = run
        self.tab.add_button.click()
        self.assertEqual(self.model.config.games["steam:1091500"], GameRule("Cyberpunk 2077", "stop"))
        self.assertEqual(seen["recent"], ["Elden Ring   (steam:1245620)", "Counter-Strike 2   (steam:730)"])

    def dialog(self):
        return advanced_games.AddGameDialog(["steam:730"], self.model.recent_games(), "ask")

    def test_game_id_rules(self):
        dialog = self.dialog()
        self.assertFalse(dialog.add_button.isEnabled())
        for text, ok in (("steam:730", True), ("730", True), ("", False), ("steam 730", False), ("steam:", False),
                         ("Steam:730", False), ("730 ", True), ("steam:7 30", False)):
            dialog.id_edit.setText(text)
            self.assertEqual(dialog.add_button.isEnabled(), ok, text)
            self.assertEqual(bool(dialog.problem()), not ok, text)

    def test_bare_app_id_becomes_a_steam_id(self):
        dialog = self.dialog()
        dialog.id_edit.setText("620")
        self.assertEqual(dialog.game_id(), "steam:620")
        self.assertEqual(dialog.values(), ("steam:620", "", "ask"))

    def test_invalid_id_blocks_accept(self):
        dialog = self.dialog()
        dialog.id_edit.setText("nope")
        dialog.accept()
        self.assertNotEqual(dialog.result(), QtWidgets.QDialog.Accepted)
        self.assertFalse(dialog.id_error.isHidden())

    def test_picking_a_recent_game_fills_the_fields(self):
        dialog = self.dialog()
        dialog.recent_list.itemClicked.emit(dialog.recent_list.item(0))
        self.assertEqual((dialog.id_edit.text(), dialog.name_edit.text()), ("steam:1245620", "Elden Ring"))
        self.assertTrue(dialog.add_button.isEnabled())

    def test_existing_rule_is_called_out(self):
        dialog = self.dialog()
        dialog.id_edit.setText("steam:730")
        self.assertIn("replaces", dialog.id_hint.text())

    def test_no_recent_games(self):
        dialog = advanced_games.AddGameDialog([], [], "ask")
        self.assertEqual(dialog.recent_list.count(), 1)
        dialog.recent_list.itemClicked.emit(dialog.recent_list.item(0))
        self.assertEqual(dialog.id_edit.text(), "")


class DetectionTabTests(Base):
    @property
    def tab(self):
        return self.view.detection

    def test_auto_containers_switch(self):
        self.assertTrue(self.tab.auto_switch.isChecked())
        self.tab.auto_switch.setChecked(False)
        self.assertFalse(self.model.config.auto_containers)
        self.model.update(auto_containers=True)
        self.assertTrue(self.tab.auto_switch.isChecked())

    def test_ignore_list_add_and_remove(self):
        editor = self.tab.ignore_editor
        self.assertEqual(editor.items(), ["backup-*"])
        editor.input.setText("test-?")
        editor.add_button.click()
        self.assertEqual(self.model.config.ignore_containers, ["backup-*", "test-?"])
        self.assertEqual(editor.input.text(), "")
        editor.list.setCurrentRow(0)
        editor.remove_button.click()
        self.assertEqual(self.model.config.ignore_containers, ["test-?"])

    def test_list_editor_rejects_blank_and_duplicate_rows(self):
        editor = self.tab.ignore_editor
        before = list(self.model.config.ignore_containers)
        for text in ("", "   ", "backup-*"):
            editor.input.setText(text)
            editor.add_button.click()
            self.assertFalse(editor.error.isHidden(), text)
        self.assertEqual(self.model.config.ignore_containers, before)

    def test_protect_list(self):
        editor = self.tab.protect_editor
        self.assertEqual(editor.items(), ["my-editor*"])
        editor.input.setText("render-*")
        editor.input.returnPressed.emit()
        self.assertEqual(self.model.config.protect, ["my-editor*", "render-*"])

    def test_builtin_protect_list_is_read_only_and_collapsible(self):
        self.assertTrue(self.tab.builtin_view.isHidden())
        self.assertTrue(self.tab.builtin_view.isReadOnly())
        self.tab.builtin_toggle.setChecked(True)
        self.assertFalse(self.tab.builtin_view.isHidden())
        text = self.tab.builtin_view.toPlainText()
        self.assertTrue(all(pattern in text for pattern in BUILTIN_PROTECT))
        self.assertIn(str(len(BUILTIN_PROTECT)), self.tab.builtin_toggle.text())
        self.tab.builtin_toggle.setChecked(False)
        self.assertTrue(self.tab.builtin_view.isHidden())

    def test_heavy_vram_spin_uses_the_configs_range(self):
        _kind, low, high = configmod._NUMBERS["heavy_vram_mib"]
        spin = self.tab.heavy_spin
        self.assertEqual((spin.minimum(), spin.maximum()), (low, high))
        self.assertEqual(spin.value(), 4096)
        spin.setValue(8192)
        self.assertEqual(self.model.config.heavy_vram_mib, 8192)
        self.assertEqual(configmod.validate(self.model.config.to_json()), [])

    def test_runtimes_are_listed_with_ticks(self):
        self.assertEqual(self.tab.runtimes(), [("docker", True), ("podman", True), ("service", True),
                                               ("command", True), ("process", True)])
        self.model.update(runtimes=["podman", "docker"])
        self.assertEqual(self.tab.runtimes()[:2], [("podman", True), ("docker", True)])
        self.assertEqual([name for name, on in self.tab.runtimes() if not on], ["service", "command", "process"])

    def test_unticking_a_runtime_writes_the_list(self):
        self.tab.runtime_list.item(1).setCheckState(QtCore.Qt.Unchecked)
        self.assertEqual(self.model.config.runtimes, ["docker", "service", "command", "process"])

    def test_runtime_order_can_be_changed(self):
        self.tab.runtime_list.setCurrentRow(0)
        self.assertFalse(self.tab.up_button.isEnabled())
        self.tab.down_button.click()
        self.assertEqual(self.model.config.runtimes[:2], ["podman", "docker"])
        self.assertEqual(self.tab.runtime_list.currentRow(), 1)
        self.tab.up_button.click()
        self.assertEqual(self.model.config.runtimes[:2], ["docker", "podman"])

    def test_restart_callout_follows_the_model(self):
        self.assertTrue(self.tab.restart_note.isHidden())
        self.model.restart = True
        self.model.changed.emit()
        self.assertFalse(self.tab.restart_note.isHidden())
        self.tab.restart_note.button.click()
        self.assertEqual(self.model.restart_calls, 1)
        self.assertTrue(self.tab.restart_note.isHidden())
        self.assertEqual(self.tab.restart_note.result.text(), "The service was restarted.")

    def test_runtime_change_needs_a_restart_note(self):
        self.assertTrue(any("restart" in label.text() for label in self.tab.findChildren(QtWidgets.QLabel)))


class TimingTabTests(Base):
    @property
    def tab(self):
        return self.view.timing

    def test_spins_use_the_exact_ranges_from_the_config(self):
        self.assertEqual(set(self.tab.spins), {"poll_seconds", "exit_grace_seconds", "no_game_timeout_seconds",
                                               "bypass_seconds", "dialog_retry_seconds"})
        for key, spin in self.tab.spins.items():
            _kind, low, high = configmod._NUMBERS[key]
            self.assertEqual((spin.minimum(), spin.maximum()), (float(low), float(high)), key)
            self.assertEqual(spin.value(), float(getattr(self.model.config, key)), key)

    def test_values_are_written_through_the_model(self):
        self.tab.spins["poll_seconds"].setValue(0.5)
        self.tab.spins["bypass_seconds"].setValue(300)
        self.assertEqual(self.model.config.poll_seconds, 0.5)
        self.assertEqual(self.model.config.bypass_seconds, 300.0)
        self.assertEqual(configmod.validate(self.model.config.to_json()), [])

    def test_out_of_range_input_is_clamped_to_a_valid_value(self):
        self.tab.spins["poll_seconds"].setValue(0.01)
        self.assertEqual(self.model.config.poll_seconds, 0.2)
        self.tab.spins["dialog_retry_seconds"].setValue(10 ** 7)
        self.assertEqual(self.model.config.dialog_retry_seconds, 86400.0)

    def test_every_row_has_a_range_line_and_tooltip(self):
        text = " ".join(label.text() for label in self.tab.findChildren(QtWidgets.QLabel))
        for key in self.tab.spins:
            self.assertIn(advanced_timing.range_text(key), text)
            self.assertIn(key, self.tab.spins[key].toolTip())

    def test_loading_does_not_write_back(self):
        writes = []
        original = self.model.update
        self.model.update = lambda **fields: writes.append(fields) or original(**fields)
        original(poll_seconds=4.0, enabled=False)
        self.assertEqual(writes, [])
        self.assertEqual(self.tab.spins["poll_seconds"].value(), 4.0)
        self.assertFalse(self.tab.enabled_switch.isChecked())

    def test_switches(self):
        self.tab.enabled_switch.setChecked(False)
        self.tab.remember_switch.setChecked(False)
        self.assertFalse(self.model.config.enabled)
        self.assertFalse(self.model.config.remember_choices)


class DialogsTabTests(Base):
    @property
    def tab(self):
        return self.view.dialogs

    def test_backends_match_config_and_report_installation(self):
        self.assertEqual([self.tab.combo.itemData(i) for i in range(self.tab.combo.count())],
                         list(configmod.DIALOGS))
        self.assertEqual(self.tab.backends, {"auto": True, "none": True, "qt": True, "zenity": True,
                                             "kdialog": False, "notify": True})
        self.assertIn("not installed", self.tab.combo.itemText(self.tab.combo.findData("kdialog")))
        self.assertEqual(self.tab.badges["kdialog"].text(), "Not installed")
        self.assertEqual(self.tab.badges["zenity"].text(), "Installed")

    def test_availability_helper_is_injectable(self):
        found = advanced_dialogs.installed_backends(lambda name: None, lambda: False)
        self.assertEqual(found, {"auto": True, "none": True, "qt": False, "zenity": False, "kdialog": False,
                                 "notify": False})

    def test_choosing_a_backend_writes_it(self):
        self.tab.combo.setCurrentIndex(self.tab.combo.findData("zenity"))
        self.assertEqual(self.model.config.dialog, "zenity")

    def test_a_missing_backend_is_called_out(self):
        self.assertTrue(self.tab.note.isHidden())
        self.model.update(dialog="kdialog")
        self.assertFalse(self.tab.note.isHidden())
        self.assertIn("not installed", self.tab.note.text())

    def test_triggers_editor(self):
        self.assertEqual(self.tab.triggers.items(), ["steam"])
        self.tab.triggers.input.setText("Heroic Games")
        self.tab.triggers.add_button.click()
        self.assertFalse(self.tab.triggers.error.isHidden())
        self.tab.triggers.input.setText("heroic")
        self.tab.triggers.add_button.click()
        self.assertEqual(self.model.config.triggers, ["steam", "heroic"])

    def test_restart_callout(self):
        self.assertTrue(self.tab.restart_note.isHidden())
        self.model.restart = True
        self.model.changed.emit()
        self.assertFalse(self.tab.restart_note.isHidden())


class ActivityTabTests(Base):
    @property
    def tab(self):
        return self.view.activity

    def test_live_status(self):
        self.assertEqual(self.tab.state_pill.text(), "Game running")
        self.assertEqual(self.tab.values["game"].text(), "Elden Ring (steam:1245620)")
        self.assertEqual(self.tab.values["stopped"].text(), "ollama")
        self.assertEqual(self.tab.values["dialog"].text(), "qt, can ask")
        self.assertEqual(self.tab.values["paused"].text(), "No")
        self.assertEqual(self.tab.values["config"].text(), "Accepted")

    def test_status_variants(self):
        self.model._status = sample_status(state="paused", paused=True, paused_until=float("inf"), game=None,
                                           stopped=[], can_ask=False, config_ok=False, config_error="bad key")
        self.tab.refresh()
        self.assertEqual(self.tab.state_pill.text(), "Paused")
        self.assertEqual(self.tab.values["paused"].text(), "Until resumed")
        self.assertEqual(self.tab.values["game"].text(), "None")
        self.assertEqual(self.tab.values["stopped"].text(), "Nothing")
        self.assertIn("cannot ask", self.tab.values["dialog"].text())
        self.assertEqual(self.tab.values["config"].text(), "Rejected: bad key")
        self.model._status = sample_status(paused=True, paused_until=time.time() + 600)
        self.tab.refresh()
        self.assertTrue(self.tab.values["paused"].text().startswith("Until "))

    def test_service_not_running(self):
        self.model._status = None
        self.tab.refresh()
        self.assertEqual(self.tab.state_pill.text(), "Not running")
        self.assertEqual(self.tab.state_pill.kind(), "danger")

    def test_events_newest_first_with_kind_badges(self):
        table = self.tab.table
        self.assertEqual(table.rowCount(), 5)
        self.assertEqual(table.item(0, 1).text(), "failure")
        self.assertEqual(table.item(0, 1).data(advanced_common.KIND_ROLE), "danger")
        self.assertEqual(table.item(4, 2).text(), "Counter-Strike 2 was already running; left alone")
        self.assertEqual(table.item(1, 1).data(advanced_common.KIND_ROLE), "ok")
        self.assertEqual(table.item(3, 1).data(advanced_common.KIND_ROLE), "warn")
        self.assertTrue(self.tab.empty.isHidden())

    def test_event_kind_colours(self):
        self.assertEqual(advanced_activity.event_kind("error"), "danger")
        self.assertEqual(advanced_activity.event_kind("config-problem"), "danger")
        self.assertEqual(advanced_activity.event_kind("pause"), "warn")
        self.assertEqual(advanced_activity.event_kind("restart"), "ok")
        self.assertEqual(advanced_activity.event_kind("mystery"), "muted")

    def test_refresh_picks_up_new_events_and_status(self):
        self.model._events.append({"t": time.time(), "kind": "restart", "message": "Started ollama again"})
        self.model._status = sample_status(state="watching", stopped=[], game=None)
        self.tab.refresh()
        self.assertEqual(self.tab.table.rowCount(), 6)
        self.assertEqual(self.tab.table.item(0, 2).text(), "Started ollama again")
        self.assertEqual(self.tab.state_pill.text(), "Watching")

    def test_timer_drives_refresh_every_two_seconds(self):
        self.assertEqual(self.tab.timer.interval(), 2000)
        self.model._events.append({"t": time.time(), "kind": "stop", "message": "again"})
        self.tab.timer.timeout.emit()
        self.assertEqual(self.tab.table.rowCount(), 6)

    def test_timer_runs_only_while_the_tab_is_visible(self):
        self.assertFalse(self.tab.timer.isActive())
        self.tab.showEvent(QtGui.QShowEvent())
        self.assertTrue(self.tab.timer.isActive())
        self.tab.hideEvent(QtGui.QHideEvent())
        self.assertFalse(self.tab.timer.isActive())

    def test_no_events(self):
        self.model._events = []
        self.tab.refresh()
        self.assertEqual(self.tab.table.rowCount(), 0)
        self.assertFalse(self.tab.empty.isHidden())


class TestRunTabTests(Base):
    @property
    def tab(self):
        return self.view.test_run

    def run_game(self, game, **options):
        self.tab.game_combo.setEditText(game)
        for key, value in options.items():
            getattr(self.tab, key).setChecked(value) if key == "ask_check" else None
        self.tab.run_button.click()
        return self.tab.last_plan

    def lines(self):
        return "\n".join(self.tab.result_lines)

    def test_managed_candidates_are_ticked_and_listed_with_memory(self):
        self.assertEqual({k: c.isChecked() for k, c in self.tab.checks.items()},
                         {"container:docker:ollama": True, "container:docker:comfyui": True,
                          "container:podman:backup-db": False, "service:system:jellyfin.service": False,
                          "process:blender": False})

    def test_game_combo_is_seeded_from_recent_games_and_takes_free_text(self):
        self.assertEqual(self.tab.game_combo.count(), 2)
        self.tab.game_combo.setCurrentIndex(1)
        self.assertEqual(self.tab.game_id(), "steam:730")
        self.tab.game_combo.setEditText("gog:1207658930")
        self.assertEqual(self.tab.game_id(), "gog:1207658930")
        self.tab.game_combo.setEditText("620")
        self.assertEqual(self.tab.game_id(), "steam:620")

    def test_stop_rule_lists_what_would_be_stopped(self):
        plan = self.run_game("steam:1245620")
        self.assertEqual(plan.action, "stop")
        self.assertEqual([t.name for t in plan.will_stop], ["ollama", "comfyui"])
        text = self.lines()
        self.assertIn("Stop automatically", text)
        self.assertIn("Would be stopped (2, 9.0 GiB", text)
        self.assertIn("ollama: 6.0 GiB", text)
        self.assertIn("its own rule", text)
        self.assertIn("A dialog can be shown.", text)
        self.assertIn("nothing was stopped or started", text)

    def test_ask_rule(self):
        plan = self.run_game("steam:620")
        self.assertEqual(plan.action, "ask")
        self.assertIn("Ask first", self.lines())
        self.assertIn("asks whether to stop", self.lines())

    def test_default_applies_without_a_rule(self):
        plan = self.run_game("steam:999")
        self.assertEqual(plan.action, "ask")
        self.assertIn("the default on_launch", self.lines())
        self.model.update(on_launch="stop")
        self.assertEqual(self.tab.last_plan.action, "stop")

    def test_ignore_rule_leaves_everything_running(self):
        plan = self.run_game("steam:730")
        self.assertEqual(plan.action, "ignore")
        self.assertIn("Do nothing", self.lines())
        self.assertIn("Not stopped (2)", self.lines())
        self.assertIn("the guard does not interfere", self.lines())

    def test_ignored_containers_are_explained(self):
        self.tab.extra_edit.setText("backup-nightly, plain")
        plan = self.run_game("steam:1245620")
        self.assertEqual([t.name for t in plan.ignored], ["backup-nightly"])
        self.assertIn("Left alone (1)", self.lines())
        self.assertIn("matches ignore pattern backup-*", self.lines())
        self.assertEqual([t.name for t in plan.will_stop], ["ollama", "comfyui", "plain"])

    def test_nothing_running(self):
        for check in self.tab.checks.values():
            check.setChecked(False)
        plan = self.run_game("steam:1245620")
        self.assertEqual(plan.action, "nothing-running")
        self.assertIn("Nothing to stop", self.lines())

    def test_paused_and_disabled(self):
        self.model.pause()
        self.assertEqual(self.run_game("steam:1245620").action, "paused")
        self.assertIn("Paused", self.lines())
        self.assertIn("never interrupted", self.lines())
        self.model.resume()
        self.model.update(enabled=False)
        self.assertEqual(self.tab.last_plan.action, "disabled")
        self.assertIn("Switched off", self.lines())

    def test_no_dialog_means_nothing_is_stopped(self):
        self.tab.ask_check.setChecked(False)
        plan = self.run_game("steam:620")
        self.assertFalse(plan.can_ask)
        self.assertEqual(plan.will_stop, [])
        self.assertIn("No dialog can be shown", self.lines())
        self.assertIn("no dialog to ask with", self.lines())

    def test_container_detection_off_is_explained(self):
        self.model.update(auto_containers=False)
        plan = self.run_game("steam:1245620")
        self.assertEqual(plan.action, "nothing-running")
        self.assertIn("container detection is off", self.lines())

    def test_a_game_id_is_required(self):
        self.tab.game_combo.setEditText("")
        self.tab.run_button.click()
        self.assertIsNone(self.tab.last_plan)
        self.assertIn("Enter a game id", self.lines())

    def test_unmanaged_candidates_can_be_added_to_the_scenario(self):
        self.tab.checks["service:system:jellyfin.service"].setChecked(True)
        plan = self.run_game("steam:1245620")
        self.assertIn("jellyfin", [t.name for t in plan.will_stop])

    def test_nothing_is_executed(self):
        calls = []
        for name in ("pause", "resume", "set_config", "update", "set_target", "remove_target", "set_game",
                     "remove_game", "apply", "restart_service"):
            setattr(self.model, name, lambda *a, _n=name, **k: calls.append(_n))
        self.run_game("steam:1245620")
        self.tab.rescan_button.click()
        self.assertEqual(calls, [])

    def test_result_follows_setting_changes_after_a_run(self):
        self.run_game("steam:620")
        self.assertEqual(self.tab.last_plan.action, "ask")
        self.model.set_game("steam:620", "Portal 2", "ignore")
        self.assertEqual(self.tab.last_plan.action, "ignore")

    def test_rescan_asks_the_model_again(self):
        before = self.model.candidate_calls
        self.model._candidates = self.model._candidates[:1]
        self.tab.rescan_button.click()
        self.assertGreater(self.model.candidate_calls, before)
        self.assertEqual(list(self.tab.checks), ["container:docker:ollama"])

    def test_target_from_candidate(self):
        candidates = sample_candidates()
        container = advanced_testrun.target_from_candidate(candidates[2])
        self.assertEqual((container.kind, container.runtime, container.name), ("container", "podman", "backup-db"))
        service = advanced_testrun.target_from_candidate(candidates[3])
        self.assertEqual((service.kind, service.runtime, service.name, service.vram_mib),
                         ("service", "service", "jellyfin", 900))
        self.assertIsInstance(service, Target)


class RawTabTests(Base):
    @property
    def tab(self):
        return self.view.raw

    def test_shows_the_config_json(self):
        self.assertEqual(json.loads(self.tab.text()), self.model.config.to_json())
        self.assertFalse(self.tab.edited())

    def test_invalid_json_never_replaces_the_config(self):
        before = copy.deepcopy(self.model.config)
        self.tab.set_text('{"poll_seconds": ')
        problems = self.tab.apply()
        self.assertTrue(problems[0].startswith("Not valid JSON"))
        self.assertEqual(self.model.config, before)
        self.assertFalse(self.tab.report.isHidden())
        self.assertEqual(self.tab.report.property("kind"), "danger")

    def test_invalid_values_never_replace_the_config(self):
        before = copy.deepcopy(self.model.config)
        data = self.model.config.to_json()
        data["poll_seconds"] = 0.0
        data["on_lauch"] = "ask"
        self.tab.set_text(json.dumps(data))
        problems = self.tab.validate()
        self.assertTrue(any("poll_seconds" in p for p in problems))
        self.assertTrue(any("did you mean 'on_launch'" in p for p in problems))
        self.assertTrue(self.tab.apply())
        self.assertEqual(self.model.config, before)
        self.assertIn("poll_seconds", self.tab.report.label.text())

    def test_a_non_object_is_refused(self):
        self.tab.set_text("[1, 2]")
        self.assertTrue(self.tab.apply())
        self.assertEqual(self.model.config.poll_seconds, 1.0)

    def test_valid_text_replaces_the_working_config(self):
        data = self.model.config.to_json()
        data["poll_seconds"] = 2.5
        data["games"]["steam:10"] = {"name": "Counter-Strike", "action": "stop"}
        self.tab.set_text(json.dumps(data))
        self.assertEqual(self.tab.validate(), [])
        self.assertEqual(self.tab.apply(), [])
        self.assertEqual(self.model.config.poll_seconds, 2.5)
        self.assertEqual(self.model.config.games["steam:10"].action, "stop")
        self.assertFalse(self.tab.edited())
        self.assertEqual(self.view.timing.spins["poll_seconds"].value(), 2.5)

    def test_reset_discards_edits(self):
        self.tab.set_text("garbage")
        self.assertTrue(self.tab.edited())
        self.tab.reset_button.click()
        self.assertEqual(json.loads(self.tab.text()), self.model.config.to_json())

    def test_unedited_editor_follows_the_model(self):
        self.model.update(poll_seconds=3.0)
        self.assertEqual(json.loads(self.tab.text())["poll_seconds"], 3.0)

    def test_an_edited_editor_is_kept_when_the_model_changes(self):
        self.tab.set_text('{"poll_seconds": 9}')
        self.model.update(poll_seconds=3.0)
        self.assertEqual(self.tab.text(), '{"poll_seconds": 9}')
        self.assertIn("changed elsewhere", self.tab.report.label.text())

    def test_import_loads_into_the_editor_without_applying(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.json"
            data = self.model.config.to_json()
            data["heavy_vram_mib"] = 1000
            path.write_text(json.dumps(data), encoding="utf-8")
            self.tab.pick_open = lambda parent: str(path)
            self.tab.import_button.click()
        self.assertEqual(json.loads(self.tab.text())["heavy_vram_mib"], 1000)
        self.assertEqual(self.model.config.heavy_vram_mib, 4096)
        self.assertIn("press Apply", self.tab.report.label.text())
        self.assertEqual(self.tab.apply(), [])
        self.assertEqual(self.model.config.heavy_vram_mib, 1000)

    def test_import_of_a_bad_file_reports_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.json"
            path.write_text("{nope", encoding="utf-8")
            self.tab.pick_open = lambda parent: str(path)
            self.tab.import_button.click()
            self.tab.pick_open = lambda parent: str(Path(tmp) / "missing.json")
            self.tab.import_button.click()
        self.assertIn("Cannot read", self.tab.report.label.text())
        self.assertEqual(self.model.config.heavy_vram_mib, 4096)

    def test_import_cancelled(self):
        self.tab.pick_open = lambda parent: ""
        before = self.tab.text()
        self.tab.import_button.click()
        self.assertEqual(self.tab.text(), before)

    def test_export_writes_the_editor_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            suggested = []
            self.tab.pick_save = lambda parent, name: suggested.append(name) or str(path)
            self.tab.export_button.click()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), self.model.config.to_json())
        self.assertEqual(suggested, ["gpu-game-guard-config.json"])

    def test_export_refuses_invalid_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            self.tab.pick_save = lambda parent, name: str(path)
            self.tab.set_text("{")
            self.tab.export_button.click()
            self.assertFalse(path.exists())
        self.assertIn("Fix the problems", self.tab.report.label.text())

    def test_view_hooks_are_injectable(self):
        opener, saver = (lambda p: "a"), (lambda p, n: "b")
        view = advanced.AdvancedView(self.model, which=lambda n: None, polkit_status=lambda u: None,
                                     file_dialogs=(opener, saver), defer_discovery=False)
        self.assertIs(view.raw.pick_open, opener)
        self.assertIs(view.raw.pick_save, saver)


@unittest.skipUnless(qt.available() and settings_model is not None, "PyQt5 or the settings model is missing")
class RealModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()
        theme.apply_theme(cls.app, dark=False)

    def tearDown(self):
        release(self.view)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.json"
        self.commands = []
        configmod.save(sample_config(), self.path)
        self.model = settings_model.SettingsModel(
            path=self.path, status_dir=self.root / "state", discover=lambda cfg: sample_candidates(),
            runner=lambda argv: self.commands.append(argv) or (0, ""))
        self.view = advanced.AdvancedView(self.model, which=lambda n: None, polkit_status=lambda u: None,
                                          qt_available=lambda: True, defer_discovery=False)

    def test_tabs_show_the_loaded_config(self):
        self.assertEqual(self.view.targets.table.rowCount(), 5)
        self.assertEqual(self.view.games.table.rowCount(), 3)
        self.assertEqual(self.view.detection.ignore_editor.items(), ["backup-*"])

    def test_edits_mark_the_model_dirty_and_apply_saves_them(self):
        self.view.games.combos["steam:620"].setCurrentText("stop")
        self.view.timing.spins["poll_seconds"].setValue(2.0)
        self.assertTrue(self.model.dirty)
        self.assertEqual(self.model.apply(), [])
        saved = configmod.load(self.path)
        self.assertEqual(saved.games["steam:620"].action, "stop")
        self.assertEqual(saved.poll_seconds, 2.0)

    def test_raw_apply_and_a_target_added_through_a_dialog(self):
        self.view.targets.run_dialog = fill_and_accept(label_edit="Unload", stop_edit="true", start_edit="true")
        self.view.targets.command_button.click()
        self.assertEqual(self.model.apply(), [])
        self.assertEqual(configmod.load(self.path).target("unload").stop, ["true"])
        data = json.loads(self.view.raw.text())
        data["dialog"] = "zenity"
        self.view.raw.set_text(json.dumps(data))
        self.assertEqual(self.view.raw.apply(), [])
        self.assertEqual(self.model.config.dialog, "zenity")

    def test_restart_callout_after_saving_a_runtime_change(self):
        self.view.detection.runtime_list.item(1).setCheckState(QtCore.Qt.Unchecked)
        self.model.apply()
        self.assertFalse(self.view.detection.restart_note.isHidden())
        self.view.detection.restart_note.button.click()
        self.assertEqual(len(self.commands), 1)
        self.assertTrue(self.view.detection.restart_note.isHidden())

    def test_test_run_and_activity_use_the_real_model(self):
        self.view.test_run.game_combo.setEditText("steam:1245620")
        self.view.test_run.run_button.click()
        self.assertEqual(self.view.test_run.last_plan.action, "stop")
        self.assertEqual(self.view.activity.state_pill.text(), "Not running")
        self.assertEqual(self.view.activity.table.rowCount(), 0)

    def test_a_missing_config_file_gives_defaults_everywhere(self):
        model = settings_model.SettingsModel(path=self.root / "none.json", status_dir=self.root / "state2",
                                             discover=lambda cfg: [])
        view = advanced.AdvancedView(model, which=lambda n: None, polkit_status=lambda u: None,
                                     defer_discovery=False)
        self.assertEqual(view.targets.table.rowCount(), 0)
        self.assertEqual(view.games.table.rowCount(), 0)
        self.assertEqual(json.loads(view.raw.text()), Config().to_json())


if __name__ == "__main__":
    unittest.main()
