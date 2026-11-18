from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from gpu_game_guard import gpu
from gpu_game_guard.config import Config, TargetSpec
from gpu_game_guard.processes import ProcessRuntime, parse_stat

UID = 1000


def spec(id="obs", match="obs", restart=True, label="OBS Studio", enabled=True):
    return types.SimpleNamespace(id=id, kind="process", label=label, enabled=enabled, unit=None, scope=None, stop=None,
                                 start=None, check=None, match=match, restart=restart, timeout_seconds=None)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)
        self.specs = [spec()]
        self.config = types.SimpleNamespace(targets=self.specs, is_protected=lambda name: name == "custom-protected")
        self.signals = []
        self.started = []
        self.now = [0.0]
        self.alive_after_term = False

    def add(self, pid, comm, start=100, ppid=1, uid=UID, state="S", cmdline=None, cwd=None):
        base = self.proc / str(pid)
        base.mkdir()
        (base / "comm").write_text(comm + "\n")
        (base / "stat").write_text(f"{pid} ({comm}) {state} {ppid} " + " ".join(["0"] * 17) + f" {start} 0 0\n")
        (base / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (base / "cmdline").write_bytes(("\0".join(cmdline or [comm]) + "\0").encode())
        if cwd:
            os.symlink(cwd, base / "cwd")

    def kill(self, pid, sig):
        self.signals.append((pid, sig))
        if not self.alive_after_term:
            import shutil
            shutil.rmtree(self.proc / str(pid))

    def sleep(self, seconds):
        self.now[0] += seconds

    def runtime(self, uid=UID, self_pid=4242, vram=None):
        return ProcessRuntime(lambda: self.config, proc=self.proc, vram=vram or (lambda: gpu.PidMeasurement()), uid=uid,
                              self_pid=self_pid, kill=self.kill, popen=lambda *a, **k: self.started.append((a, k)),
                              sleep=self.sleep, clock=lambda: self.now[0])


class StatTests(unittest.TestCase):
    def test_parse_handles_odd_command_names(self):
        text = "5 (a) b) S 7 " + " ".join(["0"] * 17) + " 9999 0\n"
        self.assertEqual(parse_stat(text), ("S", 7, "9999"))
        self.assertIsNone(parse_stat("garbage"))


class RunningTests(Fixture):
    def test_matches_by_comm_case_sensitively_with_extra_of_the_oldest(self):
        self.add(10, "obs", start=300, cmdline=["/usr/bin/obs", "--x"], cwd="/tmp")
        self.add(11, "obs", start=200, cmdline=["/usr/bin/obs", "--oldest"], cwd="/")
        self.add(12, "OBS", start=100)
        self.add(13, "obs-ffmpeg", start=100)
        (target,) = self.runtime(vram=lambda: gpu.PidMeasurement({10: 100, 11: 50, 99: 1})).running_gpu_targets()
        self.assertEqual(target.ref, {"kind": "process", "runtime": "process", "name": "OBS Studio", "id": "obs",
                                      "cmdline": ["/usr/bin/obs", "--oldest"], "cwd": "/"})
        self.assertEqual(target.vram_mib, 150)

    def test_glob(self):
        self.specs[0] = spec(match="obs*")
        self.add(10, "obs-ffmpeg")
        self.assertEqual(len(self.config.targets), 1)
        self.assertEqual(len(self.runtime().running_gpu_targets()), 1)

    def test_other_users_and_zombies_are_ignored(self):
        self.add(10, "obs", uid=0)
        self.add(11, "obs", state="Z")
        self.assertEqual(self.runtime().running_gpu_targets(), [])

    def test_unknown_memory(self):
        self.add(10, "obs")
        self.assertIsNone(self.runtime(vram=lambda: None).running_gpu_targets()[0].vram_mib)

    def test_protected_names_are_refused_and_logged_once(self):
        self.specs[:] = [spec(id="p", match="Xorg"), spec(id="q", match="custom-*"), spec(id="s", match="steam*")]
        for pid, comm in enumerate(["Xorg", "custom-protected", "steamwebhelper"], start=10):
            self.add(pid, comm)
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime = self.runtime()
            self.assertEqual(runtime.running_gpu_targets(), [])
            runtime.stop([{"kind": "process", "runtime": "process", "name": "x", "id": "p"}])
        self.assertEqual(log.call_count, 3)
        self.assertEqual(self.signals, [])

    def test_too_broad_pattern_is_refused(self):
        self.specs[:] = [spec(match="*")]
        self.add(10, "anything")
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().running_gpu_targets(), [])

    def test_guard_and_its_ancestors_are_never_matched(self):
        self.specs[:] = [spec(match="py*")]
        self.add(1, "systemd", ppid=0)
        self.add(50, "pyparent", ppid=1)
        self.add(4242, "pyguard", ppid=50)
        self.add(60, "pyother", ppid=1)
        with mock.patch("gpu_game_guard.util.log"):
            (target,) = self.runtime().running_gpu_targets()
            self.assertEqual(target.name, "OBS Studio")
            self.assertEqual(target.vram_mib, 0)
            self.runtime().stop([target.ref])
        self.assertEqual(self.signals, [(60, signal.SIGTERM)])

    def test_python_running_the_guard_is_protected(self):
        self.specs[:] = [spec(match="python3")]
        self.add(10, "python3", cmdline=["python3", "-m", "gpu_game_guard", "run"])
        self.add(11, "python3", cmdline=["python3", "script.py"])
        with mock.patch("gpu_game_guard.util.log"):
            self.runtime().stop([{"kind": "process", "runtime": "process", "name": "x", "id": "obs"}])
        self.assertEqual([pid for pid, _ in self.signals], [11])

    def test_empty_and_invalid_specs(self):
        self.specs[:] = [spec(id="bad", match="a/b")]
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().running_gpu_targets(), [])
            self.specs[:] = []
            self.assertEqual(self.runtime().running_gpu_targets(), [])

    def test_installed_and_available(self):
        self.assertTrue(self.runtime().installed())
        self.assertTrue(self.runtime().available())


class StopTests(Fixture):
    ref = {"kind": "process", "runtime": "process", "name": "OBS Studio", "id": "obs"}

    def test_sigterm_each_match_and_never_sigkill(self):
        self.add(10, "obs")
        self.add(11, "obs", start=5)
        self.runtime().stop([self.ref])
        self.assertEqual(sorted(self.signals), [(10, signal.SIGTERM), (11, signal.SIGTERM)])
        self.assertNotIn(signal.SIGKILL, [sig for _, sig in self.signals])

    def test_stayers_are_logged_after_five_seconds_and_not_killed(self):
        self.alive_after_term = True
        self.add(10, "obs")
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().stop([self.ref])
        self.assertEqual(self.signals, [(10, signal.SIGTERM)])
        self.assertGreaterEqual(self.now[0], 5.0)
        self.assertIn("still running", log.call_args[0][0])

    def test_process_that_exits_during_the_wait_is_fine(self):
        self.alive_after_term = True
        self.add(10, "obs")
        seen = []

        def sleep(seconds):
            seen.append(seconds)
            import shutil
            shutil.rmtree(self.proc / "10", ignore_errors=True)

        runtime = self.runtime()
        runtime._sleep = sleep
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime.stop([self.ref])
        self.assertEqual(len(seen), 1)
        log.assert_not_called()

    def test_vanished_before_the_signal_is_fine(self):
        self.add(10, "obs")

        def kill(pid, sig):
            raise ProcessLookupError

        runtime = self.runtime()
        runtime._kill = kill
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime.stop([self.ref])
        log.assert_not_called()

    def test_start_time_is_rechecked_before_each_signal(self):
        self.add(10, "obs", start=100)
        self.add(11, "obs", start=100)
        runtime = self.runtime()
        original = runtime._alive
        calls = []

        def alive(item):
            if not calls and item.pid == 11:  # pid 11 is replaced by a new process just before its signal
                calls.append(item)
                (self.proc / "11" / "stat").write_text((self.proc / "11" / "stat").read_text().replace(" 100 0 0", " 777 0 0"))
            return original(item)

        runtime._alive = alive
        runtime.stop([self.ref])
        self.assertEqual([pid for pid, _ in self.signals], [10])

    def test_other_users_processes_are_not_signalled(self):
        self.add(10, "obs", uid=0)
        self.runtime().stop([self.ref])
        self.assertEqual(self.signals, [])

    def test_foreign_refs_and_missing_ids_do_nothing(self):
        self.add(10, "obs")
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().stop([{"kind": "service", "runtime": "service", "name": "x", "id": "obs"},
                                 {"kind": "process", "runtime": "process", "name": "x", "id": "gone"}])
        self.assertEqual(self.signals, [])
        self.assertEqual(log.call_count, 1)

    def test_signal_permission_error_is_logged(self):
        self.add(10, "obs")
        runtime = self.runtime()
        runtime._kill = mock.Mock(side_effect=PermissionError("no"))
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime.stop([self.ref])
        self.assertTrue(log.called)


class StartTests(Fixture):
    ref = {"kind": "process", "runtime": "process", "name": "OBS Studio", "id": "obs",
           "cmdline": ["/usr/bin/obs", "--minimize"], "cwd": "/tmp"}

    def test_started_detached(self):
        self.runtime().start([self.ref])
        (args, kwargs), = self.started
        self.assertEqual(args, (["/usr/bin/obs", "--minimize"],))
        self.assertEqual(kwargs, {"cwd": "/tmp", "start_new_session": True, "stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL})

    def test_missing_cwd_is_dropped(self):
        self.runtime().start([dict(self.ref, cwd="/no/such/dir")])
        self.assertIsNone(self.started[0][1]["cwd"])

    def test_restart_off_or_no_cmdline_or_already_running(self):
        self.specs[0] = spec(restart=False)
        with mock.patch("gpu_game_guard.util.log"):
            self.runtime().start([self.ref])
            self.specs[0] = spec()
            self.runtime().start([{k: v for k, v in self.ref.items() if k != "cmdline"}])
            self.runtime().start([dict(self.ref, cmdline=[1])])
            self.add(10, "obs")
            self.runtime().start([self.ref])
        self.assertEqual(self.started, [])

    def test_popen_failure_is_logged_not_raised(self):
        runtime = self.runtime()
        runtime._popen = mock.Mock(side_effect=FileNotFoundError("x"))
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime.start([self.ref])
        self.assertTrue(log.called)


class RealConfigTests(Fixture):
    def test_works_with_a_real_config_and_target_spec(self):
        self.config = Config().with_target(TargetSpec(id="obs", kind="process", label="OBS", match="obs"))
        self.add(10, "obs")
        (target,) = self.runtime().running_gpu_targets()
        self.assertEqual(target.name, "OBS")
        self.runtime().stop([target.ref])
        self.assertEqual(self.signals, [(10, signal.SIGTERM)])

    def test_real_config_protect_list(self):
        self.config = Config.from_json({"protect": ["my-editor*"]}).with_target(
            TargetSpec(id="e", kind="process", label="E", match="my-editor*"))
        self.add(10, "my-editor")
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().running_gpu_targets(), [])


if __name__ == "__main__":
    unittest.main()
