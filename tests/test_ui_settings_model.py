"""The settings model against real files in a temporary directory: what it keeps, merges, backs up and reports."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from gpu_game_guard import config as configmod  # noqa: E402
from gpu_game_guard.config import Config, GameRule, TargetSpec  # noqa: E402
from gpu_game_guard.discovery import Candidate  # noqa: E402
from gpu_game_guard.ui import qt  # noqa: E402

if qt.available():
    from gpu_game_guard.ui.settings_model import SettingsModel


def candidate(name):
    return Candidate(f"container:docker:{name}", "container", name, name, "", 1, True, "low", None, "")


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class RealModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "config.json"
        self.found = [candidate("ollama")]

    def model(self, threaded=False, **kwargs):
        model = SettingsModel(self.path, self.dir / "state", discover=lambda cfg: list(self.found),
                              runner=lambda argv: (0, ""), threaded=threaded, **kwargs)
        self.addCleanup(model.shutdown)
        return model

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def external_edit(self, mutate):
        configmod.update_file(mutate, self.path)

    # keys this version does not know

    def test_unknown_keys_survive_an_edit_and_a_save(self):
        self.write({
            "future_top": {"a": [1, 2]},
            "games": {"steam:1": {"name": "One", "action": "stop", "future_rule": True}},
            "targets": [{"id": "svc", "kind": "service", "unit": "x.service", "future_target": "keep"}],
        })
        model = self.model()
        model.update(on_launch="stop")
        model.set_game("steam:2", "Two", "ignore")
        self.assertEqual(model.apply(), [])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["future_top"], {"a": [1, 2]})
        self.assertTrue(saved["games"]["steam:1"]["future_rule"])
        self.assertEqual(saved["targets"][0]["future_target"], "keep")
        self.assertEqual(saved["on_launch"], "stop")

    def test_the_raw_editor_text_keeps_unknown_keys_too(self):
        self.write({"future_top": 1})
        model = self.model()
        self.assertEqual(model.config.to_json()["future_top"], 1)

    # dirty

    def test_dirty_is_measured_against_the_file_as_it_was_loaded(self):
        model = self.model()
        self.assertFalse(model.dirty)
        model.update(poll_seconds=3.0)
        self.assertTrue(model.dirty)
        model.update(poll_seconds=1.0)
        self.assertFalse(model.dirty)

    def test_an_unedited_model_follows_an_external_change_without_becoming_dirty(self):
        model = self.model()
        self.external_edit(lambda c: Config.from_json({**c.to_json(), "on_launch": "stop"}))
        self.assertTrue(model.sync_with_disk())
        self.assertEqual(model.config.on_launch, "stop")
        self.assertFalse(model.dirty)
        self.assertFalse(model.sync_with_disk())

    def test_a_model_with_edits_keeps_them_when_the_file_changes(self):
        model = self.model()
        model.update(after_game="leave")
        self.external_edit(lambda c: Config.from_json({**c.to_json(), "on_launch": "stop"}))
        self.assertFalse(model.sync_with_disk())
        self.assertEqual(model.config.on_launch, "ask")
        self.assertTrue(model.dirty)

    def test_revert_reads_the_file_again(self):
        model = self.model()
        model.update(after_game="leave")
        self.external_edit(lambda c: Config.from_json({**c.to_json(), "on_launch": "stop"}))
        model.revert()
        self.assertEqual((model.config.on_launch, model.config.after_game), ("stop", "ask"))
        self.assertFalse(model.dirty)

    # concurrent writers

    def test_an_edit_elsewhere_to_another_setting_is_merged_not_overwritten(self):
        model = self.model()
        model.update(after_game="leave")
        model.set_game("steam:5", "Five", "stop")
        self.external_edit(lambda c: c.with_game("steam:9", "Nine", "ignore"))
        self.external_edit(lambda c: Config.from_json({**c.to_json(), "on_launch": "stop", "future": 1}))
        self.assertEqual(model.apply(), [])
        saved = configmod.load(self.path)
        self.assertEqual((saved.on_launch, saved.after_game), ("stop", "leave"))
        self.assertEqual(sorted(saved.games), ["steam:5", "steam:9"])
        self.assertEqual(saved.extra, {"future": 1})
        self.assertEqual(model.notices, ["The configuration changed on disk; your edits were merged."])
        self.assertEqual(model.config, saved)
        self.assertFalse(model.dirty)

    def test_a_target_added_elsewhere_survives_our_save(self):
        model = self.model()
        model.update(on_launch="stop")
        self.external_edit(lambda c: c.with_target(TargetSpec(id="job", kind="process", match="job")))
        self.assertEqual(model.apply(), [])
        self.assertEqual([t.id for t in configmod.load(self.path).targets], ["job"])

    def test_our_value_wins_when_both_changed_the_same_setting(self):
        model = self.model()
        model.update(on_launch="ignore")
        self.external_edit(lambda c: Config.from_json({**c.to_json(), "on_launch": "stop"}))
        self.assertEqual(model.apply(), [])
        self.assertEqual(configmod.load(self.path).on_launch, "ignore")

    def test_a_plain_save_reports_no_merge(self):
        model = self.model()
        model.update(on_launch="stop")
        self.assertEqual(model.apply(), [])
        self.assertEqual(model.notices, [])

    def test_two_saves_in_a_row_do_not_report_a_merge(self):
        model = self.model()
        model.update(on_launch="stop")
        model.apply()
        model.update(on_launch="ignore")
        self.assertEqual(model.apply(), [])
        self.assertEqual(model.notices, [])

    def test_a_file_that_broke_meanwhile_is_left_alone(self):
        model = self.model()
        model.update(on_launch="stop")
        self.path.write_text("{ broken", encoding="utf-8")
        problems = model.apply()
        self.assertEqual(len(problems), 1)
        self.assertIn("could not be saved", problems[0])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ broken")
        self.assertTrue(model.dirty)

    # backups

    def test_saving_keeps_the_previous_file_as_a_backup(self):
        self.write({"on_launch": "ignore"})
        model = self.model()
        model.update(on_launch="stop")
        model.apply()
        backup = self.path.with_name("config.json.bak")
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["on_launch"], "ignore")

    # a file that cannot be loaded

    def test_a_bad_value_leaves_the_file_untouched_and_lists_every_problem(self):
        text = json.dumps({"on_launch": "sometimes", "heavy_vram_mib": -4,
                           "games": {"steam:1": {"name": "Precious", "action": "stop"}}})
        self.path.write_text(text, encoding="utf-8")
        model = self.model()
        self.assertTrue(model.load_error)
        self.assertEqual(len(model.load_problems), 2)
        self.assertTrue(any("on_launch" in p for p in model.load_problems))
        self.assertTrue(any("heavy_vram_mib" in p for p in model.load_problems))
        self.assertEqual(model.raw_file_text, text)
        model.update(on_launch="stop")
        self.assertTrue(model.apply())
        self.assertEqual(self.path.read_text(encoding="utf-8"), text)

    def test_repairing_the_file_text_keeps_the_games_and_clears_the_error(self):
        data = {"on_launch": "sometimes", "games": {"steam:1": {"name": "Precious", "action": "stop"}}}
        self.write(data)
        model = self.model()
        data["on_launch"] = "ask"
        model.set_config(Config.from_json(data))
        self.assertEqual(model.load_error, "")
        self.assertTrue(model.dirty)
        self.assertEqual(model.apply(), [])
        self.assertEqual(configmod.load(self.path).games["steam:1"].name, "Precious")

    def test_starting_from_the_defaults_keeps_a_broken_copy_and_says_where(self):
        self.path.write_text("{ not json", encoding="utf-8")
        model = self.model()
        self.assertEqual(model.load_problems[0][:20], "is not valid JSON: E"[:20])
        model.accept_defaults()
        self.assertTrue(model.defaults_pending)
        self.assertEqual(model.apply(), [])
        copies = sorted(self.dir.glob("config.json.broken-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(encoding="utf-8"), "{ not json")
        self.assertIn(str(copies[0]), model.notices[0])
        self.assertEqual(configmod.load(self.path), Config())
        self.assertFalse(model.defaults_pending)

    def test_a_file_repaired_elsewhere_is_merged_instead_of_replaced(self):
        self.path.write_text("{ not json", encoding="utf-8")
        model = self.model()
        model.accept_defaults()
        model.update(after_game="leave")
        self.write({"on_launch": "stop", "games": {"steam:1": {"name": "One", "action": "ask"}}})
        self.assertEqual(model.apply(), [])
        saved = configmod.load(self.path)
        self.assertEqual((saved.on_launch, saved.after_game), ("stop", "leave"))
        self.assertIn("steam:1", saved.games)

    def test_an_oversized_broken_file_is_not_offered_for_repair(self):
        self.path.write_text("{" + " " * (3 * 1024 * 1024), encoding="utf-8")
        model = self.model()
        self.assertEqual(model.raw_file_text, "")
        self.assertTrue(model.load_problems)

    def test_a_file_that_is_not_utf8_is_reported(self):
        self.path.write_bytes(b'{"on_launch": "\xff"}')
        model = self.model()
        self.assertTrue(model.load_error)
        self.assertTrue(model.load_problems)

    # raw editor drafts

    def test_an_unusable_raw_draft_counts_as_dirty_and_blocks_apply(self):
        model = self.model()
        model.set_raw_problems(["Not valid JSON: oops"])
        self.assertTrue(model.dirty)
        problems = model.apply()
        self.assertEqual(problems, ["Raw config: Not valid JSON: oops"])
        model.set_raw_problems([])
        self.assertFalse(model.dirty)

    # save failures

    def test_a_failed_save_never_names_the_temporary_file(self):
        blocker = self.dir / "blocker"
        blocker.write_text("x")
        model = self.model()
        model.path = blocker / "config.json"
        model.update(on_launch="stop")
        problems = model.apply()
        self.assertEqual(len(problems), 1)
        self.assertIn("could not be saved", problems[0])
        self.assertNotIn(".tmp", problems[0])
        self.assertNotIn(str(blocker), problems[0])
        self.assertTrue(model.dirty)

    # discovery

    def test_a_scan_runs_in_a_worker_thread_and_lands_on_the_calling_thread(self):
        gate = threading.Event()
        seen = []

        def slow(config):
            seen.append(threading.current_thread())
            gate.wait(5)
            return [candidate("comfy")]

        model = SettingsModel(self.path, self.dir / "state", discover=slow, runner=lambda argv: (0, ""))
        self.addCleanup(model.shutdown)
        changes = []
        model.changed.connect(lambda: changes.append(threading.current_thread()))
        self.assertTrue(model.scan())
        self.assertTrue(model.scanning)
        self.assertFalse(model.candidates_loaded)
        self.assertEqual(model.cached_candidates(), [])
        self.assertFalse(model.scan())  # one scan at a time
        gate.set()
        self.assertTrue(model.wait_scan(5))
        self.assertFalse(model.scanning)
        self.assertTrue(model.candidates_loaded)
        self.assertEqual([c.name for c in model.cached_candidates()], ["comfy"])
        self.assertIsNot(seen[0], threading.main_thread())
        self.assertEqual(set(changes), {threading.main_thread()})

    def test_a_failing_scan_gives_an_empty_result(self):
        def broken(config):
            raise RuntimeError("docker is down")

        model = SettingsModel(self.path, self.dir / "state", discover=broken, runner=lambda argv: (0, ""))
        self.addCleanup(model.shutdown)
        model.scan()
        self.assertTrue(model.wait_scan(5))
        self.assertEqual(model.cached_candidates(), [])
        self.assertTrue(model.candidates_loaded)

    def test_a_scan_requested_during_a_scan_runs_again_afterwards(self):
        gate = threading.Event()
        calls = []

        def slow(config):
            calls.append(1)
            gate.wait(5)
            return []

        model = SettingsModel(self.path, self.dir / "state", discover=slow, runner=lambda argv: (0, ""))
        self.addCleanup(model.shutdown)
        model.scan()
        model.scan()
        gate.set()
        self.assertTrue(model.wait_scan(5))
        self.assertEqual(len(calls), 2)

    def test_scan_without_refresh_uses_the_result_it_has(self):
        model = self.model()
        self.assertTrue(model.scan(False))
        self.assertFalse(model.scan(False))
        self.assertTrue(model.scan(True))

    def test_a_rule_name_with_markup_is_stored_unchanged(self):
        model = self.model()
        model.update(games={"steam:1": GameRule("<b>x</b>", "ask")})
        model.apply()
        self.assertEqual(configmod.load(self.path).games["steam:1"].name, "<b>x</b>")


if __name__ == "__main__":
    unittest.main()
