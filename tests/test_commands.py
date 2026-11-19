from __future__ import annotations

import types
import unittest
from unittest import mock

from gpu_game_guard import gpu
from gpu_game_guard.commands import CommandRuntime


def spec(id="unload", stop=("stop-it",), start=("start-it",), check=None, timeout=None, label="Models", enabled=True):
    return types.SimpleNamespace(id=id, kind="command", label=label, enabled=enabled, unit=None, scope=None,
                                 stop=list(stop), start=list(start), check=list(check) if check else None, match=None,
                                 restart=False, timeout_seconds=timeout)


class CommandRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.specs = [spec()]
        self.calls = []
        self.results = {}

    def run_fake(self, args, timeout=None):
        self.calls.append((list(args), timeout))
        return self.results.get(args[0], gpu.CommandResult(0, "", ""))

    def runtime(self):
        return CommandRuntime(lambda: types.SimpleNamespace(targets=self.specs), run=self.run_fake)

    def test_without_a_check_the_target_is_always_running(self):
        (target,) = self.runtime().running_gpu_targets()
        self.assertEqual(target.ref, {"kind": "command", "runtime": "command", "name": "Models", "id": "unload"})
        self.assertIsNone(target.vram_mib)
        self.assertEqual(self.calls, [])

    def test_check_decides(self):
        self.specs = [spec(check=["probe"], timeout=7)]
        self.assertEqual(len(self.runtime().running_gpu_targets()), 1)
        self.assertEqual(self.calls, [(["probe"], 7.0)])
        self.results["probe"] = gpu.CommandResult(1, "", "")
        self.assertEqual(self.runtime().running_gpu_targets(), [])
        self.results["probe"] = None
        self.assertEqual(self.runtime().running_gpu_targets(), [])

    def test_stop_and_start_run_the_lists_with_the_timeout(self):
        ref = {"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}
        self.specs = [spec(timeout=12)]
        runtime = self.runtime()
        runtime.stop([ref])
        self.assertEqual(runtime.start([ref]), [])
        self.assertEqual(self.calls, [(["stop-it"], 12.0), (["start-it"], 12.0)])

    def test_default_timeout_and_foreign_refs(self):
        runtime = self.runtime()
        runtime.stop([{"kind": "service", "runtime": "service", "name": "x", "id": "unload"}])
        self.assertEqual(self.calls, [])
        runtime.stop([{"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}])
        self.assertEqual(self.calls[0][1], 60.0)
        self.assertEqual(runtime.start([{"kind": "service", "runtime": "service", "name": "x", "id": "unload"}]), [])

    def test_missing_id_is_logged_and_skipped(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().stop([{"kind": "command", "runtime": "command", "name": "Old", "id": "old"}])
        self.assertEqual(self.calls, [])
        self.assertIn("no configured target with id 'old'", log.call_args[0][0])

    def test_start_for_a_gone_target_is_reported_failed(self):
        ref = {"kind": "command", "runtime": "command", "name": "Old", "id": "old"}
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().start([ref]), [ref])
        self.assertEqual(self.calls, [])

    def test_start_for_a_disabled_target_still_runs_its_command(self):
        self.specs = [spec(enabled=False)]
        ref = {"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}
        self.assertEqual(self.runtime().start([ref]), [])
        self.assertEqual([call[0] for call in self.calls], [["start-it"]])

    def test_start_returns_the_refs_whose_command_failed(self):
        self.specs = [spec(id="a", start=["a-start"]), spec(id="b", start=["b-start"])]
        self.results["b-start"] = gpu.CommandResult(1, "", "no")
        refs = [{"kind": "command", "runtime": "command", "name": n, "id": n} for n in ("a", "b")]
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().start(refs), [refs[1]])

    def test_a_target_without_a_check_is_stopped_after_a_successful_stop_until_started(self):
        ref = {"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}
        runtime = self.runtime()
        self.assertEqual(len(runtime.running_gpu_targets()), 1)
        runtime.stop([ref])
        self.assertEqual(runtime.running_gpu_targets(), [])
        self.assertEqual(runtime.start([ref]), [])
        self.assertEqual(len(runtime.running_gpu_targets()), 1)

    def test_a_failed_stop_leaves_a_target_without_a_check_running(self):
        self.results["stop-it"] = gpu.CommandResult(2, "", "bad")
        runtime = self.runtime()
        with mock.patch("gpu_game_guard.util.log"):
            runtime.stop([{"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}])
        self.assertEqual(len(runtime.running_gpu_targets()), 1)

    def test_the_stopped_mark_is_per_target_and_ignored_with_a_check(self):
        self.specs = [spec(id="a", label="A"), spec(id="b", label="B", check=["probe"])]
        refs = [{"kind": "command", "runtime": "command", "name": n, "id": n.lower()} for n in ("A", "B")]
        runtime = self.runtime()
        runtime.stop(refs)
        self.assertEqual([t.name for t in runtime.running_gpu_targets()], ["B"], "the check decides for B")
        self.results["probe"] = gpu.CommandResult(1, "", "")
        self.assertEqual(runtime.running_gpu_targets(), [])
        self.results["probe"] = gpu.CommandResult(0, "", "")
        runtime.start(refs)
        self.assertEqual([t.name for t in runtime.running_gpu_targets()], ["A", "B"])

    def test_release_clears_the_stopped_mark_without_running_start(self):
        ref = {"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}
        runtime = self.runtime()
        runtime.stop([ref])
        self.assertEqual(runtime.running_gpu_targets(), [])
        self.calls.clear()
        runtime.release([ref, {"kind": "service", "runtime": "service", "id": "unload"}])
        self.assertEqual(len(runtime.running_gpu_targets()), 1)
        self.assertEqual(self.calls, [])

    def test_adopt_restores_the_stopped_mark_of_a_target_without_a_check(self):
        self.specs = [spec(id="a", label="A"), spec(id="b", label="B", check=["probe"])]
        refs = [{"kind": "command", "runtime": "command", "name": n, "id": n.lower()} for n in ("A", "B")]
        runtime = self.runtime()
        runtime.adopt(refs + [{"kind": "command", "runtime": "command", "name": "X"}])
        self.assertEqual([t.name for t in runtime.running_gpu_targets()], ["B"])
        runtime.start(refs)
        self.assertEqual([t.name for t in runtime.running_gpu_targets()], ["A", "B"])

    def test_non_zero_exit_is_logged_once(self):
        self.results["stop-it"] = gpu.CommandResult(2, "", "bad\n")
        ref = {"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}
        gpu._last_failure.clear()
        runtime = self.runtime()
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime.stop([ref])
            runtime.stop([ref])
        self.assertEqual(log.call_count, 1)

    def test_a_command_that_cannot_run_never_raises(self):
        self.results["stop-it"] = None
        ref = {"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}
        runtime = self.runtime()
        runtime.stop([ref])
        self.assertEqual(runtime.start([ref]), [])
        self.assertEqual([call[0] for call in self.calls], [["stop-it"], ["start-it"]])
        self.assertEqual(len(runtime.running_gpu_targets()), 1, "a stop that did not run leaves the target running")

    def test_a_runner_that_raises_is_logged_and_fails_the_start(self):
        def broken(args, timeout=None):
            raise RuntimeError("boom")

        runtime = CommandRuntime(lambda: types.SimpleNamespace(targets=self.specs), run=broken)
        ref = {"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime.stop([ref])
            self.assertEqual(runtime.start([ref]), [ref])
        self.assertEqual(log.call_count, 2)

    def test_invalid_specs_do_not_hide_valid_ones(self):
        self.specs = [spec(id="bad", stop=[]), spec(id="good", label="Good")]
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual([t.name for t in self.runtime().running_gpu_targets()], ["Good"])

    def test_empty_spec_list(self):
        self.specs = []
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().running_gpu_targets(), [])

    def test_installed_and_available(self):
        self.assertTrue(self.runtime().installed())
        self.assertTrue(self.runtime().available())


if __name__ == "__main__":
    unittest.main()
