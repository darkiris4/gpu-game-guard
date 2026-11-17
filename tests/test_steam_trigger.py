from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from gpu_game_guard import triggers
from gpu_game_guard.model import Launch
from gpu_game_guard.triggers import steam
from gpu_game_guard.triggers.steam import Game, SteamTrigger


def game(pid, app_id=730, flatpak=False):
    return Game(pid, str(pid * 10), app_id, flatpak)


def fake_proc(root, pid, argv, ppid=1, start="123456", state="S"):
    entry = Path(root) / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
    fields = [state, str(ppid)] + ["0"] * 17 + [start]
    (entry / "stat").write_text(f"{pid} (rea per) " + " ".join(fields) + "\n")


class ParseTests(unittest.TestCase):
    def test_native_launcher(self):
        argv = ["/home/u/.local/share/Steam/ubuntu12_32/reaper", "SteamLaunch", "AppId=730", "--", "x"]
        self.assertEqual(steam.parse_launch(argv), (730, False))

    def test_flatpak_launcher(self):
        argv = ["/home/u/.var/app/com.valvesoftware.Steam/.local/share/Steam/ubuntu12_32/reaper", "SteamLaunch",
                "AppId=730", "--", "x"]
        self.assertEqual(steam.parse_launch(argv), (730, True))

    def test_other_processes_are_not_launches(self):
        self.assertIsNone(steam.parse_launch(["steam", "steam://rungameid/730"]))
        self.assertIsNone(steam.parse_launch(["/usr/bin/reaper", "Other", "AppId=1"]))
        self.assertIsNone(steam.parse_launch(["reaper", "SteamLaunch", "AppId=abc"]))
        self.assertIsNone(steam.parse_launch(["reaper", "SteamLaunch", "--", "AppId=1"]))
        self.assertIsNone(steam.parse_launch([]))

    def test_game_ids(self):
        self.assertEqual(game(1, 730).game_id, 730)
        self.assertEqual(game(1, 2 ** 31 - 1).game_id, 2 ** 31 - 1)
        self.assertEqual(game(1, 3000000000).game_id, (3000000000 << 32) | 0x02000000)


class ProcTests(unittest.TestCase):
    def test_scan_finds_launchers_and_ignores_everything_else(self):
        with tempfile.TemporaryDirectory() as root:
            fake_proc(root, 100, ["/s/ubuntu12_32/reaper", "SteamLaunch", "AppId=730", "--", "game"], start="42")
            fake_proc(root, 101, ["/usr/bin/python3", "script.py"])
            fake_proc(root, 102, ["reaper", "SteamLaunch", "AppId=3000000000", "--"])
            (Path(root) / "self").mkdir()
            (Path(root) / "103").mkdir()  # vanished between listing and reading
            found = steam.scan_games(Path(root))
        self.assertEqual(sorted((g.pid, g.app_id, g.start_time) for g in found),
                         [(100, 730, "42"), (102, 3000000000, "123456")])

    def test_descendants_walks_the_whole_tree(self):
        with tempfile.TemporaryDirectory() as root:
            fake_proc(root, 10, ["a"], ppid=1)
            fake_proc(root, 11, ["b"], ppid=10)
            fake_proc(root, 12, ["c"], ppid=11)
            fake_proc(root, 13, ["d"], ppid=999)
            self.assertEqual(sorted(steam.descendants(10, Path(root))), [11, 12])

    def test_alive_treats_zombies_and_missing_processes_as_gone(self):
        with tempfile.TemporaryDirectory() as root:
            fake_proc(root, 20, ["a"], state="S")
            fake_proc(root, 21, ["b"], state="Z")
            self.assertTrue(steam.alive(20, Path(root)))
            self.assertFalse(steam.alive(21, Path(root)))
            self.assertFalse(steam.alive(22, Path(root)))


class GameNameTests(unittest.TestCase):
    def test_installed_game_and_unknown_game(self):
        with tempfile.TemporaryDirectory() as root:
            apps = Path(root) / "steamapps"
            apps.mkdir()
            (apps / "appmanifest_730.acf").write_text('"AppState"\n{\n\t"appid"\t\t"730"\n\t"name"\t\t"Counter-Strike 2"\n}\n')
            self.assertEqual(steam.game_name(730, [Path(root)]), "Counter-Strike 2")
            self.assertEqual(steam.game_name(731, [Path(root)]), "Game 731")

    def test_game_in_another_library_folder(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            (Path(root) / "steamapps").mkdir()
            (Path(root) / "steamapps/libraryfolders.vdf").write_text(
                f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{other}"\n\t}}\n}}\n')
            (Path(other) / "steamapps").mkdir()
            (Path(other) / "steamapps/appmanifest_440.acf").write_text('"name"\t"Team Fortress 2"')
            self.assertEqual(steam.game_name(440, [Path(root)]), "Team Fortress 2")

    def test_non_steam_shortcut(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "userdata/1/config"
            config.mkdir(parents=True)
            entry = (b"\x00" + b"0\x00" + b"\x02appid\x00" + (3000000000).to_bytes(4, "little")
                     + b"\x01AppName\x00Battle.net\x00\x08\x08")
            (config / "shortcuts.vdf").write_bytes(b"\x00shortcuts\x00" + entry)
            self.assertEqual(steam.game_name(3000000000, [Path(root)]), "Battle.net")
            self.assertEqual(steam.game_name(3000000001, [Path(root)]), "Game 3000000001")

    def test_missing_roots_give_the_fallback(self):
        self.assertEqual(steam.game_name(5, [Path("/nonexistent-steam-root")]), "Game 5")


class TriggerTests(unittest.TestCase):
    def test_scan_builds_launches(self):
        with tempfile.TemporaryDirectory() as proc, tempfile.TemporaryDirectory() as root:
            apps = Path(root) / "steamapps"
            apps.mkdir()
            (apps / "appmanifest_730.acf").write_text('"name"\t"Counter-Strike 2"')
            fake_proc(proc, 100, ["reaper", "SteamLaunch", "AppId=730", "--", "g"], start="42")
            fake_proc(proc, 101, ["python3"])
            trigger = SteamTrigger(proc=Path(proc), roots=[Path(root)])
            launches = trigger.scan()
        self.assertEqual(len(launches), 1)
        launch = launches[0]
        self.assertEqual(launch.trigger, "steam")
        self.assertEqual(launch.key, (100, "42"))
        self.assertEqual(launch.game_id, "steam:730")
        self.assertEqual(launch.title, "Counter-Strike 2")
        self.assertEqual(launch.payload, Game(100, "42", 730, False))
        hash(launch.key)

    def test_shortcut_game_id_uses_the_app_id(self):
        with tempfile.TemporaryDirectory() as proc:
            fake_proc(proc, 100, ["reaper", "SteamLaunch", "AppId=3000000000", "--"])
            launch = SteamTrigger(proc=Path(proc), roots=[]).scan()[0]
        self.assertEqual(launch.game_id, "steam:3000000000")
        self.assertEqual(launch.title, "Game 3000000000")

    def test_scan_of_an_unreadable_proc_is_empty(self):
        self.assertEqual(SteamTrigger(proc=Path("/nonexistent-proc"), roots=[]).scan(), [])

    def test_relaunch_native_flatpak_and_shortcut(self):
        calls = []
        trigger = SteamTrigger(proc=Path("/nonexistent-proc"), roots=[],
                               popen=lambda command, **options: calls.append((command, options)))
        trigger.relaunch(Launch("steam", (1, "1"), "steam:730", "g", Game(1, "1", 730, False)))
        trigger.relaunch(Launch("steam", (2, "2"), "steam:730", "g", Game(2, "2", 730, True)))
        trigger.relaunch(Launch("steam", (3, "3"), "steam:3000000000", "g", Game(3, "3", 3000000000, False)))
        self.assertEqual(calls[0][0], ["steam", "steam://rungameid/730"])
        self.assertEqual(calls[1][0], ["flatpak", "run", "com.valvesoftware.Steam", "steam://rungameid/730"])
        self.assertEqual(calls[2][0], ["steam", f"steam://rungameid/{(3000000000 << 32) | 0x02000000}"])
        self.assertTrue(all(options["start_new_session"] for _, options in calls))

    def test_relaunch_with_a_missing_command_does_not_raise(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError("steam")

        trigger = SteamTrigger(proc=Path("/nonexistent-proc"), roots=[], popen=missing)
        trigger.relaunch(Launch("steam", (1, "1"), "steam:730", "g", Game(1, "1", 730, False)))

    def test_end_signals_the_whole_tree_with_injected_functions(self):
        sent = []
        with tempfile.TemporaryDirectory() as proc:
            fake_proc(proc, 10, ["reaper"], ppid=1)
            fake_proc(proc, 11, ["b"], ppid=10)
            fake_proc(proc, 12, ["c"], ppid=11, state="Z")
            trigger = SteamTrigger(proc=Path(proc), roots=[], kill=lambda pid, sig: sent.append((pid, sig)),
                                   sleep=lambda seconds: None)
            trigger.end(Launch("steam", (10, "x"), "steam:1", "g", game(10)))
        self.assertEqual(sorted(p for p, s in sent if s == signal.SIGTERM), [10, 11, 12])
        self.assertEqual(sorted(p for p, s in sent if s == signal.SIGKILL), [10, 11])


class BuildTests(unittest.TestCase):
    def test_build_known_and_unknown(self):
        built = triggers.build(["steam", "nonsense"])
        self.assertEqual([t.name for t in built], ["steam"])
        self.assertIsInstance(built[0], SteamTrigger)
        self.assertEqual(triggers.build([]), [])


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class RealProcessTreeTests(unittest.TestCase):
    """Run end_launch on throwaway bash/sleep trees that these tests start and clean up by pid."""

    def start_tree(self, script):
        process = subprocess.Popen(["bash", "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.started = [process.pid]
        self.addCleanup(self.cleanup, process)
        self.assertTrue(wait_for(lambda: len(steam.descendants(process.pid)) >= 2), "tree did not start")
        self.started += steam.descendants(process.pid)
        start_time = steam._stat_fields(Path("/proc") / str(process.pid))[19]
        return process, Game(process.pid, start_time, 1, False)

    def cleanup(self, process):
        for pid in getattr(self, "started", []):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        process.wait()

    def test_polite_tree_ends_on_sigterm(self):
        process, launched = self.start_tree("sleep 60 & sleep 60 & wait")
        children = self.started[1:]
        began = time.monotonic()
        steam.end_launch(launched)
        elapsed = time.monotonic() - began
        process.wait(timeout=5)
        self.assertLess(elapsed, 1.5, "a polite tree must not wait out the grace period")
        self.assertTrue(wait_for(lambda: not any(steam.alive(p) for p in children)))

    def test_child_ignoring_sigterm_is_force_ended(self):
        process, launched = self.start_tree("sleep 60 & (trap '' TERM; exec sleep 60) & wait")
        children = self.started[1:]
        began = time.monotonic()
        steam.end_launch(launched)
        elapsed = time.monotonic() - began
        process.wait(timeout=5)
        self.assertGreaterEqual(elapsed, steam.KILL_WAIT_SECONDS - 0.2)
        self.assertLess(elapsed, steam.KILL_WAIT_SECONDS + 1.5)
        self.assertTrue(wait_for(lambda: not any(steam.alive(p) for p in children)), "the straggler survived")

    def test_trigger_end_on_a_real_tree(self):
        process, launched = self.start_tree("sleep 60 & sleep 60 & wait")
        children = self.started[1:]
        SteamTrigger(roots=[]).end(Launch("steam", launched.key, "steam:1", "g", launched))
        process.wait(timeout=5)
        self.assertTrue(wait_for(lambda: not any(steam.alive(p) for p in children)))


if __name__ == "__main__":
    unittest.main()
