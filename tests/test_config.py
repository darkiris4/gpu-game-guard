from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gpu_game_guard import config as cfg
from gpu_game_guard.config import Config, ConfigError, ConfigWatcher, GameRule


class DefaultsTests(unittest.TestCase):
    def test_defaults_match_the_contract(self):
        c = Config()
        self.assertEqual((c.on_launch, c.after_game, c.dialog), ("ask", "ask", "auto"))
        self.assertEqual((c.poll_seconds, c.exit_grace_seconds, c.no_game_timeout_seconds), (1.0, 8.0, 180.0))
        self.assertEqual((c.bypass_seconds, c.dialog_retry_seconds, c.heavy_vram_mib), (120.0, 300.0, 4096))
        self.assertEqual((c.runtimes, c.triggers, c.ignore_containers, c.games), (["docker", "podman"], ["steam"], [], {}))
        self.assertEqual(cfg.validate(c.to_json()), [])

    def test_instances_do_not_share_lists(self):
        a, b = Config(), Config()
        a.runtimes.append("x")
        self.assertEqual(b.runtimes, ["docker", "podman"])

    def test_to_json_has_every_key(self):
        keys = set(Config().to_json())
        self.assertEqual(keys, {"on_launch", "after_game", "poll_seconds", "exit_grace_seconds", "no_game_timeout_seconds",
                                "bypass_seconds", "dialog_retry_seconds", "heavy_vram_mib", "ignore_containers", "runtimes",
                                "triggers", "dialog", "games"})

    def test_round_trip(self):
        c = Config(on_launch="stop", after_game="leave", poll_seconds=0.5, heavy_vram_mib=1000, ignore_containers=["a*"],
                   runtimes=["podman"], triggers=[], dialog="zenity",
                   games={"steam:730": GameRule("Counter-Strike 2", "ignore")})
        self.assertEqual(Config.from_json(json.loads(json.dumps(c.to_json()))), c)


class RuleTests(unittest.TestCase):
    def test_action_for_prefers_the_game_rule(self):
        c = Config(on_launch="stop", games={"steam:1": GameRule("One", "ignore")})
        self.assertEqual(c.action_for("steam:1"), "ignore")
        self.assertEqual(c.action_for("steam:2"), "stop")

    def test_is_ignored_uses_globs_and_is_case_sensitive(self):
        c = Config(ignore_containers=["backup-*", "db?", "exact"])
        self.assertTrue(c.is_ignored("backup-nightly"))
        self.assertTrue(c.is_ignored("db1"))
        self.assertTrue(c.is_ignored("exact"))
        self.assertFalse(c.is_ignored("db12"))
        self.assertFalse(c.is_ignored("Backup-x"))
        self.assertFalse(c.is_ignored("ollama"))
        self.assertFalse(Config().is_ignored("anything"))


class ValidateTests(unittest.TestCase):
    def problems(self, **data):
        return cfg.validate(data)

    def test_empty_and_partial_are_valid(self):
        self.assertEqual(cfg.validate({}), [])
        self.assertEqual(self.problems(poll_seconds=2, heavy_vram_mib=0, bypass_seconds=0), [])

    def test_top_level_must_be_an_object(self):
        self.assertIn("object", cfg.validate([])[0])

    def test_unknown_enum_values_name_the_key_and_choices(self):
        for key, allowed in (("on_launch", "ask, stop, ignore"), ("after_game", "ask, restart, leave"),
                             ("dialog", "auto, zenity, kdialog, notify, none")):
            (problem,) = self.problems(**{key: "bogus"})
            self.assertIn(f'"{key}"', problem)
            self.assertIn(allowed, problem)
        self.assertTrue(self.problems(on_launch=3))
        self.assertTrue(self.problems(on_launch=["ask"]))

    def test_numbers_reject_wrong_types_negatives_and_non_finite(self):
        for key in ("poll_seconds", "exit_grace_seconds", "no_game_timeout_seconds", "bypass_seconds",
                    "dialog_retry_seconds", "heavy_vram_mib"):
            for bad in ("5", None, True, -1, float("nan"), float("inf"), [1]):
                problems = self.problems(**{key: bad})
                self.assertEqual(len(problems), 1, (key, bad))
                self.assertIn(f'"{key}"', problems[0])

    def test_poll_seconds_must_be_positive(self):
        (problem,) = self.problems(poll_seconds=0)
        self.assertIn("more than zero", problem)

    def test_heavy_vram_must_be_whole(self):
        self.assertTrue(self.problems(heavy_vram_mib=1.5))
        self.assertEqual(self.problems(heavy_vram_mib=2048.0), [])

    def test_lists_must_be_lists_of_strings(self):
        for key in ("ignore_containers", "runtimes", "triggers"):
            for bad in ("backup-*", {"a": 1}, [1], ["a", None], None):
                problems = self.problems(**{key: bad})
                self.assertEqual(len(problems), 1, (key, bad))
                self.assertIn(f'"{key}"', problems[0])
                self.assertIn("list of strings", problems[0])

    def test_games(self):
        self.assertTrue(self.problems(games=[]))
        self.assertTrue(self.problems(games={"steam:1": "ignore"}))
        (problem,) = self.problems(games={"steam:1": {"action": "kill"}})
        self.assertIn("steam:1", problem)
        self.assertIn("ask, stop, ignore", problem)
        self.assertTrue(self.problems(games={"steam:1": {"name": 5}}))
        self.assertEqual(self.problems(games={"steam:1": {}, "steam:2": {"name": "X", "action": "stop"}}), [])

    def test_every_problem_is_reported(self):
        self.assertEqual(len(self.problems(on_launch="x", poll_seconds=-1, runtimes="docker")), 3)


class FromJsonTests(unittest.TestCase):
    def test_invalid_raises_with_all_messages(self):
        with self.assertRaises(ConfigError) as ctx:
            Config.from_json({"on_launch": "x", "poll_seconds": -1})
        self.assertIn("on_launch", str(ctx.exception))
        self.assertIn("poll_seconds", str(ctx.exception))

    def test_unknown_keys_are_ignored_with_a_warning(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            c = Config.from_json({"colour": "red", "on_launch": "stop"})
        self.assertEqual(c.on_launch, "stop")
        self.assertIn("colour", out.getvalue())

    def test_ints_become_floats_and_missing_rule_fields_default(self):
        c = Config.from_json({"poll_seconds": 2, "games": {"steam:5": {"action": "stop"}}})
        self.assertIsInstance(c.poll_seconds, float)
        self.assertEqual(c.games["steam:5"], GameRule("", "stop"))


class FileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "sub" / "config.json"

    def test_config_path_follows_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/x/cfg"}):
            self.assertEqual(cfg.config_path(), Path("/x/cfg/gpu-game-guard/config.json"))

    def test_missing_file_gives_defaults(self):
        self.assertEqual(cfg.load(self.path), Config())

    def test_save_then_load_and_format(self):
        c = Config(on_launch="stop", games={"steam:1": GameRule("One", "ignore")})
        cfg.save(c, self.path)
        self.assertEqual(cfg.load(self.path), c)
        text = self.path.read_text()
        self.assertIn("\n  ", text)
        keys = list(json.loads(text))
        self.assertEqual(keys, sorted(keys))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["config.json"])

    def test_invalid_json_and_invalid_values_raise(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{nope")
        with self.assertRaises(ConfigError) as ctx:
            cfg.load(self.path)
        self.assertIn("JSON", str(ctx.exception))
        self.path.write_text('{"dialog": "cli"}')
        with self.assertRaises(ConfigError) as ctx:
            cfg.load(self.path)
        self.assertIn("dialog", str(ctx.exception))

    def test_default_path_is_used(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": root}):
            cfg.save(Config(after_game="leave"))
            self.assertEqual(cfg.load().after_game, "leave")


class WatcherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.json"

    def write(self, text, mtime):
        self.path.write_text(text)
        os.utime(self.path, (mtime, mtime))

    def logged(self, watcher):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = watcher.current()
        return result, out.getvalue()

    def test_missing_file_gives_defaults(self):
        self.assertEqual(ConfigWatcher(self.path).current(), Config())

    def test_rereads_only_when_the_mtime_changes(self):
        self.write('{"on_launch": "stop"}', 1000)
        watcher = ConfigWatcher(self.path)
        self.assertEqual(watcher.current().on_launch, "stop")
        self.write('{"on_launch": "ignore"}', 1000)  # same mtime: not noticed
        self.assertEqual(watcher.current().on_launch, "stop")
        os.utime(self.path, (2000, 2000))
        self.assertEqual(watcher.current().on_launch, "ignore")

    def test_file_appearing_later_is_picked_up(self):
        watcher = ConfigWatcher(self.path)
        self.assertEqual(watcher.current().on_launch, "ask")
        self.write('{"on_launch": "stop"}', 1000)
        self.assertEqual(watcher.current().on_launch, "stop")

    def test_invalid_file_keeps_the_previous_config_and_logs_once(self):
        self.write('{"on_launch": "stop"}', 1000)
        watcher = ConfigWatcher(self.path)
        watcher.current()
        self.write('{"on_launch": "bogus"}', 2000)
        c, out = self.logged(watcher)
        self.assertEqual(c.on_launch, "stop")
        self.assertIn("on_launch", out)
        c, out = self.logged(watcher)
        self.assertEqual((c.on_launch, out), ("stop", ""))
        self.write("{broken", 3000)  # a different error is logged again
        c, out = self.logged(watcher)
        self.assertEqual(c.on_launch, "stop")
        self.assertIn("JSON", out)
        self.write('{"on_launch": "ignore"}', 4000)  # recovery
        self.assertEqual(watcher.current().on_launch, "ignore")

    def test_same_error_after_recovery_is_logged_again(self):
        watcher = ConfigWatcher(self.path)
        self.write("{broken", 1000)
        _, first = self.logged(watcher)
        self.write("{}", 2000)
        watcher.current()
        self.write("{broken", 3000)
        _, again = self.logged(watcher)
        self.assertTrue(first and again)

    def test_invalid_first_file_gives_defaults(self):
        self.write('{"poll_seconds": -3}', 1000)
        c, out = self.logged(ConfigWatcher(self.path))
        self.assertEqual(c, Config())
        self.assertIn("poll_seconds", out)


if __name__ == "__main__":
    unittest.main()
