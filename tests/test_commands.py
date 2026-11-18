from __future__ import annotations

import sys
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
        runtime.start([ref])
        self.assertEqual(self.calls, [(["stop-it"], 12.0), (["start-it"], 12.0)])

    def test_default_timeout_and_foreign_refs(self):
        runtime = self.runtime()
        runtime.stop([{"kind": "service", "runtime": "service", "name": "x", "id": "unload"}])
        self.assertEqual(self.calls, [])
        runtime.stop([{"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}])
        self.assertEqual(self.calls[0][1], 60.0)

    def test_missing_id_is_logged_and_skipped(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().stop([{"kind": "command", "runtime": "command", "name": "Old", "id": "old"}])
        self.assertEqual(self.calls, [])
        self.assertTrue(log.called)

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
        runtime = CommandRuntime(lambda: types.SimpleNamespace(targets=[spec(stop=["no-such-command-xyz"])]))
        ref = {"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime.stop([ref])
            runtime.stop([ref])
        self.assertEqual(log.call_count, 1)

    def test_really_runs_without_a_shell(self):
        out = []
        runtime = CommandRuntime(lambda: types.SimpleNamespace(
            targets=[spec(stop=[sys.executable, "-c", "import sys; sys.exit('$HOME && x')"], start=["true"])]))
        with mock.patch("gpu_game_guard.util.log", side_effect=out.append):
            runtime.stop([{"kind": "command", "runtime": "command", "name": "Models", "id": "unload"}])
        self.assertIn("$HOME && x", out[0])

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
