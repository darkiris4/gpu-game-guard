import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gpu_game_guard as guard
from gpu_game_guard import Game, GpuContainer, Guard, State


class FakeDocker:
    def __init__(self, running):
        self.running = list(running)
        self.stopped, self.started = [], []

    def running_gpu_containers(self):
        return [GpuContainer(name, "image:1", name) for name in self.running]

    def stop(self, names):
        self.stopped.extend(names)
        self.running = [n for n in self.running if n not in names]

    def start(self, names):
        self.started.extend(names)


class FakeDialogs:
    def __init__(self, launch="stop", restart=True):
        self.launch_answer, self.restart_answer = launch, restart
        self.launch_asked, self.restart_asked = [], []

    def launch(self, game_name, containers):
        self.launch_asked.append([c.name for c in containers])
        return self.launch_answer

    def restart(self, names):
        self.restart_asked.append(list(names))
        return self.restart_answer


class FakeSteam:
    def __init__(self):
        self.launched = []

    def launch(self, game):
        self.launched.append(game)


class Env:
    def __init__(self, running=("ollama",), launch="stop", restart=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.games = []
        self.ended = []
        self.now = 1000.0
        self.docker = FakeDocker(running)
        self.dialogs = FakeDialogs(launch, restart)
        self.steam = FakeSteam()
        self.state = State(Path(self.tmp.name) / "state.json")
        self.guard = Guard(self.docker, self.dialogs, self.steam, self.state, scan=lambda: list(self.games),
                           end=self.ended.append, clock=lambda: self.now, wall=lambda: self.now)

    def advance(self, seconds):
        self.now += seconds
        self.guard.step()


def game(pid, app_id=730, flatpak=False):
    return Game(pid, str(pid * 10), app_id, flatpak)


class ParseTests(unittest.TestCase):
    def test_native_launcher(self):
        argv = ["/home/u/.local/share/Steam/ubuntu12_32/reaper", "SteamLaunch", "AppId=730", "--", "x"]
        self.assertEqual(guard.parse_launch(argv), (730, False))

    def test_flatpak_launcher(self):
        argv = ["/home/u/.var/app/com.valvesoftware.Steam/.local/share/Steam/ubuntu12_32/reaper", "SteamLaunch",
                "AppId=730", "--", "x"]
        self.assertEqual(guard.parse_launch(argv), (730, True))

    def test_other_processes_are_not_launches(self):
        self.assertIsNone(guard.parse_launch(["steam", "steam://rungameid/730"]))
        self.assertIsNone(guard.parse_launch(["/usr/bin/reaper", "Other", "AppId=1"]))
        self.assertIsNone(guard.parse_launch(["reaper", "SteamLaunch", "AppId=abc"]))
        self.assertIsNone(guard.parse_launch([]))

    def test_game_ids(self):
        self.assertEqual(game(1, 730).game_id, 730)
        self.assertEqual(game(1, 3000000000).game_id, (3000000000 << 32) | 0x02000000)


class ProcTests(unittest.TestCase):
    def fake_proc(self, root, pid, argv, ppid=1, start="123456"):
        entry = Path(root) / str(pid)
        entry.mkdir()
        (entry / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
        fields = ["S", str(ppid)] + ["0"] * 17 + [start]
        (entry / "stat").write_text(f"{pid} (reaper) " + " ".join(fields) + "\n")

    def test_scan_finds_launchers_and_ignores_everything_else(self):
        with tempfile.TemporaryDirectory() as root:
            self.fake_proc(root, 100, ["/s/ubuntu12_32/reaper", "SteamLaunch", "AppId=730", "--", "game"], start="42")
            self.fake_proc(root, 101, ["/usr/bin/python3", "script.py"])
            self.fake_proc(root, 102, ["reaper", "SteamLaunch", "AppId=3000000000", "--"])
            (Path(root) / "self").mkdir()
            found = guard.scan_games(Path(root))
        self.assertEqual(sorted((g.pid, g.app_id, g.start_time) for g in found), [(100, 730, "42"), (102, 3000000000, "123456")])

    def test_descendants_walks_the_whole_tree(self):
        with tempfile.TemporaryDirectory() as root:
            self.fake_proc(root, 10, ["a"], ppid=1)
            self.fake_proc(root, 11, ["b"], ppid=10)
            self.fake_proc(root, 12, ["c"], ppid=11)
            self.fake_proc(root, 13, ["d"], ppid=999)
            self.assertEqual(sorted(guard.descendants(10, Path(root))), [11, 12])


class ContainerMemoryTests(unittest.TestCase):
    CONTAINER = "4f96b9e7ddac377fc068d30686c8f0e1d2264f2fed8afdc72b49a7ebdee5f18d"

    def test_cgroup_paths(self):
        self.assertEqual(guard.container_id_from_cgroup(f"0::/system.slice/docker-{self.CONTAINER}.scope\n"), self.CONTAINER)
        self.assertEqual(guard.container_id_from_cgroup(f"12:devices:/docker/{self.CONTAINER}\n"), self.CONTAINER)
        self.assertIsNone(guard.container_id_from_cgroup("0::/user.slice/user-1000.slice/session-2.scope\n"))

    def test_gpu_memory_is_summed_per_container(self):
        with tempfile.TemporaryDirectory() as root:
            for pid, group in (("10", f"0::/system.slice/docker-{self.CONTAINER}.scope"), ("11", f"0::/system.slice/docker-{self.CONTAINER}.scope"),
                               ("12", "0::/user.slice/user-1000.slice/app.scope")):
                (Path(root) / pid).mkdir()
                (Path(root) / pid / "cgroup").write_text(group + "\n")
            gpu = guard.Gpu()
            with mock.patch.object(gpu, "_query", return_value=[["10", "512"], ["11", "256"], ["12", "900"], ["99", "5"]]):
                self.assertEqual(gpu.vram_by_container(Path(root)), {self.CONTAINER: 768})

    def test_nvidia_smi_missing_gives_empty_results(self):
        gpu = guard.Gpu()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(gpu.vram_by_container(), {})
            self.assertIsNone(gpu.totals())

    def test_launch_message_names_the_guard_and_lists_containers(self):
        message = guard.launch_message("Half-Life <2>", [GpuContainer("ollama", "ollama/ollama:0.4", "a", 1536),
                                                       GpuContainer("whisper", "w&w", "b", None)], (5120, 16311))
        self.assertIn("GPU Game Guard", message)
        self.assertIn("Half-Life &lt;2&gt;", message)
        self.assertIn("1.5 GB of GPU memory", message)
        self.assertIn("no GPU memory in use right now", message)
        self.assertIn("w&amp;w", message)
        self.assertIn("by all programs: 5.0 GB of 15.9 GB", message)
        self.assertNotIn("by all programs", guard.launch_message("g", [], None))

    def test_impact_wording_follows_how_much_gpu_memory_the_containers_hold(self):
        idle = guard.impact_sentence([GpuContainer("a", "i", "a", None), GpuContainer("b", "i", "b", 0)])
        some = guard.impact_sentence([GpuContainer("a", "i", "a", 1100)])
        heavy = guard.impact_sentence([GpuContainer("a", "i", "a", 3000), GpuContainer("b", "i", "b", 1500)])
        self.assertIn("idle", idle)
        self.assertNotIn("bottleneck", idle + some + heavy)
        self.assertIn("modest", some)
        self.assertIn("4.4 GB", heavy)
        self.assertIn("run out of memory", heavy)

    def test_restart_message_lists_names(self):
        self.assertIn("<li>a&amp;b</li>", guard.restart_message(["a&b"]))


class GameNameTests(unittest.TestCase):
    def test_installed_game_and_unknown_game(self):
        with tempfile.TemporaryDirectory() as root:
            apps = Path(root) / "steamapps"
            apps.mkdir()
            (apps / "appmanifest_730.acf").write_text('"AppState"\n{\n\t"appid"\t\t"730"\n\t"name"\t\t"Counter-Strike 2"\n}\n')
            self.assertEqual(guard.game_name(730, [Path(root)]), "Counter-Strike 2")
            self.assertEqual(guard.game_name(731, [Path(root)]), "Game 731")

    def test_game_in_another_library_folder(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            (Path(root) / "steamapps").mkdir()
            (Path(root) / "steamapps/libraryfolders.vdf").write_text(f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{other}"\n\t}}\n}}\n')
            (Path(other) / "steamapps").mkdir()
            (Path(other) / "steamapps/appmanifest_440.acf").write_text('"name"\t"Team Fortress 2"')
            self.assertEqual(guard.game_name(440, [Path(root)]), "Team Fortress 2")

    def test_non_steam_shortcut(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "userdata/1/config"
            config.mkdir(parents=True)
            entry = b"\x00" + b"0\x00" + b"\x02appid\x00" + (3000000000).to_bytes(4, "little") + b"\x01AppName\x00Battle.net\x00\x08\x08"
            (config / "shortcuts.vdf").write_bytes(b"\x00shortcuts\x00" + entry)
            self.assertEqual(guard.game_name(3000000000, [Path(root)]), "Battle.net")


class MissingDialogTests(unittest.TestCase):
    def test_a_missing_kdialog_lets_the_game_start(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("kdialog")):
            choice = guard.Dialogs(gpu=mock.Mock(totals=lambda: None)).launch("Game", [GpuContainer("a", "i", "a")])
        self.assertEqual(choice, "anyway")

    def test_a_missing_kdialog_keeps_the_containers_marked_and_asks_again_later(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("kdialog")):
            self.assertIsNone(guard.Dialogs().restart(["a"]))
        env = Env(running=("a",))
        env.dialogs.restart_answer = None
        env.state.stopped, env.state.stopped_at = ["a"], env.now - 3600
        env.state.save()
        env.advance(1)
        env.advance(1)
        self.assertEqual(len(env.dialogs.restart_asked), 1, "no repeated asking within the retry delay")
        self.assertEqual(env.state.stopped, ["a"])
        env.advance(guard.DIALOG_RETRY_SECONDS + 1)
        self.assertEqual(len(env.dialogs.restart_asked), 2)
        env.tmp.cleanup()


class GpuDetectionTests(unittest.TestCase):
    def test_device_request_with_gpu_capability(self):
        info = {"HostConfig": {"DeviceRequests": [{"Driver": "nvidia", "Capabilities": [["gpu"]]}]}}
        self.assertTrue(guard.is_gpu_container(info))

    def test_nvidia_runtime(self):
        self.assertTrue(guard.is_gpu_container({"HostConfig": {"Runtime": "nvidia"}}))

    def test_plain_container(self):
        self.assertFalse(guard.is_gpu_container({"HostConfig": {"Runtime": "runc", "DeviceRequests": None}}))
        self.assertFalse(guard.is_gpu_container({"HostConfig": {"DeviceRequests": [{"Capabilities": [["video"]]}]}}))
        self.assertFalse(guard.is_gpu_container({}))


class FlowTests(unittest.TestCase):
    def env(self, **options):
        env = Env(**options)
        self.addCleanup(env.tmp.cleanup)
        return env

    def test_no_gpu_containers_lets_the_game_start(self):
        env = self.env(running=())
        env.games = [game(10)]
        env.advance(1)
        self.assertEqual(env.dialogs.launch_asked, [])
        self.assertEqual(env.ended, [])

    def test_every_app_id_is_intercepted_including_battle_net(self):
        env = self.env()
        env.games = [game(10, app_id=3000000000)]
        env.advance(1)
        self.assertEqual(env.dialogs.launch_asked, [["ollama"]])
        self.assertEqual(env.ended, [game(10, app_id=3000000000)])

    def test_launchers_already_running_at_startup_are_left_alone(self):
        env = self.env()
        env.games = [game(10)]
        restarted = Guard(env.docker, env.dialogs, env.steam, env.state, scan=lambda: list(env.games),
                          end=env.ended.append, clock=lambda: env.now, wall=lambda: env.now)
        restarted.step()
        self.assertEqual(env.dialogs.launch_asked, [])
        self.assertEqual(env.ended, [])
        env.games = [game(10), game(11, app_id=440)]
        restarted.step()
        self.assertEqual(env.dialogs.launch_asked, [["ollama"]], "a launch after startup is still intercepted")

    def test_stop_then_relaunch_then_restart_after_exit(self):
        env = self.env(launch="stop", restart=True)
        env.games = [game(10)]
        env.advance(1)
        self.assertEqual(env.ended, [game(10)])
        self.assertEqual(env.dialogs.launch_asked, [["ollama"]])
        self.assertEqual(env.docker.stopped, ["ollama"])
        self.assertEqual(env.steam.launched, [game(10)])
        self.assertEqual(env.state.stopped, ["ollama"])

        env.games = []  # the ended launcher is gone; the relaunch has not appeared yet
        env.advance(2)
        self.assertEqual(env.dialogs.restart_asked, [], "no restart question before the game has run")

        env.games = [game(11)]  # the relaunched game
        env.advance(2)
        self.assertEqual(env.dialogs.launch_asked, [["ollama"]], "the relaunch is not asked about again")
        self.assertTrue(env.state.game_seen)

        env.games = []
        env.advance(guard.EXIT_GRACE_SECONDS - 1)
        self.assertEqual(env.dialogs.restart_asked, [])
        env.advance(2)
        self.assertEqual(env.dialogs.restart_asked, [["ollama"]])
        self.assertEqual(env.docker.started, ["ollama"])
        self.assertEqual(env.state.stopped, [])
        self.assertFalse(env.state.path.exists())

    def test_launch_anyway_relaunches_without_stopping(self):
        env = self.env(launch="anyway")
        env.games = [game(10)]
        env.advance(1)
        self.assertEqual(env.docker.stopped, [])
        self.assertEqual(env.steam.launched, [game(10)])
        env.games = [game(11)]
        env.advance(1)
        env.games = []
        env.advance(guard.EXIT_GRACE_SECONDS + 1)
        self.assertEqual(env.dialogs.launch_asked, [["ollama"]])
        self.assertEqual(env.dialogs.restart_asked, [])

    def test_cancel_ends_the_launch_and_does_nothing_else(self):
        env = self.env(launch="cancel")
        env.games = [game(10)]
        env.advance(1)
        self.assertEqual(env.ended, [game(10)])
        self.assertEqual(env.docker.stopped, [])
        self.assertEqual(env.steam.launched, [])

    def test_a_lingering_ended_launcher_does_not_ask_twice(self):
        env = self.env(launch="cancel")
        env.games = [game(10)]
        env.advance(1)
        env.advance(1)
        env.advance(1)
        self.assertEqual(len(env.dialogs.launch_asked), 1)

    def test_leaving_containers_stopped_clears_the_state(self):
        env = self.env(launch="stop", restart=False)
        env.games = [game(10)]
        env.advance(1)
        env.games = [game(11)]
        env.advance(1)
        env.games = []
        env.advance(guard.EXIT_GRACE_SECONDS + 1)
        self.assertEqual(env.docker.started, [])
        self.assertEqual(env.state.stopped, [])

    def test_restart_is_asked_when_the_relaunched_game_never_appears(self):
        env = self.env(launch="stop")
        env.games = [game(10)]
        env.advance(1)
        env.games = []
        env.advance(guard.NO_GAME_TIMEOUT_SECONDS - 5)
        self.assertEqual(env.dialogs.restart_asked, [])
        env.advance(10)
        self.assertEqual(env.dialogs.restart_asked, [["ollama"]])

    def test_state_left_by_an_earlier_run_is_asked_about_at_startup(self):
        env = self.env(running=())
        env.state.stopped, env.state.stopped_at, env.state.game_seen = ["ollama"], env.now - 3600, False
        env.state.save()
        reloaded = State(env.state.path)
        self.assertEqual(reloaded.stopped, ["ollama"])
        env.guard.state = reloaded
        env.advance(1)
        self.assertEqual(env.dialogs.restart_asked, [["ollama"]])

    def test_several_stops_in_a_row_keep_every_container(self):
        env = self.env(running=("a",), launch="stop")
        env.games = [game(10)]
        env.advance(1)
        env.docker.running = ["b"]
        env.games = [game(11, app_id=440)]
        env.advance(1)
        self.assertEqual(env.state.stopped, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
