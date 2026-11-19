from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from gpu_game_guard import config as configmod  # noqa: E402
from gpu_game_guard import status as statusmod  # noqa: E402
from gpu_game_guard.config import TargetSpec  # noqa: E402
from gpu_game_guard.discovery import Candidate  # noqa: E402
from gpu_game_guard.ui import qt  # noqa: E402

PAYLOAD = '<b>x</b><img src="file:///etc/passwd"><a href="x">y</a>'

if qt.available():
    from gpu_game_guard.ui import basic, settings_model, theme
    from gpu_game_guard.ui.settings_model import SettingsModel

    QtCore, QtWidgets = qt.QtCore, qt.QtWidgets


def container(name, image="", vram=None):
    detail = "docker container" + (f", image {image}" if image else "")
    return Candidate(f"container:docker:{name}", "container", name, name, detail, vram, True, "low", None, "")


def service(unit, vram=None, scope="system"):
    stem = unit[:-len(".service")]
    spec = {"id": stem, "kind": "service", "label": stem, "unit": unit, "scope": scope}
    return Candidate(f"service:{scope}:{unit}", "service", unit, stem, f"{scope} service {unit}", vram, False,
                     "low", spec, "")


def process(name):
    return Candidate(f"process:{name}", "process", name, name, "1 process", 100, False, "high",
                     {"id": name, "kind": "process", "label": name, "match": name, "restart": True}, "")


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class BasicViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()
        theme.apply_theme(cls.app, dark=False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.status_dir = self.dir / "state"
        self.found = [container("open-webui", "ghcr.io/open-webui/open-webui:main", 2150),
                      container("comfyui"), container("jellyfin", "jellyfin/jellyfin", 0),
                      service("ollama.service", 6400), process("obs")]
        self.discoveries = 0
        self.model = SettingsModel(self.dir / "config.json", self.status_dir, discover=self.discover,
                                   runner=lambda argv: (0, ""), threaded=False)
        self.view = basic.BasicView(self.model, autoload=False)
        self.addCleanup(self.view.deleteLater)

    def discover(self, config):
        self.discoveries += 1
        return list(self.found)

    def watching(self):
        statusmod.write_status(statusmod.Status(updated=time.time()), self.status_dir)

    def rows(self):
        return {row.item.name: row for row in self.view._rows}

    def test_nothing_is_discovered_until_load(self):
        self.assertEqual(self.discoveries, 0)
        self.assertIn("Scanning", self.view.empty_label.text())
        self.view.load()
        self.assertEqual(self.discoveries, 1)

    def test_autoload_runs_from_the_event_loop(self):
        view = basic.BasicView(self.model)
        self.addCleanup(view.deleteLater)
        self.assertEqual(self.discoveries, 0)
        self.app.processEvents()
        self.assertEqual(self.discoveries, 1)

    def test_rows_hide_processes_and_show_names_subtitles_and_memory(self):
        self.view.load()
        rows = self.rows()
        self.assertEqual(list(rows), ["open-webui", "comfyui", "jellyfin", "ollama.service"])
        self.assertEqual(rows["open-webui"].title_label.text(), "Open Webui")
        self.assertEqual(rows["open-webui"].subtitle_label.text(), "ghcr.io/open-webui/open-webui:main")
        self.assertEqual(rows["open-webui"].badge.text(), "2.1 GiB")
        self.assertEqual(rows["comfyui"].badge.text(), "unknown")
        self.assertEqual(rows["comfyui"].subtitle_label.text(), "docker container")
        self.assertEqual(rows["jellyfin"].badge.text(), "none measured")
        self.assertEqual(rows["ollama.service"].title_label.text(), "Ollama")
        self.assertEqual(rows["ollama.service"].subtitle_label.text(), "ollama.service")
        self.assertEqual(rows["ollama.service"].badge.text(), "6.2 GiB")

    def test_memory_text(self):
        self.assertEqual(basic.format_memory(512), "512 MiB")
        self.assertEqual(basic.format_memory(1024), "1.0 GiB")
        self.assertEqual(basic.format_memory(0), "none measured")
        self.assertEqual(basic.format_memory(None), "unknown")

    def test_friendly_names(self):
        self.assertEqual(basic.friendly_name("ollama.service"), "Ollama")
        self.assertEqual(basic.friendly_name("open_webui"), "Open Webui")
        self.assertEqual(basic.friendly_name("ComfyUI"), "ComfyUI")
        self.assertEqual(basic.friendly_name("redis6"), "redis6")
        self.assertEqual(basic.friendly_name("---"), "---")

    def test_container_toggle_off_and_on_edits_ignore_containers(self):
        self.view.load()
        switch = self.rows()["open-webui"].switch
        self.assertTrue(switch.isChecked())
        switch.setChecked(False)
        self.assertEqual(self.model.config.ignore_containers, ["open-webui"])
        switch.setChecked(True)
        self.assertEqual(self.model.config.ignore_containers, [])
        self.assertFalse(self.model.dirty)

    def test_toggle_keeps_other_ignore_entries_and_does_not_duplicate(self):
        self.model.update(ignore_containers=["other", "comfyui"])
        self.view.load()
        self.assertFalse(self.rows()["comfyui"].switch.isChecked())
        self.rows()["open-webui"].switch.setChecked(False)
        self.assertEqual(self.model.config.ignore_containers, ["other", "comfyui", "open-webui"])
        self.rows()["comfyui"].switch.setChecked(True)
        self.assertEqual(self.model.config.ignore_containers, ["other", "open-webui"])

    def test_pattern_ignored_container_is_locked(self):
        self.model.update(ignore_containers=["open-*"])
        self.view.load()
        row = self.rows()["open-webui"]
        self.assertFalse(row.switch.isChecked())
        self.assertFalse(row.switch.isEnabled())
        self.assertTrue(row.switch.toolTip())

    def test_containers_cannot_be_switched_on_while_detection_is_off(self):
        self.model.update(auto_containers=False)
        self.view.load()
        row = self.rows()["comfyui"]
        self.assertFalse(row.switch.isChecked())
        self.assertFalse(row.switch.isEnabled())
        self.assertIn("Automatic detection", row.switch.toolTip())
        self.assertFalse(self.view.auto_button.isHidden())
        self.view.auto_button.click()
        self.assertTrue(self.model.config.auto_containers)
        self.assertTrue(self.view.auto_button.isHidden())
        self.assertTrue(self.rows()["comfyui"].switch.isEnabled())
        self.assertTrue(self.rows()["comfyui"].switch.isChecked())

    def test_the_automatic_detection_button_is_hidden_while_detection_is_on(self):
        self.view.load()
        self.assertTrue(self.view.auto_button.isHidden())

    def test_service_toggle_adds_and_removes_a_target(self):
        self.view.load()
        switch = self.rows()["ollama.service"].switch
        self.assertFalse(switch.isChecked())
        switch.setChecked(True)
        spec = self.model.config.target("ollama")
        self.assertEqual((spec.kind, spec.unit, spec.scope, spec.label, spec.enabled),
                         ("service", "ollama.service", "system", "Ollama", True))
        self.assertEqual(configmod.validate(self.model.config.to_json()), [])
        switch.setChecked(False)
        self.assertEqual([(t.id, t.enabled) for t in self.model.config.targets], [("ollama", False)])
        self.assertFalse(self.rows()["ollama.service"].switch.isChecked())

    def test_switching_a_service_off_keeps_a_hand_made_spec(self):
        spec = TargetSpec(id="llm", kind="service", label="My LLM", unit="ollama.service", extra={"note": "keep"})
        twin = TargetSpec(id="llm-2", kind="service", label="My LLM 2", unit="ollama.service")
        self.model.set_target(spec)
        self.model.set_target(twin)
        self.view.load()
        switch = self.rows()["ollama.service"].switch
        self.assertTrue(switch.isChecked())
        switch.setChecked(False)
        self.assertEqual([(t.id, t.label, t.enabled) for t in self.model.config.targets],
                         [("llm", "My LLM", False), ("llm-2", "My LLM 2", False)])
        self.assertEqual(self.model.config.target("llm").extra, {"note": "keep"})
        switch.setChecked(True)
        self.assertEqual([(t.id, t.enabled) for t in self.model.config.targets], [("llm", True), ("llm-2", True)])

    def test_a_service_with_one_enabled_spec_of_several_shows_as_on(self):
        self.model.set_target(TargetSpec(id="a", kind="service", label="A", unit="ollama.service", enabled=False))
        self.model.set_target(TargetSpec(id="b", kind="service", label="B", unit="ollama.service"))
        self.view.load()
        self.assertTrue(self.rows()["ollama.service"].switch.isChecked())

    def test_service_toggle_picks_an_unused_id(self):
        self.model.set_target(TargetSpec(id="ollama", kind="command", stop=["a"], start=["b"]))
        self.view.load()
        self.rows()["ollama.service"].switch.setChecked(True)
        self.assertEqual([t.id for t in self.model.config.targets], ["ollama", "ollama-2"])
        self.assertEqual(self.model.problems, [])

    def test_user_scope_service_keeps_its_scope(self):
        self.found = [service("ollama.service", 100, "user")]
        self.view.load()
        row = self.rows()["ollama.service"]
        self.assertIn("user service", row.subtitle_label.text())
        row.switch.setChecked(True)
        self.assertEqual(self.model.config.target("ollama").scope, "user")

    def test_existing_disabled_target_is_reenabled_not_duplicated(self):
        self.model.set_target(TargetSpec(id="llm", kind="service", unit="ollama.service", enabled=False))
        self.view.load()
        switch = self.rows()["ollama.service"].switch
        self.assertFalse(switch.isChecked())
        switch.setChecked(True)
        self.assertEqual([(t.id, t.enabled) for t in self.model.config.targets], [("llm", True)])

    def test_configured_service_that_is_idle_still_gets_a_row(self):
        self.found = []
        self.model.set_target(TargetSpec(id="ollama", kind="service", label="Ollama", unit="ollama.service"))
        self.view.load()
        row = self.rows()["ollama.service"]
        self.assertEqual(row.badge.text(), "idle")
        self.assertTrue(row.switch.isChecked())
        row.switch.setChecked(False)
        self.assertEqual([(t.id, t.enabled) for t in self.model.config.targets], [("ollama", False)])
        self.assertFalse(self.rows()["ollama.service"].switch.isChecked())

    def test_rows_follow_changes_made_elsewhere(self):
        self.view.load()
        self.model.update(ignore_containers=["comfyui"])
        self.assertFalse(self.rows()["comfyui"].switch.isChecked())
        self.model.revert()
        self.assertTrue(self.rows()["comfyui"].switch.isChecked())

    def test_toggling_does_not_rebuild_rows(self):
        self.view.load()
        first = self.rows()["comfyui"]
        first.switch.setChecked(False)
        self.assertIs(self.rows()["comfyui"], first)

    def test_empty_state_and_refresh(self):
        self.found = []
        self.view.load()
        self.assertFalse(self.view.empty_label.isHidden())
        self.assertEqual(self.view.empty_label.text(),
                         "Nothing is using the GPU right now. Start your apps and press Refresh.")
        self.found = [container("comfyui")]
        self.view.refresh_button.click()
        self.assertEqual(self.discoveries, 2)
        self.assertTrue(self.view.empty_label.isHidden())
        self.assertEqual(list(self.rows()), ["comfyui"])

    def test_only_processes_gives_the_empty_state(self):
        self.found = [process("obs")]
        self.view.load()
        self.assertFalse(self.view.empty_label.isHidden())

    def test_option_cards_reflect_and_change_the_config(self):
        self.assertEqual(self.view.on_launch.value(), "ask")
        self.view.on_launch.cards["stop"].click()
        self.assertEqual(self.model.config.on_launch, "stop")
        self.view.on_launch.cards["ignore"].click()
        self.assertEqual(self.model.config.on_launch, "ignore")
        self.view.after_game.cards["restart"].click()
        self.assertEqual(self.model.config.after_game, "restart")
        self.view.after_game.cards["leave"].click()
        self.assertEqual(self.model.config.after_game, "leave")
        self.assertTrue(self.model.dirty)

    def test_cards_follow_the_model(self):
        self.model.update(on_launch="ignore", after_game="leave")
        self.assertEqual((self.view.on_launch.value(), self.view.after_game.value()), ("ignore", "leave"))
        self.model.revert()
        self.assertEqual((self.view.on_launch.value(), self.view.after_game.value()), ("ask", "ask"))

    def test_cards_are_exclusive_and_cover_every_choice(self):
        self.assertEqual(list(self.view.on_launch.cards), list(basic.ON_LAUNCH))
        self.assertEqual(list(self.view.after_game.cards), list(basic.AFTER_GAME))
        self.view.on_launch.cards["stop"].click()
        self.assertEqual([c.isChecked() for c in self.view.on_launch.cards.values()], [False, True, False])

    def test_card_titles(self):
        self.assertEqual([c.title_label.text() for c in self.view.on_launch.cards.values()],
                         ["Ask me", "Stop them automatically", "Do nothing"])
        self.assertEqual([c.title_label.text() for c in self.view.after_game.cards.values()],
                         ["Ask me", "Start them again automatically", "Leave them stopped"])

    def test_status_not_running(self):
        self.view.load()
        self.assertEqual(self.view.headline.text(), "GPU Game Guard is not running")
        self.assertTrue(self.view.button_row.isHidden())

    def test_status_watching_counts_active_things(self):
        self.watching()
        self.view.load()
        self.assertEqual(self.view.headline.text(), "GPU Game Guard is watching")
        self.assertEqual(self.view.detail.text(), "Ready to pause 3 things when a game starts.")
        self.rows()["comfyui"].switch.setChecked(False)
        self.rows()["jellyfin"].switch.setChecked(False)
        self.assertEqual(self.view.detail.text(), "Ready to pause 1 thing when a game starts.")
        self.rows()["open-webui"].switch.setChecked(False)
        self.assertEqual(self.view.detail.text(), "Nothing is set to be paused right now.")

    def test_command_and_process_targets_count_as_set_to_be_paused(self):
        self.found = []
        self.watching()
        self.model.set_target(TargetSpec(id="job", kind="command", stop=["a"], start=["b"]))
        self.model.set_target(TargetSpec(id="obs", kind="process", match="obs"))
        self.view.load()
        self.assertEqual(self.view.detail.text(), "Ready to pause 2 things when a game starts.")
        self.model.set_target(TargetSpec(id="job", kind="command", stop=["a"], start=["b"], enabled=False))
        self.assertEqual(self.view.detail.text(), "Ready to pause 1 thing when a game starts.")
        self.model.set_target(TargetSpec(id="obs", kind="process", match="obs", enabled=False))
        self.assertEqual(self.view.detail.text(), "Nothing is set to be paused right now.")

    def test_the_card_and_the_header_pill_agree_about_a_pending_restart(self):
        self.watching()
        self.model.update(dialog="none")
        self.model.apply()
        self.view.refresh_status()
        self.assertEqual(self.view.state, "restart-pending")
        self.assertEqual(self.view.headline.text(), "GPU Game Guard needs a restart")
        self.assertEqual(settings_model.service_state(self.model)[0], settings_model.STATE_RESTART)
        self.model.pause()
        self.view.refresh_status()
        self.assertEqual(self.view.state, "paused")
        self.assertEqual(settings_model.service_state(self.model)[0], settings_model.STATE_PAUSED)

    def test_status_buttons_when_watching(self):
        self.watching()
        self.view.refresh_status()
        self.assertFalse(self.view.button_row.isHidden())
        self.assertFalse(self.view.pause_hour.isHidden())
        self.assertFalse(self.view.pause_until_resumed.isHidden())
        self.assertTrue(self.view.resume_button.isHidden())

    def test_pause_one_hour_and_resume(self):
        self.watching()
        self.view.pause_hour.click()
        paused, until = self.model.paused()
        self.assertTrue(paused)
        self.assertAlmostEqual(until, time.time() + 3600, delta=30)
        self.assertEqual(self.view.headline.text(),
                         f"GPU Game Guard is paused until {basic.clock_text(until)}")
        self.assertRegex(basic.clock_text(until), r"^(\w{3} )?\d\d:\d\d$")
        self.assertFalse(self.view.resume_button.isHidden())
        self.assertTrue(self.view.pause_hour.isHidden())
        self.view.resume_button.click()
        self.assertEqual(self.model.paused(), (False, None))
        self.assertEqual(self.view.headline.text(), "GPU Game Guard is watching")

    def test_pause_until_resumed(self):
        self.watching()
        self.view.pause_until_resumed.click()
        self.assertEqual(self.model.paused(), (True, None))
        self.assertEqual(self.view.headline.text(), "GPU Game Guard is paused until you resume it")

    def test_switched_off_offers_to_switch_on(self):
        self.model.update(enabled=False)
        self.model.apply()
        self.watching()
        self.view.refresh_status()
        self.assertEqual(self.view.headline.text(), "GPU Game Guard is switched off")
        self.assertFalse(self.view.switch_on.isHidden())
        self.assertTrue(self.view.pause_hour.isHidden())
        self.view.switch_on.click()
        self.assertTrue(self.model.config.enabled)

    def test_status_lists_what_is_stopped_for_the_running_game(self):
        statusmod.write_status(statusmod.Status(
            updated=time.time(), state="game-running",
            stopped=[{"kind": "container", "runtime": "docker", "name": "open-webui"}]), self.status_dir)
        self.view.refresh_status()
        self.assertEqual(self.view.detail.text(), "Paused for the current game: Open Webui.")

    def test_clock_text(self):
        now = time.mktime((2026, 11, 18, 12, 0, 0, 0, 0, -1))
        self.assertEqual(basic.clock_text(time.mktime((2026, 11, 18, 15, 40, 0, 0, 0, -1)), now), "15:40")
        self.assertEqual(basic.clock_text(time.mktime((2026, 11, 18, 9, 5, 0, 0, 0, -1)), now), "09:05")
        self.assertEqual(basic.clock_text(time.mktime((2026, 11, 19, 9, 5, 0, 0, 0, -1)), now), "Thu 09:05")

    def test_clock_text_survives_values_no_clock_can_show(self):
        for value in (1e18, 1e30, float("inf"), float("nan"), 253402300800.0 * 10):
            self.assertEqual(basic.clock_text(value), "until resumed", value)

    def test_a_pause_that_ends_beyond_year_9999_reads_as_until_resumed(self):
        self.watching()
        statusmod.pause(1e12, self.status_dir)
        self.view.refresh_status()
        self.assertEqual(self.view.headline.text(), "GPU Game Guard is paused until you resume it")

    def test_slug_ids(self):
        self.assertEqual(basic.slug_id("My App@1.service", []), "my-app-1")
        self.assertEqual(basic.slug_id("ollama.service", ["ollama", "ollama-2"]), "ollama-3")
        self.assertEqual(len(basic.slug_id("x" * 80 + ".service", [])), 40)
        self.assertTrue(basic.slug_id("###.service", []).startswith("service-"))
        self.assertNotEqual(basic.slug_id("###.service", []), basic.slug_id("%%%.service", []))

    def test_a_new_service_label_never_repeats_an_existing_one(self):
        targets = [TargetSpec(id="a", kind="command", label="Ollama", stop=["x"], start=["y"]),
                   TargetSpec(id="b", kind="service", label="Ollama (2)", unit="b.service")]
        self.assertEqual(basic.unique_label("ollama", targets), "ollama (3)")
        self.assertEqual(basic.unique_label("Other", targets), "Other")

    def test_footnote_link_requests_advanced(self):
        seen = []
        self.view.advancedRequested.connect(lambda: seen.append(1))
        self.view.footnote.linkActivated.emit("advanced")
        self.assertEqual(seen, [1])

    def test_switches_have_accessible_names(self):
        self.view.load()
        self.assertEqual(self.rows()["comfyui"].switch.accessibleName(), "Pause Comfyui while gaming")

    def test_the_status_card_and_rows_show_markup_literally(self):
        payload = PAYLOAD
        self.found = [container(payload, payload, 10)]
        statusmod.write_status(statusmod.Status(
            updated=time.time(), state="game-running",
            stopped=[{"kind": "container", "runtime": "docker", "name": payload}]), self.status_dir)
        self.view.load()
        self.view.refresh_status()
        for label in self.view.findChildren(QtWidgets.QLabel):
            if label is not self.view.footnote and QtCore.Qt.mightBeRichText(label.text()):
                self.assertEqual(label.textFormat(), QtCore.Qt.PlainText, label.text())
        self.assertIn("<b>", self.view.detail.text())
        row = list(self.rows().values())[0]
        self.assertEqual(row.title_label.textFormat(), QtCore.Qt.PlainText)

    def test_a_failed_scan_is_not_shown_as_an_idle_gpu(self):
        def broken(config):
            raise RuntimeError("nvidia-smi is gone")

        model = SettingsModel(self.dir / "other.json", self.status_dir, discover=broken, runner=lambda argv: (0, ""),
                              threaded=False)
        view = basic.BasicView(model, autoload=False)
        self.addCleanup(view.deleteLater)
        view.load()
        self.assertFalse(view.empty_label.isHidden())
        self.assertIn("Could not check what is using the graphics card", view.empty_label.text())
        self.assertNotIn("Nothing is using", view.empty_label.text())
        self.assertTrue(view.refresh_button.isEnabled())
        self.assertEqual(view.refresh_button.text(), "Refresh")
        view.model._discover = lambda config: []
        view.refresh_button.click()
        self.assertIn("Nothing is using the GPU", view.empty_label.text())

    def test_the_load_is_in_the_background_and_lands_on_the_ui_thread(self):
        gate = threading.Event()
        threads = []

        def slow(config):
            threads.append(threading.current_thread())
            gate.wait(5)
            return [container("comfyui")]

        model = SettingsModel(self.dir / "other.json", self.status_dir, discover=slow, runner=lambda argv: (0, ""))
        view = basic.BasicView(model, autoload=False)
        self.addCleanup(view.deleteLater)
        self.addCleanup(model.shutdown)
        view.load()
        self.assertTrue(model.scanning)
        self.assertEqual(view.empty_label.text(), "Scanning...")
        self.assertFalse(view.refresh_button.isEnabled())
        gate.set()
        self.assertTrue(model.wait_scan(5))
        self.assertIsNot(threads[0], threading.main_thread())
        self.assertFalse(model.scanning)
        self.assertEqual([row.item.name for row in view._rows], ["comfyui"])
        self.assertTrue(view.refresh_button.isEnabled())

    def test_renders_populated_and_empty_in_both_themes(self):
        self.watching()
        self.view.load()
        for dark in (False, True):
            theme.apply_theme(self.app, dark=dark)
            out = self.dir / f"basic-{dark}.png"
            self.assertTrue(theme.render(self.view, str(out)))
            self.assertGreater(out.stat().st_size, 1000)
        theme.apply_theme(self.app, dark=False)


if __name__ == "__main__":
    unittest.main()
