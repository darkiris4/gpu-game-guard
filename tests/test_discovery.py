from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path

from gpu_game_guard import discovery
from gpu_game_guard.config import Config, TargetSpec
from gpu_game_guard.model import Target

UID = 1000
USER_SLICE = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
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

    def add(self, pid, comm, cgroup="0::/user.slice/user-1000.slice/session-2.scope\n", cmdline=None, mib=100, uid=UID):
        base = self.proc / str(pid)
        base.mkdir()
        (base / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (base / "comm").write_text(comm + "\n")
        (base / "cgroup").write_text(cgroup)
        (base / "cmdline").write_bytes(("\0".join(cmdline or [comm]) + "\0").encode())
        self.mib[pid] = mib

    def discover(self, config=None, runtimes=()):
        smi = "".join(f"{pid}, {mib}\n" for pid, mib in self.mib.items())
        return discovery.discover(config or Config(), list(runtimes), self.proc, run=lambda args, **_: smi, uid=UID)

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

    def test_session_daemons_are_protected(self):
        for name in ("bluetoothd", "gnome-keyring-daemon", "xdg-desktop-portal", "xdg-desktop-portal-gtk", "gnome-shell",
                     "org.gnome.Shell.Notifications", "ibus-daemon", "fcitx5", "at-spi-bus-launcher", "at-spi2-registryd",
                     "gvfsd", "gvfs-udisks2-volume-monitor", "polkitd", "udisksd", "upowerd", "NetworkManager",
                     "NetworkManager-dispatcher", "wpa_supplicant", "cupsd", "cups-browsed", "accounts-daemon", "colord",
                     "rtkit-daemon", "systemd-resolved", "dbus-broker", "gamescope", "gamescope-wl"):
            with self.subTest(name):
                self.assertTrue(discovery.is_protected(Config(), name), name)

    def test_app_scoped_units_are_checked_without_prefix_and_instance(self):
        self.assertEqual(discovery._protect_names("app-steam@abc.service"), ["app-steam@abc.service", "app-steam@abc", "steam"])
        self.assertEqual(discovery._protect_names("ollama.service"), ["ollama.service", "ollama"])
        self.assertEqual(discovery.stable_stem("app-obs@3f9a1c.service"), "obs")
        self.assertEqual(discovery.stable_stem("app-obs.service"), "app-obs")
        self.assertEqual(discovery.stable_stem("ollama.service"), "ollama")


class ContainerTests(Fixture):
    def test_containers_come_first_with_managed_flag(self):
        runtimes = [FakeRuntime("docker", [Target("container", "docker", "ollama", "ollama/ollama", 2048, DOCKER_ID),
                                           Target("container", "docker", "plex", "plex", None, "x")]),
                    FakeRuntime("service", [Target("service", "service", "Ollama", "", 0)])]
        self.add(10, "obs")
        config = Config.from_json({"ignore_containers": ["plex"]})
        found = self.discover(config, runtimes)
        self.assertEqual([c.kind for c in found], ["container", "container", "process"])
        self.assertNotIn("service", [c.kind for c in found], "a service runtime's targets are not containers")
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
        self.add(14, "blender", USER_SLICE + "ollama.service\n", mib=1)
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

    def test_unknown_memory_still_offers_candidates_with_none(self):
        self.add(10, "ollama", "0::/system.slice/ollama.service\n", mib="[N/A]")
        self.add(11, "obs", mib="[N/A]")
        by_key = self.by_key(self.discover())
        self.assertIsNone(by_key["service:system:ollama.service"].vram_mib)
        self.assertIsNone(by_key["process:obs"].vram_mib)

    def test_one_unknown_pid_makes_the_total_unknown_not_undercounted(self):
        self.add(10, "ollama", "0::/system.slice/ollama.service\n", mib=3000)
        self.add(11, "ollama", "0::/system.slice/ollama.service\n", mib="[N/A]")
        self.add(12, "obs", mib=200)
        by_key = self.by_key(self.discover())
        self.assertIsNone(by_key["service:system:ollama.service"].vram_mib)
        self.assertEqual(by_key["process:obs"].vram_mib, 200)

    def test_a_name_with_too_many_root_processes_is_not_offered(self):
        self.add(10, "chrome")
        for pid in range(11, 71):
            self.add(pid, "chrome", mib=1)
        for pid in range(10, 71):
            (self.proc / str(pid) / "stat").write_text(f"{pid} (chrome) S {10 if pid > 10 else 1} " + " ".join(["0"] * 17) + " 5 0 0\n")
        self.assertEqual([c.key for c in self.discover()], ["process:chrome"])
        for pid in range(11, 40):
            (self.proc / str(pid) / "stat").write_text(f"{pid} (chrome) S 1 " + " ".join(["0"] * 17) + " 5 0 0\n")
        self.assertEqual(self.discover(), [])

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

    def test_services_are_protected_by_the_comm_of_their_processes(self):
        self.add(10, "gnome-shell", "0::/system.slice/shell-helper.service\n")
        self.add(11, "worker", "0::/system.slice/mixed.service\n")
        self.add(12, "ibus-daemon", "0::/system.slice/mixed.service\n")
        self.add(13, "worker", "0::/system.slice/plain.service\n")
        self.assertEqual([c.key for c in self.discover()], ["service:system:plain.service"])

    def test_a_unit_with_a_python_guard_process_is_protected(self):
        self.add(10, "python3", "0::/system.slice/guardian.service\n", cmdline=["python3", "-m", "gpu_game_guard"])
        self.assertEqual(self.discover(), [])

    def test_app_forms_are_protected_and_other_apps_get_a_stable_id(self):
        self.add(10, "worker", USER_SLICE + "app-steam@abc123.service\n")
        self.add(11, "worker", USER_SLICE + "app-gamescope@x.service\n")
        self.add(12, "worker", USER_SLICE + "app-blender@9f8e7d.service\n")
        (candidate,) = self.discover()
        self.assertEqual(candidate.spec["id"], "blender")
        self.assertEqual(candidate.label, "blender")
        self.assertEqual(candidate.spec["unit"], "app-blender@9f8e7d.service")

    def test_only_the_current_users_units_and_processes_are_offered(self):
        self.add(10, "worker", "0::/user.slice/user-1001.slice/user@1001.service/app.slice/theirs.service\n")
        self.add(11, "worker", USER_SLICE + "mine.service\n")
        self.add(12, "theirproc", uid=1001)
        self.add(13, "myproc")
        self.add(14, "worker", "0::/system.slice/shared.service\n", uid=0)
        self.assertEqual(sorted(c.key for c in self.discover()),
                         ["process:myproc", "service:system:shared.service", "service:user:mine.service"])

    def test_processes_with_unreadable_ownership_are_not_offered(self):
        self.add(10, "worker")
        (self.proc / "10" / "status").unlink()
        self.assertEqual(self.discover(), [])

    def test_names_too_short_to_be_matched_are_not_offered(self):
        self.add(10, "ab")
        self.add(11, "abc")
        self.assertEqual([c.key for c in self.discover()], ["process:abc"])

    def test_container_processes_and_the_guard_itself_are_skipped(self):
        self.add(10, "ollama", f"0::/system.slice/docker-{DOCKER_ID}.scope\n")
        self.add(os.getpid(), "worker")
        self.assertEqual(self.discover(), [])

    def test_already_managed_targets_are_marked(self):
        self.add(10, "ollama", "0::/system.slice/ollama.service\n")
        self.add(11, "ollama", USER_SLICE + "ollama.service\n")
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
        found = discovery.discover(Config(), [], self.proc, run=lambda args, **_: None, uid=UID)
        self.assertEqual(found, [])

    def test_odd_names_are_not_offered_as_process_globs(self):
        self.add(10, "we[ird")
        self.add(11, "a*b")
        self.assertEqual(self.discover(), [])

    def test_the_default_uid_is_the_running_users(self):
        self.add(10, "mine", uid=os.getuid())
        smi = "10, 5\n"
        found = discovery.discover(Config(), [], self.proc, run=lambda args, **_: smi)
        self.assertEqual([c.key for c in found], ["process:mine"])


if __name__ == "__main__":
    unittest.main()
