from __future__ import annotations

import tempfile
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

    def run(args, timeout=None):
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


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        gpu._last_failure.clear()

    def test_the_same_failure_is_logged_once_until_the_command_recovers(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            for _ in range(5):
                gpu.run_command(["definitely-not-a-real-binary-xyz"])
            self.assertEqual(log.call_count, 1)
            gpu.run_command(["sh", "-c", "exit 4"])
            gpu.run_command(["sh", "-c", "exit 4"])
            self.assertEqual(log.call_count, 2, "a different failure of another command is logged")
            self.assertEqual(gpu.run_command(["echo", "ok"]), "ok\n")
            gpu.run_command(["definitely-not-a-real-binary-xyz"])
            self.assertEqual(log.call_count, 2, "a different command succeeding does not reset the first one's failure")

    def test_a_failure_logs_again_after_the_same_command_succeeds(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            gpu.run_command(["sh", "-c", "exit 4"])
            self.assertEqual(gpu.run_command(["sh", "-c", "exit 0"]), "")
            gpu.run_command(["sh", "-c", "exit 4"])
            self.assertEqual(log.call_count, 2, "sh failed, sh recovered then failed again: two lines")

    def test_missing_binary(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertIsNone(gpu.run_command(["definitely-not-a-real-binary-xyz"]))
        self.assertEqual(log.call_count, 1)

    def test_non_zero_exit(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertIsNone(gpu.run_command(["sh", "-c", "echo broken >&2; exit 3"]))
        self.assertEqual(log.call_count, 1)
        self.assertIn("broken", log.call_args[0][0])

    def test_timeout(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            self.assertIsNone(gpu.run_command(["sleep", "5"], timeout=0.1))
        self.assertEqual(log.call_count, 1)

    def test_success(self):
        self.assertEqual(gpu.run_command(["echo", "hi"]), "hi\n")


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
        self.assertEqual(gpu.vram_by_container(self.proc / "nope", fake_smi()), {})

    def test_everything_missing_never_raises(self):
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(gpu.vram_by_container(self.proc), {})


if __name__ == "__main__":
    unittest.main()
