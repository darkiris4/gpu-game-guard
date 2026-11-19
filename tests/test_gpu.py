from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from gpu_game_guard import gpu

CONTAINER = "4f96b9e7ddac377fc068d30686c8f0e1d2264f2fed8afdc72b49a7ebdee5f18d"
OTHER = "a5c7e80f694bd5b70957b072d117a7340b123c47a16ceadbc6b1c7868633bec2"
DOCKER_CGROUP = f"0::/system.slice/docker-{CONTAINER}.scope\n"


def fake_smi(**outputs):
    """A runner answering nvidia-smi by the query flag it was given; a missing key behaves like a failure."""
    calls = []

    def run(args, timeout=None, keep_output=False):
        calls.append(list(args))
        assert args[0] == "nvidia-smi" and args[-1] == "--format=csv,noheader,nounits"
        return outputs.get(args[1].split("=")[1].split(",")[0])

    run.calls = calls
    return run


class CgroupTests(unittest.TestCase):
    def test_docker_paths(self):
        self.assertEqual(gpu.container_id_from_cgroup(DOCKER_CGROUP), CONTAINER)
        self.assertEqual(gpu.container_id_from_cgroup(f"12:devices:/docker/{CONTAINER}\n"), CONTAINER)

    def test_podman_paths(self):
        self.assertEqual(gpu.container_id_from_cgroup(
            f"0::/user.slice/user-1000.slice/user@1000.service/user.slice/libpod-{CONTAINER}.scope/container\n"), CONTAINER)
        self.assertEqual(gpu.container_id_from_cgroup(f"1:name=systemd:/machine.slice/libpod-{CONTAINER}/ctr\n"), CONTAINER)

    def test_not_a_container(self):
        self.assertIsNone(gpu.container_id_from_cgroup("0::/user.slice/user-1000.slice/session-2.scope\n"))
        self.assertIsNone(gpu.container_id_from_cgroup(""))
        self.assertIsNone(gpu.container_id_from_cgroup("0::/system.slice/docker-abc.scope\n"))


def py(code, *extra):
    """An argument list running a Python snippet with the interpreter running the tests."""
    return [sys.executable, "-c", code, *extra]


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        gpu._last_failure.clear()

    def test_the_same_failure_is_logged_once_until_the_command_recovers(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            for _ in range(5):
                gpu.run_command(["definitely-not-a-real-binary-xyz"])
            self.assertEqual(log.call_count, 1)
            gpu.run_command(py("import sys; sys.exit(4)"))
            gpu.run_command(py("import sys; sys.exit(4)"))
            self.assertEqual(log.call_count, 2, "a different failure of another command is logged")
            self.assertEqual(gpu.run_command(py("print('ok')")), "ok\n")
            gpu.run_command(["definitely-not-a-real-binary-xyz"])
            self.assertEqual(log.call_count, 2, "a different command succeeding does not reset the first one's failure")

    def test_a_failure_logs_again_after_the_same_command_succeeds(self):
        code = "import sys; sys.exit(int(sys.argv[1]))"
        with mock.patch("gpu_game_guard.util.log") as log:
            gpu.run_command(py(code, "4"))
            self.assertEqual(gpu.run_command(py(code, "0")), "")
            gpu.run_command(py(code, "4"))
            self.assertEqual(log.call_count, 2, "the same program failed, recovered, then failed again: two lines")

    def test_subcommand_failures_are_tracked_separately(self):
        script = "import sys; sys.exit(int(sys.argv[1]))"
        with mock.patch("gpu_game_guard.util.log") as log:
            gpu.run_command(["definitely-not-a-real-binary-xyz", "a"])
            gpu.run_command(["definitely-not-a-real-binary-xyz", "b"])
            gpu.run_command(["definitely-not-a-real-binary-xyz", "a"])
            self.assertEqual(log.call_count, 2, "the second argument is part of the key")
            gpu.run_command(py(script, "1"))
            gpu.run_command(py(script, "0"))
            gpu.run_command(py(script, "1"))
            self.assertEqual(log.call_count, 4)

    def test_failure_keys_are_bounded(self):
        with mock.patch("gpu_game_guard.util.log"):
            for number in range(gpu._MAX_FAILURE_KEYS * 3):
                gpu._report_failure(f"cmd{number} sub", "failed")
        self.assertLessEqual(len(gpu._last_failure), gpu._MAX_FAILURE_KEYS)

    def test_keep_output_on_failure(self):
        script = "import sys; print('partial'); sys.stderr.write('broken\\n'); sys.exit(1)"
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertIsNone(gpu.run_command(py(script)))
            self.assertEqual(gpu.run_command(py(script), keep_output=True), "partial\n")
            self.assertIsNone(gpu.run_command(py("import sys; sys.stderr.write('broken\\n'); sys.exit(1)"), keep_output=True))
        self.assertEqual(log.call_count, 2, "the same failure is logged once; the other program has its own key")

    def test_embedded_nul_and_undecodable_output(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertIsNone(gpu.run_command([sys.executable, "a\x00b"]))
            out = gpu.run_command(py("import sys; sys.stdout.buffer.write(b'\\xffok')"))
        self.assertEqual(log.call_count, 1)
        self.assertTrue(out.endswith("ok"))

    def test_missing_binary(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertIsNone(gpu.run_command(["definitely-not-a-real-binary-xyz"]))
        self.assertEqual(log.call_count, 1)

    def test_non_zero_exit(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertIsNone(gpu.run_command(py("import sys; sys.stderr.write('broken\\n'); sys.exit(3)")))
        self.assertEqual(log.call_count, 1)
        self.assertIn("broken", log.call_args[0][0])
        self.assertIn("status 3", log.call_args[0][0])

    def test_timeout(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertIsNone(gpu.run_command(py("import time; time.sleep(5)"), timeout=0.1))
        self.assertEqual(log.call_count, 1)

    def test_success(self):
        self.assertEqual(gpu.run_command(py("print('hi')")), "hi\n")


class ExecutionTests(unittest.TestCase):
    """How commands the guard runs itself are executed: files instead of pipes, own session, bounded output."""

    def setUp(self):
        gpu._last_failure.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_background_grandchild_holding_the_output_does_not_delay_the_call(self):
        pidfile = Path(self.tmp.name) / "pid"
        code = ("import subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                "open(sys.argv[1], 'w').write(str(child.pid))\n"
                "print('parent done')\n")
        started = time.monotonic()
        result = gpu.run_process(py(code, str(pidfile)), timeout=20)
        elapsed = time.monotonic() - started
        grandchild = int(pidfile.read_text())
        try:
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "parent done"))
            self.assertLess(elapsed, 10)
        finally:
            os.kill(grandchild, signal.SIGKILL)

    def test_a_timeout_ends_the_whole_process_group(self):
        pidfile = Path(self.tmp.name) / "pid"
        code = ("import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "open(sys.argv[1], 'w').write(str(child.pid))\n"
                "time.sleep(60)\n")
        with mock.patch("gpu_game_guard.util.log"):
            self.assertIsNone(gpu.run_process(py(code, str(pidfile)), timeout=1.5))
        grandchild = int(pidfile.read_text())
        try:
            for _ in range(50):
                try:
                    state = Path(f"/proc/{grandchild}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except OSError:
                    state = "gone"
                if state in ("gone", "Z"):
                    break
                time.sleep(0.1)
            self.assertIn(state, ("gone", "Z"))
        finally:
            try:
                os.kill(grandchild, signal.SIGKILL)
            except OSError:
                pass

    def test_a_command_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        started = time.monotonic()
        with mock.patch("gpu_game_guard.util.log"), mock.patch.object(gpu, "_KILL_GRACE", 0.5):
            self.assertIsNone(gpu.run_process(py(code), timeout=0.5))
        self.assertLess(time.monotonic() - started, 10)

    def test_the_command_runs_in_its_own_session(self):
        result = gpu.run_process(py("import os; print(os.getsid(0) == os.getpid())"))
        self.assertEqual(result.stdout.strip(), "True")

    def test_output_is_read_back_up_to_one_mebibyte_per_stream(self):
        code = "import sys; sys.stdout.write('a' * (3 * 1024 * 1024)); sys.stderr.write('b' * (2 * 1024 * 1024))"
        result = gpu.run_process(py(code))
        self.assertEqual((len(result.stdout), len(result.stderr)), (1024 * 1024, 1024 * 1024))

    def test_stdin_is_closed(self):
        result = gpu.run_process(py("import sys; print(repr(sys.stdin.read()))"))
        self.assertEqual(result.stdout.strip(), "''")


class LoggingTests(unittest.TestCase):
    def setUp(self):
        gpu._last_failure.clear()

    def test_the_key_is_the_first_two_arguments(self):
        self.assertEqual(gpu._command_key(["systemctl", "--user", "stop", "x"]), "systemctl --user")
        self.assertEqual(gpu._command_key(["ls"]), "ls")

    def test_interpreter_programs_are_keyed_by_a_hash_of_the_program(self):
        one = gpu._command_key(["python3", "-c", "print(1)"])
        two = gpu._command_key(["python3", "-c", "print(2)"])
        self.assertNotEqual(one, two)
        self.assertEqual(one, gpu._command_key(["python3", "-c", "print(1)", "extra"]))
        self.assertTrue(one.startswith("python3 "))
        self.assertNotIn("print", one)
        self.assertNotEqual(gpu._command_key(["python3", "-m", "a"]), gpu._command_key(["python3", "-m", "b"]))
        self.assertEqual(gpu._command_key(["python3", "script.py", "x"]), "python3 script.py")

    def test_different_programs_through_one_interpreter_log_separately(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            gpu.run_command(py("import sys; sys.exit(1)"))
            gpu.run_command(py("import sys; sys.exit(2)"))
        self.assertEqual(log.call_count, 2)

    def test_logged_text_is_truncated(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            gpu.log_once("k" * 500, "m" * 500)
        line = log.call_args[0][0]
        self.assertLessEqual(len(line), 200 + 2 + 200)
        self.assertTrue(line.endswith("..."))

    def test_a_long_error_detail_is_truncated(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            gpu.run_command(py("import sys; sys.stderr.write('x' * 1000); sys.exit(1)"))
        self.assertLess(len(log.call_args[0][0]), 450)


class ValidUnitTests(unittest.TestCase):
    def test_accepted(self):
        for unit in ("ollama.service", "getty@tty1.service", "a:b_c-d.service", "A.1.service"):
            self.assertTrue(gpu.valid_unit(unit), unit)

    def test_refused(self):
        for unit in ("-x.service", "--user.service", "a*.service", "a?.service", "a[b].service", "a b.service",
                     "a/b.service", "x.timer", ".service", "", "x.service\n", None, 5):
            self.assertFalse(gpu.valid_unit(unit), repr(unit))


class TotalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.drm = Path(self.tmp.name)

    def card(self, name, used, total):
        device = self.drm / name / "device"
        device.mkdir(parents=True)
        (device / "mem_info_vram_used").write_text(f"{used}\n")
        (device / "mem_info_vram_total").write_text(f"{total}\n")

    def test_nvidia_sums_gpus(self):
        run = fake_smi(**{"memory.used": "1000, 8192\n2000, 8192\n"})
        self.assertEqual(gpu.totals(run, self.drm), (3000, 16384))

    def test_nvidia_preferred_over_sysfs(self):
        self.card("card0", 1 << 30, 8 << 30)
        run = fake_smi(**{"memory.used": "5, 10\n"})
        self.assertEqual(gpu.totals(run, self.drm), (5, 10))

    def test_sysfs_when_nvidia_missing(self):
        self.card("card0", 2048 * 1024 * 1024, 16384 * 1024 * 1024)
        self.card("card1", 1024 * 1024 * 1024, 8192 * 1024 * 1024)
        (self.drm / "card0-DP-1").mkdir()
        self.assertEqual(gpu.totals(fake_smi(), self.drm), (3072, 24576))

    def test_unparseable_nvidia_output(self):
        run = fake_smi(**{"memory.used": "[N/A], [N/A]\n"})
        self.assertIsNone(gpu.totals(run, self.drm))

    def test_bad_rows_are_skipped_whole(self):
        run = fake_smi(**{"memory.used": "100, [N/A]\ninf, 8\n7, inf\nnan, 5\n500, 1000\n"})
        self.assertEqual(gpu.totals(run, self.drm), (500, 1000))

    def test_nothing_known(self):
        self.assertIsNone(gpu.totals(fake_smi(), self.drm / "missing"))

    def test_unreadable_sysfs_values_skipped(self):
        self.card("card0", "oops", 100)
        self.assertIsNone(gpu.totals(fake_smi(), self.drm))


class VramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)

    def process(self, pid, cgroup, fdinfo=()):
        base = self.proc / str(pid)
        (base / "fdinfo").mkdir(parents=True)
        if cgroup is not None:
            (base / "cgroup").write_text(cgroup)
        for number, text in enumerate(fdinfo, start=3):
            (base / "fdinfo" / str(number)).write_text(text)

    def test_nvidia_sums_per_container_and_skips_the_rest(self):
        self.process(100, DOCKER_CGROUP)
        self.process(101, DOCKER_CGROUP)
        self.process(102, f"0::/system.slice/docker-{OTHER}.scope\n")
        self.process(103, "0::/user.slice/session-2.scope\n")
        run = fake_smi(pid="100, 1000\n101, 524\n102, 300\n103, 77\n999, 5\nabc, [N/A]\n104, [N/A]\n")
        self.assertEqual(gpu.vram_by_container(self.proc, run), {CONTAINER: 1524, OTHER: 300})

    def test_nvidia_without_compute_apps_is_measured_and_skips_fdinfo(self):
        self.process(200, DOCKER_CGROUP, ["drm-driver:\tamdgpu\ndrm-client-id:\t1\ndrm-memory-vram:\t5 MiB\n"])
        self.assertEqual(gpu.vram_by_container(self.proc, fake_smi(pid="")), {})

    def test_nvidia_infinite_memory_row_is_skipped(self):
        self.process(100, DOCKER_CGROUP)
        run = fake_smi(pid="100, inf\n100, 10\n")
        self.assertEqual(gpu.vram_by_container(self.proc, run), {CONTAINER: 10})

    def test_fdinfo_amd(self):
        client = "pos:\t0\ndrm-driver:\tamdgpu\ndrm-client-id:\t{id}\ndrm-memory-vram:\t{kib} KiB\ndrm-memory-gtt:\t999 KiB\n"
        self.process(200, DOCKER_CGROUP, [
            client.format(id=7, kib=1024 * 512),
            client.format(id=7, kib=1024 * 512),  # the same client through a duplicated descriptor
            client.format(id=8, kib=1024 * 100),
            "pos:\t0\nflags:\t02\n",  # not a DRM file
        ])
        self.process(201, DOCKER_CGROUP, [client.format(id=7, kib=1024 * 50)])  # same id, other process
        self.assertEqual(gpu.vram_by_container(self.proc, fake_smi()), {CONTAINER: 512 + 100 + 50})

    def test_fdinfo_clients_are_keyed_by_device(self):
        client = "drm-driver:\tamdgpu\ndrm-pdev:\t{pdev}\ndrm-client-id:\t7\ndrm-memory-vram:\t{mib} MiB\n"
        self.process(210, DOCKER_CGROUP, [
            client.format(pdev="0000:03:00.0", mib=100),
            client.format(pdev="0000:03:00.0", mib=100),  # the same client again
            client.format(pdev="0000:04:00.0", mib=40),  # same id on another device
        ])
        self.assertEqual(gpu.vram_by_container(self.proc, fake_smi()), {CONTAINER: 140})

    def test_fdinfo_intel_local_memory(self):
        self.process(310, DOCKER_CGROUP, ["drm-driver:\ti915\ndrm-client-id:\t3\ndrm-total-local0:\t256 MiB\n"])
        self.assertEqual(gpu.vram_by_container(self.proc, fake_smi()), {CONTAINER: 256})

    def test_fdinfo_without_drm_files_is_not_measured(self):
        self.process(320, DOCKER_CGROUP, ["pos:\t0\nflags:\t02\n"])
        self.assertIsNone(gpu.vram_by_container(self.proc, fake_smi()))

    def test_fdinfo_intel_mib_units(self):
        self.process(300, f"0::/user.slice/libpod-{OTHER}.scope/container\n", [
            "drm-driver:\txe\ndrm-client-id:\t3\ndrm-total-vram0:\t900 MiB\ndrm-resident-vram0:\t640 MiB\n",
        ])
        self.assertEqual(gpu.vram_by_container(self.proc, fake_smi()), {OTHER: 900})

    def test_fdinfo_ignores_host_processes_and_zero(self):
        self.process(400, "0::/user.slice/session-2.scope\n", ["drm-driver:\ti915\ndrm-client-id:\t1\ndrm-memory-vram:\t5 MiB\n"])
        self.process(401, DOCKER_CGROUP, ["drm-driver:\ti915\ndrm-client-id:\t1\ndrm-memory-vram:\t0 KiB\n"])
        self.assertEqual(gpu.vram_by_container(self.proc, fake_smi()), {})

    def test_vanishing_processes_do_not_raise(self):
        self.process(500, None, ["drm-driver:\tamdgpu\ndrm-client-id:\t1\ndrm-memory-vram:\t5 MiB\n"])
        (self.proc / "501").mkdir()  # no fdinfo directory
        (self.proc / "self").mkdir()
        (self.proc / "meminfo").write_text("x")
        self.assertEqual(gpu.vram_by_container(self.proc, fake_smi()), {})

    def test_missing_proc_root(self):
        self.assertIsNone(gpu.vram_by_container(self.proc / "nope", fake_smi()))

    def test_everything_missing_never_raises(self):
        with mock.patch("gpu_game_guard.util.log"):
            result = gpu.vram_by_container(self.proc / "nope")
        self.assertTrue(result is None or isinstance(result, dict))


class VramByPidTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)

    def drm_process(self, pid, text):
        base = self.proc / str(pid) / "fdinfo"
        base.mkdir(parents=True)
        (base / "3").write_text(text)

    def test_nvidia_is_summed_per_pid_and_authoritative(self):
        def run(args, **_):
            return "10, 100\n10, 50\n11, 7\nbad row\n"

        result = gpu.vram_by_pid(self.proc, run)
        self.assertEqual(dict(result), {10: 150, 11: 7})
        self.assertTrue(result.authoritative)

    def test_nvidia_with_no_compute_apps_is_empty_not_none(self):
        self.assertEqual(gpu.vram_by_pid(self.proc, lambda args, **_: ""), {})

    def test_fdinfo_fallback_is_not_authoritative(self):
        self.drm_process(20, "drm-driver: amdgpu\ndrm-client-id: 1\ndrm-memory-vram: 4096 KiB\n")
        result = gpu.vram_by_pid(self.proc, lambda args, **_: None)
        self.assertEqual(dict(result), {20: 4})
        self.assertFalse(result.authoritative)

    def test_unmeasurable_is_none(self):
        self.assertIsNone(gpu.vram_by_pid(self.proc, lambda args, **_: None))
        self.assertIsNone(gpu.vram_by_pid(self.proc / "missing", lambda args, **_: None))

    def test_memory_for_pids(self):
        authoritative = gpu.PidMeasurement({1: 10, 2: 5})
        self.assertEqual(gpu.memory_for_pids(authoritative, [1, 2, 3]), 15)
        self.assertEqual(gpu.memory_for_pids(authoritative, [9]), 0)
        self.assertEqual(gpu.memory_for_pids(authoritative, []), 0)
        partial = gpu.PidMeasurement({1: 10})
        partial.authoritative = False
        self.assertEqual(gpu.memory_for_pids(partial, [1]), 10)
        self.assertIsNone(gpu.memory_for_pids(partial, [9]))
        self.assertIsNone(gpu.memory_for_pids(None, [1]))


class CgroupHelperTests(unittest.TestCase):
    def test_path_prefers_the_unified_line(self):
        self.assertEqual(gpu.cgroup_path("12:cpu:/old\n0::/system.slice/a.service\n"), "/system.slice/a.service")
        self.assertEqual(gpu.cgroup_path("1:name=systemd:/x.slice\n"), "/x.slice")
        self.assertEqual(gpu.cgroup_path(""), "")

    def test_in_unit_compares_whole_components(self):
        text = "0::/system.slice/ollama.service\n"
        self.assertTrue(gpu.cgroup_in_unit(text, "ollama.service"))
        self.assertFalse(gpu.cgroup_in_unit(text, "llama.service"))
        self.assertTrue(gpu.cgroup_in_unit("0::/system.slice/ollama.service/sub\n", "ollama.service"))

    def test_service_from_cgroup(self):
        cases = {
            "0::/system.slice/ollama.service\n": ("system", "ollama.service"),
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/ollama.service\n": ("user", "ollama.service"),
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-obs-1.scope\n": None,
            "0::/user.slice/user-1000.slice/session-3.scope\n": None,
            "0::/user.slice/user-1000.slice/user@1000.service/init.scope\n": None,
            "0::/system.slice/docker-" + "a" * 64 + ".scope\n": None,
            "0::/\n": None,
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(gpu.service_from_cgroup(text), expected)


class HostSpecTests(unittest.TestCase):
    def config(self, *specs):
        return lambda: types.SimpleNamespace(targets=list(specs))

    def test_valid_specs_of_one_kind_and_a_bad_one_does_not_hide_the_rest(self):
        good = types.SimpleNamespace(id="a", kind="service", unit="a.service", scope="system", enabled=True)
        bad = types.SimpleNamespace(id="b", kind="service", unit="nope", scope="system", enabled=True)
        off = types.SimpleNamespace(id="c", kind="service", unit="c.service", scope="user", enabled=False)
        other = types.SimpleNamespace(id="d", kind="process", match="x", enabled=True)
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertEqual(gpu.usable_specs(self.config(bad, good, off, other), "service"), [good])
            gpu.usable_specs(self.config(bad, good, off, other), "service")
        self.assertEqual(log.call_count, 1)

    def test_an_empty_list_is_silent_and_an_unusable_config_logs_once(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertEqual(gpu.usable_specs(self.config(), "command"), [])
            self.assertEqual(gpu.usable_specs(self.config(), "command"), [])
            self.assertEqual(log.call_count, 0)
            self.assertEqual(gpu.usable_specs(lambda: types.SimpleNamespace(targets=None), "process"), [])
            self.assertEqual(gpu.usable_specs(lambda: types.SimpleNamespace(targets=None), "process"), [])
            self.assertEqual(gpu.usable_specs(lambda: 1 / 0, "service"), [])
            self.assertEqual(log.call_count, 2)

    def test_disabled_specs_are_returned_on_request(self):
        off = types.SimpleNamespace(id="c", kind="service", unit="c.service", scope="user", enabled=False)
        self.assertEqual(gpu.usable_specs(self.config(off), "service"), [])
        self.assertEqual(gpu.usable_specs(self.config(off), "service", include_disabled=True), [off])
        self.assertIs(gpu.find_spec(self.config(off), "service", {"id": "c"}, include_disabled=True), off)

    def test_service_units_are_validated_strictly(self):
        bad = [types.SimpleNamespace(id=f"b{n}", kind="service", unit=unit, scope="system", enabled=True)
               for n, unit in enumerate(("-x.service", "a*.service", "a[b].service"))]
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertEqual(gpu.usable_specs(self.config(*bad), "service"), [])
        self.assertEqual(log.call_count, 3)

    def test_find_spec_and_own_refs(self):
        spec = types.SimpleNamespace(id="a", kind="process", match="x", enabled=True)
        self.assertIs(gpu.find_spec(self.config(spec), "process", {"id": "a"}), spec)
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertIsNone(gpu.find_spec(self.config(spec), "process", {"id": "gone", "name": "Gone"}))
        self.assertEqual(log.call_count, 1)
        refs = [{"kind": "process", "runtime": "process"}, {"kind": "container", "runtime": "docker"}, "x"]
        self.assertEqual(gpu.own_refs(refs, "process", "process"), refs[:1])
        self.assertEqual(gpu.display_name(types.SimpleNamespace(id="a", label="")), "a")
        self.assertEqual(gpu.display_name(types.SimpleNamespace(id="a", label="Label")), "Label")


class RunProcessTests(unittest.TestCase):
    def test_exit_status_is_returned_not_logged(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            result = gpu.run_process([sys.executable, "-c", "import sys; print('x'); sys.exit(3)"])
        self.assertEqual((result.returncode, result.stdout.strip()), (3, "x"))
        log.assert_not_called()

    def test_missing_command_is_none_and_logged_once(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertIsNone(gpu.run_process(["no-such-command-xyz"]))
            self.assertIsNone(gpu.run_process(["no-such-command-xyz"]))
        self.assertEqual(log.call_count, 1)

    def test_timeout_is_none(self):
        with mock.patch("gpu_game_guard.util.log"):
            self.assertIsNone(gpu.run_process([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2))


class MeasurementTests(unittest.TestCase):
    def test_nvidia_results_are_authoritative_and_fdinfo_results_are_not(self):
        with tempfile.TemporaryDirectory() as root:
            result = gpu.vram_by_container(Path(root), run=lambda *a, **k: "")
            self.assertEqual(result, {})
            self.assertTrue(result.authoritative)
            proc = Path(root)
            (proc / "10" / "fdinfo").mkdir(parents=True)
            (proc / "10" / "cgroup").write_text("0::/system.slice/docker-" + "a" * 64 + ".scope\n")
            (proc / "10" / "fdinfo" / "3").write_text("drm-driver: amdgpu\ndrm-client-id: 1\ndrm-memory-vram: 2048 KiB\n")
            result = gpu.vram_by_container(proc, run=lambda *a, **k: None)
            self.assertEqual(result, {"a" * 64: 2})
            self.assertFalse(result.authoritative)


class NotAvailableMemoryTests(unittest.TestCase):
    """nvidia-smi prints "[N/A]" for processes whose memory it cannot report; that is unknown, never zero."""

    def smi(self, rows):
        return lambda args, **_: rows

    def test_a_pid_without_a_figure_makes_the_reading_non_authoritative(self):
        result = gpu.vram_by_pid(Path("/nonexistent"), run=self.smi("10, 300\n11, [N/A]\n"))
        self.assertEqual(dict(result), {10: 300})
        self.assertFalse(result.authoritative)
        self.assertEqual(result.unknown, {11})
        self.assertIsNone(gpu.memory_for_pids(result, [11]))
        self.assertIsNone(gpu.memory_for_pids(result, [10, 11]), "a partial sum is not offered as the figure")
        self.assertEqual(gpu.memory_for_pids(result, [10]), 300)
        self.assertIsNone(gpu.memory_for_pids(result, [99]))

    def test_a_complete_reading_stays_authoritative(self):
        result = gpu.vram_by_pid(Path("/nonexistent"), run=self.smi("10, 300\n"))
        self.assertTrue(result.authoritative)
        self.assertEqual(gpu.memory_for_pids(result, [99]), 0)

    def test_a_container_with_an_unreported_process_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root)
            for pid, container in (("10", CONTAINER), ("11", OTHER)):
                (proc / pid).mkdir()
                (proc / pid / "cgroup").write_text(f"0::/system.slice/docker-{container}.scope\n")
            result = gpu.vram_by_container(proc, run=self.smi("10, 100\n11, [N/A]\n"))
        self.assertEqual(dict(result), {CONTAINER: 100})
        self.assertFalse(result.authoritative)
        self.assertEqual(result.unknown, {OTHER})

    def test_the_user_manager_uid_of_a_cgroup(self):
        own = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/x.service\n"
        self.assertEqual(gpu.user_manager_uid(own), 1000)
        self.assertIsNone(gpu.user_manager_uid("0::/system.slice/x.service\n"))


if __name__ == "__main__":
    unittest.main()


class UnknownMemoryHelperTests(unittest.TestCase):
    def test_measured_pids_include_pids_listed_without_a_figure(self):
        by_pid = gpu.PidMeasurement({1: 10})
        by_pid.unknown.add(2)
        self.assertEqual(gpu.measured_pids(by_pid), {1, 2})
        self.assertEqual(gpu.measured_pids(None), set())
        self.assertIsNone(gpu.memory_for_pids(by_pid, [1, 2]))

    def test_root_pids_are_those_whose_parent_is_not_in_the_set(self):
        self.assertEqual(gpu.root_pids({10: 1, 11: 10, 12: 10, 20: 1}), [10, 20])
