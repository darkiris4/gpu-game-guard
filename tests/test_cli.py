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
        with mock.patch.object(cli.runtimes, "build", return_value=[]), \
                mock.patch.object(cli.triggers, "build", return_value=[]), \
                mock.patch.object(cli.dialogs, "choose", return_value=prompter), \
                mock.patch.object(cli.gpu, "totals", return_value=None):
            _, out, _ = run("--config", "/nonexistent/config.json", "check")
        self.assertIn("without asking", out)
        self.assertIn("GPU memory overall: unknown", out)


class RunCommandTests(unittest.TestCase):
    def test_refuses_to_start_on_an_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"on_launch": "maybe"}')
            with mock.patch.object(cli, "log") as log:
                code, _, _ = run("--config", str(path), "run")
        self.assertEqual(code, 1)
        message = log.call_args[0][0]
        self.assertEqual(message.count(str(path)), 1, "the path is shown once")
        self.assertIn("on_launch", message)

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
