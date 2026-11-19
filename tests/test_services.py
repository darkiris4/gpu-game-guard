from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from gpu_game_guard import gpu
from gpu_game_guard.services import ServiceRuntime


def spec(id="ollama", unit="ollama.service", scope="system", label="Ollama", enabled=True):
    return types.SimpleNamespace(id=id, kind="service", label=label, enabled=enabled, unit=unit, scope=scope,
                                 stop=None, start=None, check=None, match=None, restart=False, timeout_seconds=None)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)
        self.specs = [spec()]
        self.calls = []
        self.answers = {}

    def run_fake(self, args, timeout=None):
        self.calls.append(list(args))
        if args[1:2] == ["is-active"] or args[2:3] == ["is-active"]:
            default = gpu.CommandResult(3, "inactive\n", "")
        else:
            default = gpu.CommandResult(0, "", "")
        return self.answers.get(tuple(args), default)

    def runtime(self, vram=None):
        return ServiceRuntime(lambda: types.SimpleNamespace(targets=self.specs), run=self.run_fake, proc=self.proc,
                              vram=vram or (lambda: gpu.PidMeasurement()))

    def process(self, pid, cgroup):
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / "cgroup").write_text(cgroup)


class RunningTests(Fixture):
    def test_active_unit_is_a_target_with_extra(self):
        self.answers[("systemctl", "is-active", "--", "ollama.service")] = gpu.CommandResult(0, "active\n", "")
        (target,) = self.runtime().running_gpu_targets()
        self.assertEqual((target.kind, target.runtime, target.name), ("service", "service", "Ollama"))
        self.assertEqual(target.ref, {"kind": "service", "runtime": "service", "name": "Ollama", "id": "ollama",
                                      "unit": "ollama.service", "scope": "system"})

    def test_inactive_and_failed_units_are_not_targets(self):
        for out in ("inactive\n", "failed\n", "activating\n", ""):
            self.answers[("systemctl", "is-active", "--", "ollama.service")] = gpu.CommandResult(3, out, "")
            self.assertEqual(self.runtime().running_gpu_targets(), [], out)

    def test_user_scope_uses_the_user_flag_and_label_falls_back_to_id(self):
        self.specs = [spec(id="mine", unit="x.service", scope="user", label="")]
        self.answers[("systemctl", "--user", "is-active", "--", "x.service")] = gpu.CommandResult(0, "active\n", "")
        (target,) = self.runtime().running_gpu_targets()
        self.assertEqual((target.name, target.ref["scope"]), ("mine", "user"))

    def test_command_that_cannot_run_is_not_a_target(self):
        runtime = ServiceRuntime(lambda: types.SimpleNamespace(targets=self.specs), run=lambda args, timeout=None: None,
                                 proc=self.proc, vram=lambda: {})
        self.assertEqual(runtime.running_gpu_targets(), [])

    def test_memory_is_the_sum_over_the_units_cgroup(self):
        self.answers[("systemctl", "is-active", "--", "ollama.service")] = gpu.CommandResult(0, "active\n", "")
        self.process(10, "0::/system.slice/ollama.service\n")
        self.process(11, "0::/system.slice/ollama.service\n")
        self.process(12, "0::/system.slice/other.service\n")
        by_pid = gpu.PidMeasurement({10: 300, 11: 20, 12: 999})
        (target,) = self.runtime(vram=lambda: by_pid).running_gpu_targets()
        self.assertEqual(target.vram_mib, 320)

    def test_memory_zero_when_measured_and_none_held_and_unknown_when_unmeasurable(self):
        self.answers[("systemctl", "is-active", "--", "ollama.service")] = gpu.CommandResult(0, "active\n", "")
        self.assertEqual(self.runtime(vram=lambda: gpu.PidMeasurement()).running_gpu_targets()[0].vram_mib, 0)
        self.assertIsNone(self.runtime(vram=lambda: None).running_gpu_targets()[0].vram_mib)

        def broken():
            raise RuntimeError("boom")

        with mock.patch("gpu_game_guard.util.log"):
            self.assertIsNone(self.runtime(vram=broken).running_gpu_targets()[0].vram_mib)

    def test_empty_disabled_and_invalid_specs(self):
        self.specs = []
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().running_gpu_targets(), [])
            self.specs = [spec(enabled=False)]
            self.assertEqual(self.runtime().running_gpu_targets(), [])
            self.specs = [spec(id="bad", unit="nope"), spec()]
            self.answers[("systemctl", "is-active", "--", "ollama.service")] = gpu.CommandResult(0, "active\n", "")
            self.assertEqual([t.name for t in self.runtime().running_gpu_targets()], ["Ollama"])

    def test_an_empty_target_list_is_not_logged(self):
        self.specs = []
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().running_gpu_targets()
        log.assert_not_called()

    def test_installed_and_available(self):
        runtime = ServiceRuntime(lambda: None, which=lambda name: None)
        self.assertTrue(runtime.installed())
        self.assertFalse(runtime.available())
        self.assertTrue(ServiceRuntime(lambda: None, which=lambda name: "/bin/systemctl").available())


class ChangeTests(Fixture):
    ref = {"kind": "service", "runtime": "service", "name": "Ollama", "id": "ollama", "unit": "ollama.service"}

    def test_stop_and_start_commands(self):
        runtime = self.runtime()
        runtime.stop([self.ref])
        runtime.start([self.ref])
        self.assertEqual(self.calls, [["systemctl", "--no-ask-password", "stop", "--", "ollama.service"],
                                      ["systemctl", "--no-ask-password", "start", "--", "ollama.service"]])

    def test_user_scope(self):
        self.specs = [spec(scope="user")]
        self.runtime().stop([self.ref])
        self.assertEqual(self.calls, [["systemctl", "--user", "--no-ask-password", "stop", "--", "ollama.service"]])

    def test_other_providers_refs_are_ignored(self):
        self.runtime().stop([{"kind": "container", "runtime": "docker", "name": "x", "id": "ollama"},
                             {"kind": "process", "runtime": "process", "name": "x", "id": "ollama"}])
        self.assertEqual(self.calls, [])

    def test_ref_whose_id_is_gone_is_logged_and_skipped(self):
        self.specs = []
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().stop([self.ref])
        self.assertEqual(self.calls, [])
        self.assertIn("no configured target with id 'ollama'", log.call_args[0][0])

    def test_the_live_config_decides_the_unit(self):
        self.specs = [spec(unit="renamed.service")]
        self.runtime().stop([self.ref])
        self.assertEqual(self.calls[0][-1], "renamed.service")

    def test_authorisation_failure_names_the_fix_and_is_logged_once(self):
        for text in ("Interactive authentication required.", "Failed to stop ollama.service: Access denied",
                     "Authentication is required to manage system services or other units."):
            self.answers[("systemctl", "--no-ask-password", "stop", "--", "ollama.service")] = gpu.CommandResult(1, "", text)
            gpu._last_failure.clear()
            with mock.patch("gpu_game_guard.util.log") as log:
                runtime = self.runtime()
                runtime.stop([self.ref])
                runtime.stop([self.ref])
            self.assertEqual(log.call_count, 1, text)
            self.assertIn("gpu-game-guard polkit allow ollama.service", log.call_args[0][0])

    def test_other_failures_do_not_suggest_polkit_and_never_raise(self):
        self.answers[("systemctl", "--no-ask-password", "start", "--", "ollama.service")] = gpu.CommandResult(5, "", "Unit not found.")
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().start([self.ref])
        self.assertNotIn("polkit", log.call_args[0][0])

    def test_user_scope_failure_never_mentions_polkit(self):
        self.specs = [spec(scope="user")]
        self.answers[("systemctl", "--user", "--no-ask-password", "stop", "--", "ollama.service")] = gpu.CommandResult(1, "", "Access denied")
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().stop([self.ref])
        self.assertNotIn("polkit", log.call_args[0][0])

    def test_failed_to_stop_alone_is_not_an_authorisation_failure(self):
        for verb in ("stop", "start"):
            self.answers[("systemctl", "--no-ask-password", verb, "--", "ollama.service")] = gpu.CommandResult(
                1, "", f"Failed to {verb} ollama.service: Unit is masked.")
            gpu._last_failure.clear()
            with mock.patch("gpu_game_guard.util.log") as log:
                getattr(self.runtime(), verb)([self.ref])
            self.assertNotIn("polkit", log.call_args[0][0], verb)
            self.assertNotIn("not authorised", log.call_args[0][0], verb)

    def test_start_failure_with_a_refusal_names_the_fix(self):
        self.answers[("systemctl", "--no-ask-password", "start", "--", "ollama.service")] = gpu.CommandResult(
            1, "", "Interactive authentication required.")
        gpu._last_failure.clear()
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().start([self.ref])
        self.assertIn("polkit allow ollama.service", log.call_args[0][0])

    def test_start_returns_the_refs_that_failed(self):
        other = dict(self.ref, id="b", unit="b.service", name="B")
        self.answers[("systemctl", "--no-ask-password", "start", "--", "b.service")] = gpu.CommandResult(1, "", "Unit not found.")
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().start([self.ref, other]), [other])
            self.assertEqual(self.runtime().start([]), [])

    def test_start_failure_when_the_command_cannot_run(self):
        runtime = ServiceRuntime(lambda: types.SimpleNamespace(targets=self.specs), run=lambda *a, **k: None)
        self.assertEqual(runtime.start([self.ref]), [self.ref])

    def test_start_uses_the_refs_own_unit_after_the_spec_is_gone_or_disabled(self):
        user_ref = dict(self.ref, unit="mine.service", scope="user")
        for specs in ([], [spec(enabled=False, unit="other.service")]):
            self.specs = specs
            self.calls.clear()
            self.assertEqual(self.runtime().start([user_ref]), [])
            self.assertEqual(self.calls, [["systemctl", "--user", "--no-ask-password", "start", "--", "mine.service"]])

    def test_units_are_validated_strictly_and_refused_once(self):
        gpu._last_failure.clear()
        for unit in ("-x.service", "a*.service", "a?.service", "a[b].service", "a b.service", "../x.service", "x.timer", ""):
            ref = dict(self.ref, unit=unit)
            with mock.patch("gpu_game_guard.util.log") as log:
                runtime = self.runtime()
                self.assertEqual(runtime.start([ref]), [ref], unit)
                runtime.start([ref])
            self.assertEqual(log.call_count, 1, unit)
        self.assertEqual(self.calls, [])

    def test_a_spec_with_a_bad_unit_is_refused_for_stop_and_listing(self):
        for unit in ("-x.service", "a*.service", "a[b].service"):
            self.specs = [spec(unit=unit)]
            with mock.patch("gpu_game_guard.util.log"):
                self.assertEqual(self.runtime().running_gpu_targets(), [])
                self.runtime().stop([self.ref])
        self.assertEqual(self.calls, [])

    def test_a_ref_with_a_bad_scope_is_refused(self):
        ref = dict(self.ref, scope="machine")
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().start([ref]), [ref])
        self.assertEqual(self.calls, [])

    def test_runner_exception_is_logged_not_raised(self):
        runtime = ServiceRuntime(lambda: types.SimpleNamespace(targets=self.specs), run=lambda *a, **k: 1 / 0)
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime.stop([self.ref])
            self.assertEqual(runtime.start([self.ref]), [self.ref])
        self.assertEqual(log.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class FailureLineTests(Fixture):
    ref = {"kind": "service", "runtime": "service", "name": "Ollama", "id": "ollama"}
    key = ("systemctl", "--no-ask-password", "stop", "--", "ollama.service")

    def logged(self, stderr, unit_calls=1):
        self.answers[self.key] = gpu.CommandResult(1, "", stderr)
        gpu._last_failure.clear()
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().stop([self.ref])
        self.assertEqual(log.call_count, unit_calls)
        return log.call_args[0][0]

    def test_the_line_naming_the_error_is_logged_not_the_last_line(self):
        line = self.logged("Failed to stop ollama.service: Unit is masked.\nSee system logs and 'systemctl status ollama.service' for details.")
        self.assertIn("Failed to stop ollama.service: Unit is masked.", line)
        self.assertNotIn("See system logs", line)

    def test_the_last_line_is_used_when_none_names_an_error(self):
        self.assertIn("second", self.logged("first\nsecond"))

    def test_the_polkit_hint_survives_a_long_message(self):
        line = self.logged("Failed to stop ollama.service: Access denied " + "x" * 600)
        self.assertIn("run 'sudo gpu-game-guard polkit allow ollama.service' to allow it without a password", line)
        self.assertLess(len(line), 400)

    def test_the_prefix_is_not_repeated(self):
        line = self.logged("Failed to stop ollama.service: Unit is masked.")
        self.assertEqual(line.count("systemctl stop ollama.service"), 1)
