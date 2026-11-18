from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from gpu_game_guard import cli, config, dialogs, status
from gpu_game_guard.model import Target
from tests.fakes import FakeRuntime, FakeTrigger, make_launch


def run(*argv, state_dir=None):
    """Run the CLI with the state directory pointed at a temporary one, so nothing touches the real home."""
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as fresh:
        env = {"XDG_STATE_HOME": str(state_dir if state_dir is not None else fresh)}
        with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ConfigCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"

    def test_path_prints_the_config_location(self):
        code, out, _ = run("--config", str(self.path), "config", "path")
        self.assertEqual((code, out.strip()), (0, str(self.path)))

    def test_init_writes_defaults_and_refuses_to_overwrite(self):
        self.assertEqual(run("--config", str(self.path), "config", "init")[0], 0)
        self.assertEqual(json.loads(self.path.read_text())["on_launch"], "ask")
        self.path.write_text('{"on_launch": "stop"}')
        code, _, err = run("--config", str(self.path), "config", "init")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(json.loads(self.path.read_text()), {"on_launch": "stop"})

    def test_init_in_an_unwritable_place_fails_cleanly(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        path = blocker / "sub" / "config.json"
        code, out, err = run("--config", str(path), "config", "init")
        self.assertEqual(code, 1)
        self.assertIn("cannot write", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")

    def test_config_is_accepted_after_the_subcommand(self):
        self.path.write_text('{"on_launch": "stop"}')
        code, out, _ = run("config", "show", "--config", str(self.path))
        self.assertEqual((code, json.loads(out)["on_launch"]), (0, "stop"))
        code, out, _ = run("config", "path", "--config", str(self.path))
        self.assertEqual((code, out.strip()), (0, str(self.path)))
        code, out, _ = run("--config", str(self.path), "config", "path")
        self.assertEqual(out.strip(), str(self.path))

    def test_config_defaults_when_not_given(self):
        with mock.patch.object(cli.configmod, "config_path", return_value=self.path):
            code, out, _ = run("config", "path")
        self.assertEqual((code, out.strip()), (0, str(self.path)))

    def test_an_unexpected_os_error_is_a_one_line_failure(self):
        with mock.patch.object(cli, "command_config", side_effect=PermissionError(13, "Permission denied")):
            code, _, err = run("--config", str(self.path), "config", "show")
        self.assertEqual(code, 1)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_show_prints_the_effective_config(self):
        self.path.write_text('{"on_launch": "stop", "games": {"steam:730": {"name": "CS", "action": "ignore"}}}')
        code, out, _ = run("--config", str(self.path), "config", "show")
        shown = json.loads(out)
        self.assertEqual((code, shown["on_launch"], shown["after_game"]), (0, "stop", "ask"))
        self.assertEqual(shown["games"]["steam:730"]["action"], "ignore")

    def test_validate_reports_problems_with_a_failing_status(self):
        self.path.write_text('{"on_launch": "sometimes", "poll_seconds": -1}')
        code, out, err = run("--config", str(self.path), "config", "validate")
        self.assertEqual(code, 1)
        self.assertIn("on_launch", err)
        self.assertIn("problem", out)

    def test_validate_accepts_a_good_or_missing_file(self):
        self.assertEqual(run("--config", str(self.path), "config", "validate")[0], 0)
        self.path.write_text("{}")
        code, out, _ = run("--config", str(self.path), "config", "validate")
        self.assertEqual((code, out.strip()), (0, "config is valid"))

    def test_validate_rejects_non_json(self):
        self.path.write_text("not json")
        self.assertEqual(run("--config", str(self.path), "config", "validate")[0], 1)

    def test_show_fails_on_an_invalid_file(self):
        self.path.write_text('{"after_game": "never"}')
        code, _, err = run("--config", str(self.path), "config", "show")
        self.assertEqual(code, 1)
        self.assertIn("after_game", err)


class CheckCommandTests(unittest.TestCase):
    def check(self, config_text=None, running=("ollama",), launches=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        if config_text is not None:
            path.write_text(config_text)
        runtime = FakeRuntime("docker", running)
        trigger = FakeTrigger()
        trigger.launches = list(launches)
        prompter = mock.Mock()
        prompter.name = "kdialog"
        prompter.can_ask = True
        with mock.patch.object(cli.runtimes, "build", return_value=[runtime]), \
                mock.patch.object(cli.triggers, "build", return_value=[trigger]), \
                mock.patch.object(cli.dialogs, "choose", return_value=prompter), \
                mock.patch.object(cli.gpu, "totals", return_value=(1100, 16300)):
            return run("--config", str(path), "check")

    def test_reports_dialogs_runtimes_containers_and_memory(self):
        code, out, _ = self.check(launches=[make_launch(1, 730, title="Counter-Strike 2")])
        self.assertEqual(code, 0)
        for expected in ("dialogs:   kdialog", "docker (available)", "steam:730  Counter-Strike 2",
                         "ollama  [docker]  image:1", "1100 of 16300 MiB"):
            self.assertIn(expected, out)

    def test_distinguishes_missing_from_unresponsive_runtimes(self):
        up, down, missing = FakeRuntime("docker"), FakeRuntime("nerdctl"), FakeRuntime("podman")
        down.is_available = False
        missing.installed = lambda: False
        missing.available = lambda: self.fail("a runtime that is not installed must not be probed")
        prompter = mock.Mock()
        prompter.name = "none"
        with mock.patch.object(cli.runtimes, "build", return_value=[up, down, missing]), \
                mock.patch.object(cli.triggers, "build", return_value=[]), \
                mock.patch.object(cli.dialogs, "choose", return_value=prompter), \
                mock.patch.object(cli.gpu, "totals", return_value=None):
            _, out, _ = run("--config", "/nonexistent/config.json", "check")
        self.assertIn("docker (available), nerdctl (installed, but not answering), podman (not installed)", out)

    def test_marks_containers_the_config_ignores(self):
        _, out, _ = self.check('{"ignore_containers": ["olla*"]}')
        self.assertIn("(ignored by config)", out)

    def test_an_invalid_config_fails_but_still_reports(self):
        code, out, _ = self.check('{"dialog": "carrier-pigeon"}')
        self.assertEqual(code, 1)
        self.assertIn("INVALID", out)
        self.assertEqual(out.count("config.json"), 1, "the path is shown once")
        self.assertIn("ollama", out)

    def test_says_when_games_would_start_without_asking(self):
        prompter = mock.Mock()
        prompter.name = "none"
        prompter.can_ask = False
        with mock.patch.object(cli.runtimes, "build", return_value=[]), \
                mock.patch.object(cli.triggers, "build", return_value=[]), \
                mock.patch.object(cli.dialogs, "choose", return_value=prompter), \
                mock.patch.object(cli.gpu, "totals", return_value=None):
            _, out, _ = run("--config", "/nonexistent/config.json", "check")
        self.assertIn("cannot ask: games start without being stopped or restarted", out)
        self.assertNotIn("without asking", out)
        self.assertIn("GPU memory overall: unknown", out)

    def test_a_backend_that_can_ask_has_no_warning(self):
        _, out, _ = self.check()
        self.assertNotIn("cannot ask", out)

    def test_no_trailing_spaces_before_entries(self):
        _, out, _ = self.check(launches=[make_launch(1, 730)])
        for line in out.splitlines():
            self.assertEqual(line, line.rstrip(), repr(line))
        self.assertIn("launches running now:\n", out)
        self.assertIn("GPU containers running now:\n", out)
        _, out, _ = self.check(running=())
        self.assertIn("launches running now: none\n", out)
        self.assertIn("GPU containers running now: none\n", out)

    def test_notes_that_startup_only_settings_need_a_restart(self):
        _, out, _ = self.check('{"runtimes": ["podman"], "dialog": "zenity"}')
        self.assertIn("runtimes, dialog differ from the defaults", out)
        self.assertIn("restart", out)
        _, out, _ = self.check("{}")
        self.assertNotIn("restart", out)


class RunCommandTests(unittest.TestCase):
    def test_refuses_to_start_on_an_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"on_launch": "maybe"}')
            with mock.patch.object(cli, "log") as log:
                code, _, _ = run("--config", str(path), "run")
        self.assertEqual(code, 78)
        message = log.call_args[0][0]
        self.assertTrue(message.startswith("cannot start: "))
        self.assertEqual(message.count(str(path)), 1, "the path is shown once")
        self.assertIn("on_launch", message)

    def test_run_accepts_config_after_the_subcommand(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("[]")
            with mock.patch.object(cli, "log"):
                code, _, _ = run("run", "--config", str(path))
        self.assertEqual(code, 78)

    def test_interrupt_exits_130(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cli.runtimes, "build", return_value=[]), \
                mock.patch.object(cli.triggers, "build", return_value=[]), \
                mock.patch.object(cli.dialogs, "choose", return_value=mock.Mock(name="x")), \
                mock.patch.object(cli, "State"), \
                mock.patch.object(cli, "Guard") as guard:
            guard.return_value.run.side_effect = KeyboardInterrupt
            code, _, _ = run("--config", str(Path(tmp) / "config.json"))
        self.assertEqual(code, 130)

    def test_skips_runtimes_that_are_not_installed_and_starts_the_guard(self):
        present, absent = FakeRuntime("docker"), FakeRuntime("podman")
        absent.installed = lambda: False
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cli.runtimes, "build", return_value=[present, absent]), \
                mock.patch.object(cli.triggers, "build", return_value=[FakeTrigger()]), \
                mock.patch.object(cli.dialogs, "choose", return_value=mock.Mock(name="x")), \
                mock.patch.object(cli, "State"), \
                mock.patch.object(cli, "Guard") as guard:
            code, _, _ = run("--config", str(Path(tmp) / "config.json"))
        self.assertEqual(code, 0)
        self.assertEqual(guard.call_args[0][2], [present])
        guard.return_value.run.assert_called_once_with()


class LiveConfigTests(unittest.TestCase):
    def test_the_heavy_memory_threshold_follows_the_config(self):
        original = dialogs.HEAVY_VRAM_MIB
        self.addCleanup(setattr, dialogs, "HEAVY_VRAM_MIB", original)
        cfg = config.Config(heavy_vram_mib=2048)
        live = cli.LiveConfig(mock.Mock(current=lambda: cfg))
        self.assertIs(live.current(), cfg)
        self.assertEqual(dialogs.HEAVY_VRAM_MIB, 2048)
        cfg.heavy_vram_mib = 9000
        live.current()
        self.assertEqual(dialogs.HEAVY_VRAM_MIB, 9000)


class UsageTests(unittest.TestCase):
    def test_version_and_bad_usage(self):
        self.assertEqual(run("--version")[0], 0)
        self.assertEqual(run("config", "frobnicate")[0], 2)
        self.assertEqual(run("--no-such-flag")[0], 2)


class Sandbox(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "config.json"
        self.state = self.dir / "state"
        self.state.mkdir()

    def cli(self, *argv):
        return run("--config", str(self.path), *argv, state_dir=self.state)

    def write(self, data):
        self.path.write_text(json.dumps(data))

    def stored(self):
        return json.loads(self.path.read_text())


class PauseResumeTests(Sandbox):
    def paused(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}):
            return status.paused()

    def test_pause_until_resumed_and_resume(self):
        code, out, _ = self.cli("pause")
        self.assertEqual((code, self.paused()), (0, (True, None)))
        self.assertIn("until resumed", out)
        self.assertEqual(self.cli("resume")[0], 0)
        self.assertEqual(self.paused(), (False, None))

    def test_pause_for_a_duration(self):
        for text, seconds in (("90s", 90), ("30m", 1800), ("2h", 7200), ("1d", 86400)):
            self.assertEqual(self.cli("pause", "--for", text)[0], 0)
            flag, until = self.paused()
            self.assertTrue(flag, text)
            self.assertAlmostEqual(until - time.time(), seconds, delta=5)

    def test_a_bad_duration_is_a_usage_error(self):
        for text in ("soon", "10", "0m", "-5m", "5w"):
            self.assertEqual(self.cli("pause", "--for", text)[0], 2, text)
        self.assertEqual(self.paused(), (False, None))

    def test_parse_duration(self):
        self.assertEqual(cli.parse_duration("1.5h"), 5400)


class StatusCommandTests(Sandbox):
    def write_status(self, **changes):
        fields = dict(version="2.0", pid=7, updated=time.time(), state="watching", dialog="zenity",
                      targets=2)
        fields.update(changes)
        status.write_status(status.Status(**fields), self.state / "gpu-game-guard")

    def test_not_running(self):
        code, out, _ = self.cli("status")
        self.assertEqual(code, 1)
        self.assertIn("not running", out)
        code, out, _ = self.cli("status", "--json")
        self.assertEqual((code, json.loads(out)["running"]), (1, False))

    def test_running_text(self):
        self.write_status(game={"id": "steam:730", "title": "CS"}, stopped=[{"kind": "container", "runtime": "docker",
                                                                           "name": "ollama"}])
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        for part in ("is running (pid 7)", "state:    watching", "CS (steam:730)", "ollama", "zenity", "2 configured", "ok"):
            self.assertIn(part, out)

    def test_running_json_and_infinite_pause(self):
        self.write_status(paused=True, paused_until=float("inf"), config_ok=False, config_error="bad")
        code, out, _ = self.cli("status", "--json")
        data = json.loads(out)
        self.assertEqual((code, data["running"], data["paused"], data["paused_until"]), (0, True, True, None))
        _, text, _ = self.cli("status")
        self.assertIn("until resumed", text)
        self.assertIn("INVALID: bad", text)

    def test_a_stale_status_means_not_running(self):
        self.write_status(updated=1.0)
        self.assertEqual(self.cli("status")[0], 1)

    def test_not_running_but_paused_says_so(self):
        self.cli("pause")
        _, out, _ = self.cli("status")
        self.assertIn("a pause is set: until resumed", out)


TARGETS = [
    {"id": "svc", "kind": "service", "unit": "svc.service"},
    {"id": "cmd", "kind": "command", "stop": ["a"], "start": ["b"]},
    {"id": "obs", "kind": "process", "match": "obs"},
]


class TargetsCommandTests(Sandbox):
    def test_list(self):
        self.write({"targets": TARGETS})
        code, out, _ = self.cli("targets", "list")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 3)
        self.assertIn("svc.service (system)", out)
        self.assertIn("match obs", out)

    def test_list_when_empty_and_when_the_file_is_missing(self):
        self.assertIn("no targets", self.cli("targets", "list")[1])

    def test_disable_enable_remove(self):
        self.write({"targets": TARGETS})
        self.assertEqual(self.cli("targets", "disable", "obs")[0], 0)
        self.assertFalse(self.stored()["targets"][2]["enabled"])
        self.assertIn("disabled", self.cli("targets", "list")[1])
        self.assertEqual(self.cli("targets", "enable", "obs")[0], 0)
        self.assertTrue(self.stored()["targets"][2]["enabled"])
        self.assertEqual(self.cli("targets", "remove", "svc")[0], 0)
        self.assertEqual([t["id"] for t in self.stored()["targets"]], ["cmd", "obs"])

    def test_unknown_id_and_missing_id(self):
        self.write({"targets": TARGETS})
        code, _, err = self.cli("targets", "remove", "nope")
        self.assertEqual(code, 1)
        self.assertIn("nope", err)
        self.assertEqual(self.cli("targets", "enable")[0], 2)
        self.assertEqual(len(self.stored()["targets"]), 3)

    def test_an_invalid_config_is_not_touched(self):
        self.path.write_text('{"targets": [{"id": "x"}]}')
        before = self.path.read_text()
        self.assertEqual(self.cli("targets", "disable", "x")[0], 1)
        self.assertEqual(self.path.read_text(), before)


def fake_preset(preset_id="p1", spec=None):
    spec = spec or {"id": preset_id, "kind": "service", "label": "P", "unit": "p.service", "scope": "system"}
    return types.SimpleNamespace(id=preset_id, label="Preset " + preset_id, description="does things", spec=spec, detect={})


class PresetsCommandTests(Sandbox):
    def patched(self, presets, applicable=True):
        module = types.ModuleType("gpu_game_guard.presets")
        module.load_presets = lambda: presets
        module.applicable = lambda preset: applicable
        return mock.patch.dict(sys.modules, {"gpu_game_guard.presets": module}), mock.patch.dict(
            "gpu_game_guard.__dict__", {"presets": module})

    def test_list_marks_added_and_applicability(self):
        self.write({"targets": [dict(TARGETS[0], id="p1", unit="p.service")]})
        a, b = self.patched([fake_preset("p1"), fake_preset("p2")], applicable=False)
        with a, b:
            code, out, _ = self.cli("presets", "list")
        self.assertEqual(code, 0)
        self.assertIn("p1  Preset p1  (added, not found here)", out)
        self.assertIn("p2  Preset p2  (not found here)", out)
        self.assertIn("does things", out)

    def test_add_writes_a_valid_target_and_hints_at_polkit(self):
        a, b = self.patched([fake_preset("p1")])
        with a, b:
            code, out, _ = self.cli("presets", "add", "p1")
        self.assertEqual(code, 0)
        self.assertEqual(self.stored()["targets"][0]["unit"], "p.service")
        self.assertIn("polkit allow p.service", out)
        self.assertEqual(config.load(self.path).target("p1").kind, "service")

    def test_add_twice_unknown_and_missing_id(self):
        a, b = self.patched([fake_preset("p1")])
        with a, b:
            self.assertEqual(self.cli("presets", "add", "p1")[0], 0)
            self.assertEqual(self.cli("presets", "add", "p1")[0], 1)
            self.assertEqual(self.cli("presets", "add", "zz")[0], 1)
            self.assertEqual(self.cli("presets", "add")[0], 2)
        self.assertEqual(len(self.stored()["targets"]), 1)

    def test_add_warns_when_not_applicable(self):
        a, b = self.patched([fake_preset("p1")], applicable=False)
        with a, b:
            code, _, err = self.cli("presets", "add", "p1")
        self.assertEqual(code, 0)
        self.assertIn("not found on this system", err)

    def test_an_invalid_preset_is_refused(self):
        a, b = self.patched([fake_preset("p1", {"id": "p1", "kind": "service", "unit": "nope"})])
        with a, b:
            code, _, err = self.cli("presets", "add", "p1")
        self.assertEqual(code, 1)
        self.assertIn("invalid", err)
        self.assertFalse(self.path.exists())

    def test_the_real_presets_are_valid(self):
        from gpu_game_guard import presets
        for preset in presets.load_presets():
            self.assertEqual(config.validate({"targets": [preset.spec]}), [], preset.id)


class DiscoverCommandTests(Sandbox):
    def test_prints_candidates(self):
        from gpu_game_guard import discovery
        item = discovery.Candidate("service:ollama", "service", "ollama.service", "Ollama", "unit ollama.service",
                                   2048, False, "low", {"id": "ollama", "kind": "service"}, "a note")
        managed = discovery.Candidate("process:obs", "process", "obs", "OBS", "", None, True, "high", None, "")
        with mock.patch.object(discovery, "discover", return_value=[item, managed]) as discover, \
                mock.patch.object(cli.runtimes, "build", return_value=[FakeRuntime("docker")]):
            code, out, _ = self.cli("discover")
        self.assertEqual(code, 0)
        self.assertEqual(discover.call_args[0][1][0].name, "docker")
        self.assertIn("service:ollama  [service, risk low]  Ollama  2048 MiB", out)
        self.assertIn("a note", out)
        self.assertIn('target: {"id": "ollama", "kind": "service"}', out)
        self.assertIn("(already managed)", out)

    def test_nothing_found(self):
        from gpu_game_guard import discovery
        with mock.patch.object(discovery, "discover", return_value=[]), \
                mock.patch.object(cli.runtimes, "build", return_value=[]):
            self.assertIn("nothing", self.cli("discover")[1])

    def test_an_invalid_config_fails(self):
        self.path.write_text("[]")
        self.assertEqual(self.cli("discover")[0], 1)


class PolkitCommandTests(Sandbox):
    def test_allow_revoke_status(self):
        from gpu_game_guard import polkit
        with mock.patch.object(polkit, "allow", return_value=Path("/x/rule")) as allow, \
                mock.patch.object(polkit, "revoke", return_value=Path("/x/rule")) as revoke, \
                mock.patch.object(polkit, "status", return_value={"a.service": True, "b.service": False}) as stat:
            code, out, _ = self.cli("polkit", "allow", "a.service", "b.service", "--user", "alice")
            self.assertEqual((code, allow.call_args[0]), (0, (["a.service", "b.service"], "alice")))
            self.assertIn("/x/rule", out)
            self.assertEqual(self.cli("polkit", "revoke", "a.service")[0], 0)
            self.assertEqual(revoke.call_args[0], (["a.service"], None))
            code, out, _ = self.cli("polkit", "status", "a.service", "b.service")
            self.assertEqual(code, 0)
            self.assertIn("a.service: allowed", out)
            self.assertIn("b.service: not allowed", out)
            self.assertEqual(stat.call_count, 1)

    def test_a_refused_change_is_a_failure(self):
        from gpu_game_guard import polkit
        with mock.patch.object(polkit, "allow", side_effect=polkit.PolkitError("needs root")):
            code, _, err = self.cli("polkit", "allow", "a.service")
        self.assertEqual(code, 1)
        self.assertIn("needs root", err)

    def test_units_are_required(self):
        self.assertEqual(self.cli("polkit", "allow")[0], 2)


class QtCommandTests(Sandbox):
    def modules(self, available=True, **entry_points):
        qt = types.ModuleType("gpu_game_guard.ui.qt")
        qt.available = lambda: available
        mods = {"gpu_game_guard.ui.qt": qt}
        for name, attrs in entry_points.items():
            module = types.ModuleType(f"gpu_game_guard.ui.{name}")
            for attr, value in attrs.items():
                setattr(module, attr, value)
            mods[f"gpu_game_guard.ui.{name}"] = module
        return [mock.patch.dict(sys.modules, mods)]

    def run_with(self, argv, available=True, **entry_points):
        patches = self.modules(available, **entry_points)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return self.cli(*argv)

    def test_settings_and_tray_call_run(self):
        calls = []
        code, _, _ = self.run_with(["settings"], settings={"run": lambda: calls.append("settings") or 0})
        self.assertEqual((code, calls), (0, ["settings"]))
        code, _, _ = self.run_with(["tray"], tray={"run": lambda: calls.append("tray") or 3})
        self.assertEqual((code, calls), (3, ["settings", "tray"]))

    def test_prompt_passes_the_streams(self):
        seen = []

        def run_prompt(argv, stdin, stdout):
            seen.append((argv, stdin is sys.stdin, stdout is sys.stdout))
            return 0

        code, _, _ = self.run_with(["prompt", "restart"], prompt={"run_prompt": run_prompt})
        self.assertEqual((code, len(seen), seen[0][0]), (0, 1, ["restart"]))

    def test_missing_pyqt_prints_the_hint(self):
        for command in (["settings"], ["tray"], ["prompt", "launch"]):
            code, out, err = self.run_with(command, available=False)
            self.assertEqual((code, out), (1, ""), command)
            self.assertIn("Install python3-pyqt5 (sudo apt install python3-pyqt5)", err)

    def test_an_import_error_prints_the_hint(self):
        with mock.patch.dict(sys.modules, {"gpu_game_guard.ui.qt": None}):
            code, _, err = self.cli("settings")
        self.assertEqual(code, 1)
        self.assertIn("python3-pyqt5", err)

    def test_prompt_needs_a_kind(self):
        self.assertEqual(self.cli("prompt")[0], 2)
        self.assertEqual(self.cli("prompt", "other")[0], 2)


class CheckExtensionTests(Sandbox):
    def check(self, running_targets=(), view=None):
        host = FakeRuntime("service")
        host.running_gpu_targets = lambda: [Target("service", "service", "Svc", extra={"id": i}) for i in running_targets]
        with mock.patch.object(cli.runtimes, "build", return_value=[host]), \
                mock.patch.object(cli.triggers, "build", return_value=[]), \
                mock.patch.object(cli.gpu, "totals", return_value=None), \
                mock.patch.object(cli.statusmod, "read_status", return_value=view):
            return self.cli("check")

    def test_lists_targets_with_their_state(self):
        self.write({"targets": TARGETS[:1] + [dict(TARGETS[1], enabled=False)]})
        code, out, _ = self.check(running_targets=["svc"])
        self.assertEqual(code, 0)
        self.assertIn("svc  [service]  svc  running", out)
        self.assertIn("cmd  [command]  cmd  disabled", out)

    def test_not_running_target_and_no_targets(self):
        self.write({"targets": TARGETS[:1]})
        self.assertIn("svc  [service]  svc  not running", self.check()[1])
        self.write({})
        self.assertIn("configured targets: none", self.check()[1])

    def test_service_view(self):
        self.assertIn("service:   not running", self.check()[1])
        view = status.Status(pid=9, state="asking", paused=True)
        out = self.check(view=view)[1]
        self.assertIn("service:   running (pid 9), state asking, paused", out)


class ConfigNewKeysTests(Sandbox):
    def test_show_and_validate_handle_new_keys(self):
        self.write({"enabled": False, "protect": ["x*"], "targets": TARGETS})
        shown = json.loads(self.cli("config", "show")[1])
        self.assertEqual((shown["enabled"], shown["protect"], len(shown["targets"])), (False, ["x*"], 3))
        self.assertEqual(self.cli("config", "validate")[0], 0)
        self.write({"targets": [{"id": "a", "kind": "process", "match": "x", "restrat": 1}]})
        code, _, err = self.cli("config", "validate")
        self.assertEqual(code, 1)
        self.assertIn("did you mean 'restart'", err)


class RunWiringTests(Sandbox):
    def test_run_builds_host_runtimes_and_passes_status_arguments(self):
        prompter = mock.Mock()
        prompter.name = "zenity"
        with mock.patch.object(cli.runtimes, "build", return_value=[]) as build, \
                mock.patch.object(cli.triggers, "build", return_value=[]), \
                mock.patch.object(cli.dialogs, "choose", return_value=prompter), \
                mock.patch.object(cli, "State"), mock.patch.object(cli, "Guard") as guard:
            code, _, _ = self.cli("run")
        self.assertEqual(code, 0)
        self.assertIn("get_config", build.call_args[1])
        self.assertTrue(callable(build.call_args[1]["get_config"]))
        self.assertEqual((guard.call_args[1]["status_dir"], guard.call_args[1]["dialog_name"]), (None, "zenity"))

    def test_live_config_exposes_the_watchers_path_and_error(self):
        watcher = config.ConfigWatcher(self.path)
        live = cli.LiveConfig(watcher)
        self.assertEqual((live.path, live.error), (self.path, ""))
        self.path.write_text('{"poll_seconds": -1}')
        live.current()
        self.assertIn("poll_seconds", live.error)


if __name__ == "__main__":
    unittest.main()
