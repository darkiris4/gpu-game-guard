from __future__ import annotations

import os
import shutil
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
        self.missing = {"systemd-run"}  # commands `which` does not find
        self.runs = []
        self.manager = gpu.CommandResult(0, "running\n", "")
        self.environment = gpu.CommandResult(0, "DISPLAY=:1\nXDG_RUNTIME_DIR=/run/user/1000\nOTHER=x\n", "")
        self.child = types.SimpleNamespace(poll=lambda: None)

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
            shutil.rmtree(self.proc / str(pid))

    def sleep(self, seconds):
        self.now[0] += seconds

    def run_fake(self, args, timeout=None):
        self.runs.append(list(args))
        return self.environment if args[-1] == "show-environment" else self.manager

    def popen(self, *args, **kwargs):
        self.started.append((args, kwargs))
        return self.child

    def runtime(self, uid=UID, self_pid=4242, vram=None, environ=None):
        return ProcessRuntime(lambda: self.config, run=self.run_fake, proc=self.proc,
                              vram=vram or (lambda: gpu.PidMeasurement()), uid=uid, self_pid=self_pid, kill=self.kill,
                              popen=self.popen, sleep=self.sleep, clock=lambda: self.now[0],
                              which=lambda name: None if name in self.missing or ":" in name or " " in name else "/usr/bin/" + name,
                              wall=lambda: 1700000000.0, environ={"HOME": "/h"} if environ is None else environ)

    def executable(self, name="prog"):
        path = self.proc.parent / (self.proc.name + "-bin") / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        self.addCleanup(lambda: shutil.rmtree(path.parent, ignore_errors=True))
        return str(path)


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

    def test_a_pattern_with_too_few_literal_characters_is_refused_and_logged_once(self):
        for pattern in ("*", "ob*", "o?", "**?"):
            self.specs[:] = [spec(match=pattern)]
            self.add(10, "obs")
            gpu._last_failure.clear()
            with mock.patch("gpu_game_guard.util.log") as log:
                runtime = self.runtime()
                self.assertEqual(runtime.running_gpu_targets(), [], pattern)
                self.assertEqual(runtime.running_gpu_targets(), [], pattern)
                runtime.stop([{"kind": "process", "runtime": "process", "name": "x", "id": "obs"}])
            self.assertEqual(log.call_count, 1, pattern)
            self.assertIn("literal", log.call_args[0][0])
            self.assertEqual(self.signals, [])
            shutil.rmtree(self.proc / "10")

    def test_a_pattern_longer_than_a_process_name_can_be_is_refused(self):
        self.specs[:] = [spec(match="a-very-long-name-x")]
        self.add(10, "a-very-long-name")
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertEqual(self.runtime().running_gpu_targets(), [])
        self.assertIn("longer than 15", log.call_args[0][0])
        self.specs[:] = [spec(match="a" * 15)]
        self.add(11, "a" * 15)
        self.assertEqual(len(self.runtime().running_gpu_targets()), 1)

    def test_a_pattern_matching_more_than_25_processes_is_refused_once(self):
        self.specs[:] = [spec(match="work*")]
        for pid in range(100, 126):
            self.add(pid, "worker")
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime = self.runtime()
            self.assertEqual(runtime.running_gpu_targets(), [])
            self.assertEqual(runtime.running_gpu_targets(), [])
            runtime.stop([{"kind": "process", "runtime": "process", "name": "x", "id": "obs"}])
        self.assertEqual(log.call_count, 1)
        self.assertIn("too broad", log.call_args[0][0])
        self.assertEqual(self.signals, [])
        shutil.rmtree(self.proc / "125")
        self.assertEqual(len(self.runtime().running_gpu_targets()), 1, "25 is allowed")

    def test_guard_and_its_ancestors_are_never_matched(self):
        self.specs[:] = [spec(match="xpy*")]
        self.add(1, "systemd", ppid=0)
        self.add(50, "xpyparent", ppid=1)
        self.add(4242, "xpyguard", ppid=50)
        self.add(60, "xpyother", ppid=1)
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
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertEqual(self.runtime().running_gpu_targets(), [])
            self.assertEqual(log.call_count, 1)
            self.specs[:] = []
            self.assertEqual(self.runtime().running_gpu_targets(), [])
            self.assertEqual(log.call_count, 1)

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
        self.assertIn("cannot signal", log.call_args[0][0])


class CaptureTests(Fixture):
    """The command line and working directory are read when the process is stopped."""

    ref = {"kind": "process", "runtime": "process", "name": "OBS Studio", "id": "obs"}

    def test_cmdline_and_cwd_are_read_immediately_before_the_signal(self):
        exe = self.executable("obs")
        self.add(10, "obs", cmdline=[exe, "--old"], cwd="/tmp")
        runtime = self.runtime()
        (target,) = runtime.running_gpu_targets()
        # The process changes its arguments and directory between the scan and the stop.
        (self.proc / "10" / "cmdline").write_bytes(("\0".join([exe, "--new"]) + "\0").encode())
        os.unlink(self.proc / "10" / "cwd")
        os.symlink("/", self.proc / "10" / "cwd")
        ref = dict(target.ref)
        order = []
        original = runtime._kill
        runtime._kill = lambda pid, sig: (order.append(dict(ref)), original(pid, sig))
        runtime.stop([ref])
        self.assertEqual(order[0]["cmdline"], [exe, "--new"], "recorded before the SIGTERM")
        self.assertEqual((ref["cmdline"], ref["cwd"]), ([exe, "--new"], "/"))

    def test_the_oldest_match_supplies_the_command(self):
        exe = self.executable("obs")
        self.add(10, "obs", start=300, cmdline=[exe, "--young"])
        self.add(11, "obs", start=200, cmdline=[exe, "--oldest"])
        ref = dict(self.ref)
        self.runtime().stop([ref])
        self.assertEqual(ref["cmdline"], [exe, "--oldest"])

    def test_a_rewritten_argv0_falls_back_to_the_executable_path(self):
        exe = self.executable("zzgg-daemon")
        self.add(10, "zzgg-daemon", cmdline=["zzgg-daemon: master process"])
        os.symlink(exe, self.proc / "10" / "exe")
        self.specs[:] = [spec(match="zzgg*")]
        ref = dict(self.ref)
        self.runtime().stop([ref])
        self.assertEqual(ref["cmdline"], [exe])

    def test_a_rewritten_argv0_keeps_the_remaining_arguments(self):
        exe = self.executable("zzgg-daemon")
        self.add(10, "zzgg-daemon", cmdline=["zzgg-daemon: worker", "--flag"])
        os.symlink(exe + " (deleted)", self.proc / "10" / "exe")
        self.specs[:] = [spec(match="zzgg*")]
        ref = dict(self.ref)
        self.runtime().stop([ref])
        self.assertEqual(ref["cmdline"], [exe, "--flag"])

    def test_an_argv0_that_is_not_rewritten_and_not_executable_is_unusable(self):
        exe = self.executable("obs")
        self.add(10, "obs", cmdline=["/no/such/obs", "--x"])
        os.symlink(exe, self.proc / "10" / "exe")
        ref = dict(self.ref)
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().stop([ref])
        self.assertEqual(ref["cmdline"], [])
        self.assertIn("is not an executable that can be found", log.call_args[0][0])

    def test_no_executable_for_a_rewritten_title_is_unusable(self):
        self.add(10, "obs", cmdline=["obs: master process"])
        ref = dict(self.ref)
        with mock.patch("gpu_game_guard.util.log"):
            self.runtime().stop([ref])
        self.assertEqual(ref["cmdline"], [])

    def test_a_bare_command_name_on_path_is_usable(self):
        self.add(10, "obs", cmdline=["obs", "--x"])
        ref = dict(self.ref)
        self.runtime().stop([ref])
        self.assertEqual(ref["cmdline"], ["/usr/bin/obs", "--x"])

    def test_a_relative_path_is_resolved_against_the_process_cwd(self):
        exe = self.executable("obs")
        directory = os.path.dirname(exe)
        self.add(10, "obs", cmdline=["./obs", "--x"], cwd=directory)
        ref = dict(self.ref)
        self.runtime().stop([ref])
        self.assertEqual(ref["cmdline"], [exe, "--x"])

    def test_a_bare_name_is_resolved_on_the_path_of_the_process_environment(self):
        exe = self.executable("zzprog")
        self.add(10, "zzprog", cmdline=["zzprog"], cwd="/")
        (self.proc / "10" / "environ").write_bytes(f"HOME=/h\0PATH=/nonexistent:{os.path.dirname(exe)}\0".encode())
        self.specs[:] = [spec(match="zzprog")]
        ref = dict(self.ref)
        self.runtime().stop([ref])
        self.assertEqual(ref["cmdline"], [exe])

    def test_an_unusable_line_is_logged_once_with_the_reason(self):
        self.add(10, "obs", cmdline=["./gone", "--x"], cwd="/")
        runtime = self.runtime()
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime.stop([dict(self.ref)])
            self.add(11, "obs", cmdline=["./gone", "--x"], cwd="/")
            runtime.stop([dict(self.ref)])
        self.assertEqual(log.call_count, 1)
        self.assertIn("'./gone' is not an executable that can be found", log.call_args[0][0])

    def test_a_rewritten_title_with_a_bare_interpreter_is_unusable(self):
        for name in ("python3.11", "perl", "node", "bash", "sh", "java", "ruby"):
            exe = self.executable(name)
            self.add(10, "zzgg-daemon", cmdline=["zzgg-daemon: master"])
            os.symlink(exe, self.proc / "10" / "exe")
            self.specs[:] = [spec(match="zzgg*")]
            ref = dict(self.ref)
            with mock.patch("gpu_game_guard.util.log") as log:
                self.runtime().stop([ref])
            self.assertEqual(ref["cmdline"], [], name)
            self.assertIn("interpreter", log.call_args[0][0])
            shutil.rmtree(self.proc / "10", ignore_errors=True)
            gpu._last_failure.clear()

    def test_an_interpreter_with_a_script_argument_is_kept(self):
        exe = self.executable("python3")
        self.add(10, "zzgg-daemon", cmdline=["zzgg-daemon: worker", "-u", "app.py"])
        os.symlink(exe, self.proc / "10" / "exe")
        self.specs[:] = [spec(match="zzgg*")]
        ref = dict(self.ref)
        self.runtime().stop([ref])
        self.assertEqual(ref["cmdline"], [exe, "-u", "app.py"])

    def test_the_scan_values_stay_when_the_process_is_gone_before_the_stop(self):
        ref = dict(self.ref, cmdline=["obs"], cwd="/tmp")
        self.runtime().stop([ref])
        self.assertEqual((ref["cmdline"], ref["cwd"]), (["obs"], "/tmp"))


class CountTests(Fixture):
    def test_helpers_of_one_application_count_once(self):
        self.specs[:] = [spec(match="chrome")]
        self.add(10, "chrome", ppid=1)
        for pid in range(11, 71):
            self.add(pid, "chrome", ppid=10)
        found = self.runtime().running_gpu_targets()
        self.assertEqual(len(found), 1)
        runtime = self.runtime()
        runtime.stop([found[0].ref])
        self.assertEqual(len(self.signals), 61)

    def test_more_than_the_limit_of_root_processes_is_refused(self):
        self.specs[:] = [spec(match="chrome")]
        for pid in range(10, 10 + 26):
            self.add(pid, "chrome", ppid=1)
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertEqual(self.runtime().running_gpu_targets(), [])
        self.assertIn("too broad", log.call_args[0][0])

    def test_protection_still_applies_to_each_process(self):
        self.specs[:] = [spec(match="custom-*")]
        self.add(10, "custom-protected", ppid=1)
        self.add(11, "custom-ok", ppid=10)
        found = self.runtime().running_gpu_targets()
        self.assertEqual(len(found), 1)
        self.runtime().stop([found[0].ref])
        self.assertEqual([pid for pid, _ in self.signals], [11])


class StartTests(Fixture):
    ref = {"kind": "process", "runtime": "process", "name": "OBS Studio", "id": "obs",
           "cmdline": ["/usr/bin/obs", "--minimize"], "cwd": "/tmp"}
    detached = {"start_new_session": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "stdin": subprocess.DEVNULL}

    def test_started_detached_without_systemd_run(self):
        self.assertEqual(self.runtime().start([self.ref]), [])
        (args, kwargs), = self.started
        self.assertEqual(args, (["/usr/bin/obs", "--minimize"],))
        env = kwargs.pop("env")
        self.assertEqual(kwargs, dict(self.detached, cwd="/tmp"))
        self.assertEqual(env["HOME"], "/h")

    def test_started_in_a_scope_when_systemd_run_exists_and_the_manager_answers(self):
        self.missing = set()
        self.assertEqual(self.runtime().start([self.ref]), [])
        (args, kwargs), = self.started
        self.assertEqual(args, (["systemd-run", "--user", "--scope", "--quiet", "--collect",
                                "--unit=gpu-game-guard-obs-1700000000", "--", "/usr/bin/obs", "--minimize"],))
        self.assertTrue(kwargs["start_new_session"])

    def test_no_scope_when_the_user_manager_does_not_answer(self):
        self.missing = set()
        for answer in (None, gpu.CommandResult(1, "offline\n", ""), gpu.CommandResult(1, "", "Failed to connect")):
            self.manager = answer
            self.started.clear()
            self.runtime().start([self.ref])
            self.assertEqual(self.started[0][0], (["/usr/bin/obs", "--minimize"],), answer)

    def test_no_scope_without_systemctl(self):
        self.missing = {"systemctl"}
        self.runtime().start([self.ref])
        self.assertEqual(self.started[0][0][0][0], "/usr/bin/obs")

    def test_session_variables_are_added_from_the_user_manager_when_missing(self):
        self.runtime().start([self.ref])
        env = self.started[0][1]["env"]
        self.assertEqual((env["DISPLAY"], env["XDG_RUNTIME_DIR"]), (":1", "/run/user/1000"))
        self.assertNotIn("OTHER", env)

    def test_the_guards_own_session_variables_win(self):
        self.runtime(environ={"DISPLAY": ":9", "WAYLAND_DISPLAY": "w", "XAUTHORITY": "a",
                              "DBUS_SESSION_BUS_ADDRESS": "d", "XDG_RUNTIME_DIR": "/r"}).start([self.ref])
        env = self.started[0][1]["env"]
        self.assertEqual((env["DISPLAY"], env["XDG_RUNTIME_DIR"]), (":9", "/r"))
        self.assertFalse([run for run in self.runs if run[-1] == "show-environment"])

    def test_missing_cwd_is_dropped(self):
        self.runtime().start([dict(self.ref, cwd="/no/such/dir")])
        self.assertIsNone(self.started[0][1]["cwd"])

    def test_restart_off_is_not_a_failure(self):
        self.specs[0] = spec(restart=False)
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertEqual(self.runtime().start([self.ref]), [])
        self.assertEqual(self.started, [])
        self.assertIn("restart is off", log.call_args[0][0])

    def test_a_disabled_spec_still_decides_through_its_restart_flag(self):
        self.specs[0] = spec(enabled=False)
        self.assertEqual(self.runtime().start([self.ref]), [])
        self.assertEqual(len(self.started), 1)

    def test_a_gone_spec_is_a_failure(self):
        self.specs[:] = []
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().start([self.ref]), [self.ref])
        self.assertEqual(self.started, [])

    def test_no_usable_command_line_is_a_failure(self):
        refs = [{k: v for k, v in self.ref.items() if k != "cmdline"}, dict(self.ref, cmdline=[1]),
                dict(self.ref, cmdline=[])]
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertEqual(self.runtime().start(refs), refs)
        self.assertEqual(self.started, [])
        self.assertIn("no usable command line", log.call_args[0][0])

    def test_already_running_is_not_started_again_and_not_a_failure(self):
        self.add(10, "obs")
        self.assertEqual(self.runtime().start([self.ref]), [])
        self.assertEqual(self.started, [])

    def test_an_immediate_non_zero_exit_is_a_failure(self):
        self.child = types.SimpleNamespace(poll=lambda: 3)
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().start([self.ref]), [self.ref])

    def test_an_immediate_zero_exit_is_fine_and_a_running_child_is_watched_for_one_and_a_half_seconds(self):
        self.child = types.SimpleNamespace(poll=lambda: 0)
        self.assertEqual(self.runtime().start([self.ref]), [])
        self.child = types.SimpleNamespace(poll=lambda: None)
        self.now[0] = 0.0
        self.assertEqual(self.runtime().start([self.ref]), [])
        self.assertGreaterEqual(self.now[0], 1.5)
        self.assertLess(self.now[0], 2.0)

    def test_several_programs_share_one_watch(self):
        self.specs[:] = [spec(id=f"p{n}", match=f"pp{n}") for n in range(5)]
        refs = [dict(self.ref, id=f"p{n}") for n in range(5)]
        self.assertEqual(self.runtime().start(refs), [])
        self.assertEqual(len(self.started), 5)
        self.assertLessEqual(self.now[0], 1.6)

    def test_only_the_programs_that_exited_non_zero_are_failed(self):
        self.specs[:] = [spec(id=f"p{n}", match=f"pp{n}") for n in range(3)]
        refs = [dict(self.ref, id=f"p{n}") for n in range(3)]
        codes = iter([0, 7, None])
        runtime = self.runtime()
        runtime._popen = lambda *a, **k: types.SimpleNamespace(poll=lambda code=next(codes): code)
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(runtime.start(refs), [refs[1]])

    def test_a_missing_systemd_run_is_logged_once(self):
        gpu._last_failure.clear()
        runtime = self.runtime()
        with mock.patch("gpu_game_guard.util.log") as log:
            runtime.start([self.ref])
            runtime.start([self.ref])
        lines = [c[0][0] for c in log.call_args_list if "lifetime" in c[0][0]]
        self.assertEqual(len(lines), 1)

    def test_an_unanswering_user_manager_is_logged(self):
        gpu._last_failure.clear()
        self.missing = set()
        self.manager = None
        with mock.patch("gpu_game_guard.util.log") as log:
            self.runtime().start([self.ref])
        self.assertTrue([c for c in log.call_args_list if "share the guard's lifetime" in c[0][0]])

    def test_a_late_exit_during_the_watch_is_a_failure(self):
        codes = iter([None, None, 1])
        self.child = types.SimpleNamespace(poll=lambda: next(codes))
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().start([self.ref]), [self.ref])

    def test_popen_failure_is_logged_and_reported(self):
        runtime = self.runtime()
        runtime._popen = mock.Mock(side_effect=FileNotFoundError("x"))
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertEqual(runtime.start([self.ref]), [self.ref])
        self.assertIn("process start", log.call_args[0][0])

    def test_one_failure_does_not_stop_the_others(self):
        self.specs[:] = [spec(id="a", match="aaa"), spec(id="b", match="bbb")]
        refs = [dict(self.ref, id="a", cmdline=[]), dict(self.ref, id="b")]
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(self.runtime().start(refs), [refs[0]])
        self.assertEqual(len(self.started), 1)


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
