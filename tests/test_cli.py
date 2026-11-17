from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from gpu_game_guard import cli, config, dialogs
from tests.fakes import FakeRuntime, FakeTrigger, make_launch


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
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


if __name__ == "__main__":
    unittest.main()
