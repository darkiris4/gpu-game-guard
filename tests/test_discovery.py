from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path

from gpu_game_guard import discovery, gpu
from gpu_game_guard.config import Config, TargetSpec
from gpu_game_guard.model import Target

DOCKER_ID = "4f96b9e7ddac377fc068d30686c8f0e1d2264f2fed8afdc72b49a7ebdee5f18d"


class FakeRuntime:
    def __init__(self, name, targets):
        self.name = name
        self._targets = targets

    def running_gpu_targets(self):
        return self._targets


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)
        self.mib = {}

    def add(self, pid, comm, cgroup="0::/user.slice/user-1000.slice/session-2.scope\n", cmdline=None, mib=100):
        base = self.proc / str(pid)
        base.mkdir()
        (base / "comm").write_text(comm + "\n")
        (base / "cgroup").write_text(cgroup)
        (base / "cmdline").write_bytes(("\0".join(cmdline or [comm]) + "\0").encode())
        self.mib[pid] = mib

    def discover(self, config=None, runtimes=()):
        smi = "".join(f"{pid}, {mib}\n" for pid, mib in self.mib.items())
        return discovery.discover(config or Config(), list(runtimes), self.proc, run=lambda args, **_: smi)

    def by_key(self, candidates):
        return {c.key: c for c in candidates}


class ProtectTests(unittest.TestCase):
    def test_builtin_list_covers_the_required_families(self):
        names = ["Xorg", "Xwayland", "kwin_wayland", "kwin_x11", "plasmashell", "gnome-shell", "mutter", "cosmic-comp",
                 "sway", "Hyprland", "weston", "pipewire", "pipewire-pulse", "wireplumber", "pulseaudio", "systemd",
                 "systemd-logind", "dbus-daemon", "polkitd", "polkit-gnome-authentication-agent-1", "gdm", "sddm",
                 "lightdm", "steam", "steamwebhelper", "reaper", "pressure-vessel-wrap", "srt-bwrap", "bash", "zsh",
                 "fish", "sh", "ssh", "sshd", "tmux: server", "screen", "konsole", "gnome-terminal-", "xterm", "kitty",
                 "alacritty", "wezterm-gui", "foot", "nautilus", "dolphin", "thunar", "nemo", "gpu-game-guard"]
        for name in names:
            with self.subTest(name):
                self.assertTrue(discovery.is_protected(Config(), name), name)

    def test_ordinary_names_are_not_protected(self):
        for name in ("obs", "ollama", "python3", "blender", "firefox"):
            self.assertFalse(discovery.is_protected(Config(), name), name)

    def test_python_is_protected_only_for_the_guard(self):
        self.assertTrue(discovery.is_protected(Config(), "python3", ["python3", "-m", "gpu_game_guard"]))
        self.assertTrue(discovery.is_protected(Config(), "python3.11", ["/usr/bin/python3", "/usr/bin/gpu-game-guard", "run"]))
        self.assertFalse(discovery.is_protected(Config(), "python3", ["python3", "train.py"]))

    def test_configured_globs_and_case(self):
        config = Config.from_json({"protect": ["my-editor*"]})
        self.assertTrue(discovery.is_protected(config, "My-Editor-bin"))
        self.assertTrue(discovery.is_protected(Config(), "XORG"))
        self.assertTrue(discovery.is_protected(types.SimpleNamespace(), "bash"))
        self.assertFalse(discovery.is_protected(types.SimpleNamespace(), "obs"))


class ContainerTests(Fixture):
    def test_containers_come_first_with_managed_flag(self):
        runtimes = [FakeRuntime("docker", [Target("container", "docker", "ollama", "ollama/ollama", 2048, DOCKER_ID),
                                           Target("container", "docker", "plex", "plex", None, "x")]),
                    FakeRuntime("service", [Target("service", "service", "Ollama", "", 0)])]
        self.add(10, "obs")
        config = Config.from_json({"ignore_containers": ["plex"]})
        found = self.discover(config, runtimes)
        self.assertEqual([c.kind for c in found], ["container", "container", "process"])
        by_key = self.by_key(found)
        ollama, plex = by_key["container:docker:ollama"], by_key["container:docker:plex"]
        self.assertTrue(ollama.managed)
        self.assertFalse(plex.managed)
        self.assertEqual((ollama.risk, ollama.spec, ollama.vram_mib), ("low", None, 2048))
        self.assertIsNone(plex.vram_mib)

    def test_auto_containers_off_means_not_managed(self):
        runtimes = [FakeRuntime("docker", [Target("container", "docker", "a", "", 1, "x")])]
        found = self.discover(Config.from_json({"auto_containers": False}), runtimes)
        self.assertFalse(found[0].managed)

    def test_a_failing_runtime_does_not_hide_the_rest(self):
        class Broken:
            name = "broken"

            def running_gpu_targets(self):
                raise RuntimeError("boom")

        runtimes = [Broken(), FakeRuntime("docker", [Target("container", "docker", "a", "", 1, "x")])]
        from unittest import mock
        with mock.patch("gpu_game_guard.util.log"):
            self.assertEqual(len(self.discover(runtimes=runtimes)), 1)


class HostTests(Fixture):
    def test_services_group_by_unit_and_processes_by_name(self):
        self.add(10, "ollama", "0::/system.slice/ollama.service\n", mib=3000)
        self.add(11, "ollama", "0::/system.slice/ollama.service\n", mib=500)
        self.add(12, "obs", mib=200)
        self.add(13, "obs", mib=100)
        self.add(14, "blender", "0::/user.slice/user-1000.slice/user@1000.service/app.slice/ollama.service\n", mib=1)
        found = self.discover()
        by_key = self.by_key(found)
        self.assertEqual([c.kind for c in found], ["service", "service", "process"])
        system = by_key["service:system:ollama.service"]
        self.assertEqual((system.vram_mib, system.risk, system.managed), (3500, "low", False))
        self.assertEqual(system.spec, {"id": "ollama", "kind": "service", "label": "ollama", "unit": "ollama.service",
                                       "scope": "system"})
        self.assertIn("polkit allow ollama.service", system.note)
        self.assertEqual(by_key["service:user:ollama.service"].spec["id"], "ollama-2")
        proc = by_key["process:obs"]
        self.assertEqual((proc.vram_mib, proc.risk), (300, "high"))
        self.assertEqual(proc.spec, {"id": "obs", "kind": "process", "label": "obs", "match": "obs", "restart": True})
        self.assertIn("unsaved work", proc.note)

    def test_specs_validate_against_the_real_config(self):
        self.add(10, "ollama", "0::/system.slice/ollama.service\n")
        self.add(11, "obs")
        config = Config()
        for candidate in self.discover():
            config = config.with_target(TargetSpec(**candidate.spec))
        self.assertEqual({t.id for t in config.targets}, {"ollama", "obs"})
        self.assertEqual(Config.from_json(config.to_json()).targets, config.targets)

    def test_protected_names_and_units_never_appear(self):
        self.add(10, "Xorg", mib=500)
        self.add(11, "steamwebhelper")
        self.add(12, "gnome-shell")
        self.add(13, "python3", cmdline=["python3", "-m", "gpu_game_guard"])
        self.add(14, "x", "0::/system.slice/gdm.service\n")
        self.add(15, "y", "0::/system.slice/systemd-logind.service\n")
        self.add(16, "my-editor")
        self.add(17, "python3", cmdline=["python3", "train.py"])
        found = self.discover(Config.from_json({"protect": ["my-editor"]}))
        self.assertEqual([c.key for c in found], ["process:python3"])

    def test_container_processes_and_the_guard_itself_are_skipped(self):
        self.add(10, "ollama", f"0::/system.slice/docker-{DOCKER_ID}.scope\n")
        self.add(os.getpid(), "worker")
        self.assertEqual(self.discover(), [])

    def test_already_managed_targets_are_marked(self):
        self.add(10, "ollama", "0::/system.slice/ollama.service\n")
        self.add(11, "ollama", "0::/user.slice/user-1000.slice/user@1000.service/app.slice/ollama.service\n")
        self.add(12, "obs-studio")
        config = (Config().with_target(TargetSpec(id="o", kind="service", unit="ollama.service", scope="system"))
                  .with_target(TargetSpec(id="obs", kind="process", match="obs*")))
        by_key = self.by_key(self.discover(config))
        self.assertTrue(by_key["service:system:ollama.service"].managed)
        self.assertFalse(by_key["service:user:ollama.service"].managed)
        self.assertTrue(by_key["process:obs-studio"].managed)

    def test_ids_do_not_collide_with_existing_targets(self):
        self.add(10, "obs")
        config = Config().with_target(TargetSpec(id="obs", kind="command", stop=["a"], start=["b"]))
        (candidate,) = self.discover(config)
        self.assertEqual(candidate.spec["id"], "obs-2")

    def test_unmeasurable_gpu_memory_yields_only_containers(self):
        found = discovery.discover(Config(), [], self.proc, run=lambda args, **_: None)
        self.assertEqual(found, [])

    def test_odd_names_are_not_offered_as_process_globs(self):
        self.add(10, "we[ird")
        self.add(11, "a*b")
        self.assertEqual(self.discover(), [])

    def test_gpu_module_is_what_measures(self):
        self.assertTrue(callable(gpu.vram_by_pid))


if __name__ == "__main__":
    unittest.main()
