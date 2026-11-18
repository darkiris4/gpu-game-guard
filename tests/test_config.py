from __future__ import annotations

import contextlib
import io
import json
import os
import re
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from gpu_game_guard import config as cfg
from gpu_game_guard.config import Config, ConfigError, ConfigWatcher, GameRule, TargetSpec


class DefaultsTests(unittest.TestCase):
    def test_defaults_match_the_contract(self):
        c = Config()
        self.assertEqual((c.on_launch, c.after_game, c.dialog), ("ask", "ask", "auto"))
        self.assertEqual((c.poll_seconds, c.exit_grace_seconds, c.no_game_timeout_seconds), (1.0, 8.0, 180.0))
        self.assertEqual((c.bypass_seconds, c.dialog_retry_seconds, c.heavy_vram_mib), (120.0, 300.0, 4096))
        self.assertEqual((c.runtimes, c.triggers, c.ignore_containers, c.games),
                         (["docker", "podman", "service", "command", "process"], ["steam"], [], {}))
        self.assertEqual((c.enabled, c.auto_containers, c.remember_choices, c.protect, c.targets), (True, True, True, [], []))
        self.assertEqual(cfg.validate(c.to_json()), [])

    def test_instances_do_not_share_lists(self):
        a, b = Config(), Config()
        a.runtimes.append("x")
        self.assertEqual(b.runtimes, ["docker", "podman", "service", "command", "process"])

    def test_to_json_has_every_key(self):
        keys = set(Config().to_json())
        self.assertEqual(keys, {"on_launch", "after_game", "poll_seconds", "exit_grace_seconds", "no_game_timeout_seconds",
                                "bypass_seconds", "dialog_retry_seconds", "heavy_vram_mib", "ignore_containers", "runtimes",
                                "triggers", "dialog", "games", "enabled", "auto_containers", "remember_choices", "protect",
                                "targets"})

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
        self.assertEqual(self.problems(poll_seconds=2, heavy_vram_mib=0, bypass_seconds=5), [])

    def test_top_level_must_be_an_object(self):
        self.assertIn("object", cfg.validate([])[0])

    def test_unknown_enum_values_name_the_key_and_choices(self):
        for key, allowed in (("on_launch", "ask, stop, ignore"), ("after_game", "ask, restart, leave"),
                             ("dialog", "auto, qt, zenity, kdialog, notify, none")):
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

    def test_ranges_reject_outside_values_and_name_key_range_and_value(self):
        ranges = {"poll_seconds": (0.2, 60), "exit_grace_seconds": (2, 3600), "no_game_timeout_seconds": (30, 86400),
                  "bypass_seconds": (5, 3600), "dialog_retry_seconds": (30, 86400), "heavy_vram_mib": (0, 10000000)}
        for key, (low, high) in ranges.items():
            self.assertEqual(self.problems(**{key: low}), [], key)
            self.assertEqual(self.problems(**{key: high}), [], key)
            for bad in (low - 0.1 if low else -1, high + 1, 10 ** 400, -(10 ** 400)):
                (problem,) = self.problems(**{key: bad})
                self.assertIn(f'"{key}"', problem)
                self.assertIn(f"from {low} to {high}", problem)
        (problem,) = self.problems(bypass_seconds=0)
        self.assertIn("got 0", problem)

    def test_zero_values_that_caused_loops_are_rejected(self):
        for key in ("bypass_seconds", "no_game_timeout_seconds", "poll_seconds", "exit_grace_seconds",
                    "dialog_retry_seconds"):
            self.assertEqual(len(self.problems(**{key: 0})), 1, key)

    def test_huge_integers_do_not_raise(self):
        self.assertEqual(len(self.problems(poll_seconds=10 ** 400)), 1)
        self.assertEqual(len(self.problems(heavy_vram_mib=10 ** 400)), 1)

    def test_validate_reports_unknown_keys_with_a_suggestion(self):
        (problem,) = self.problems(on_lauch="stop")
        self.assertEqual(problem, "unknown key 'on_lauch'; did you mean 'on_launch'?")
        (problem,) = self.problems(zzzzzz=1)
        self.assertEqual(problem, "unknown key 'zzzzzz'")

    def test_validate_reports_unknown_keys_in_a_game_rule(self):
        (problem,) = self.problems(games={"steam:1": {"actoin": "stop"}})
        self.assertIn("steam:1", problem)
        self.assertIn("unknown key 'actoin'; did you mean 'action'?", problem)

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
        with contextlib.redirect_stderr(out):
            c = Config.from_json({"colour": "red", "on_launch": "stop"})
        self.assertEqual(c.on_launch, "stop")
        self.assertIn("colour", out.getvalue())

    def test_unknown_keys_in_a_game_rule_are_ignored_with_one_warning(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c = Config.from_json({"colour": "red", "games": {"steam:1": {"action": "stop", "extra": 1}}})
        self.assertEqual(c.games["steam:1"].action, "stop")
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertIn("steam:1.extra", err.getvalue())

    def test_error_carries_reason_and_path(self):
        self.assertEqual(str(ConfigError("bad")), "bad")
        error = ConfigError("bad", Path("/x/c.json"))
        self.assertEqual((error.reason, error.path, str(error)), ("bad", Path("/x/c.json"), "/x/c.json: bad"))

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

    def test_errors_carry_the_path_without_repeating_it_in_the_reason(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{nope")
        with self.assertRaises(ConfigError) as ctx:
            cfg.load(self.path)
        self.assertEqual(ctx.exception.path, self.path)
        self.assertNotIn(str(self.path), ctx.exception.reason)
        self.assertTrue(str(ctx.exception).startswith(f"{self.path}: "))
        self.path.write_text('{"dialog": "cli"}')
        with self.assertRaises(ConfigError) as ctx:
            cfg.load(self.path)
        self.assertEqual(ctx.exception.path, self.path)
        self.assertNotIn(str(self.path), ctx.exception.reason)

    def test_huge_integer_is_a_config_error(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"poll_seconds": ' + "9" * 400 + "}")
        with self.assertRaises(ConfigError) as ctx:
            cfg.load(self.path)
        self.assertIn("poll_seconds", str(ctx.exception))

    def test_invalid_utf8_is_a_config_error(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'{"dialog": "\xff\xfe"}')
        with self.assertRaises(ConfigError) as ctx:
            cfg.load(self.path)
        self.assertIn("UTF-8", ctx.exception.reason)

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
        with contextlib.redirect_stderr(out):
            result = watcher.current()
        return result, out.getvalue()

    def test_missing_file_gives_defaults(self):
        self.assertEqual(ConfigWatcher(self.path).current(), Config())

    def test_rereads_when_the_mtime_changes(self):
        self.write('{"on_launch": "stop"}', 1000)
        watcher = ConfigWatcher(self.path)
        self.assertEqual(watcher.current().on_launch, "stop")
        self.assertEqual(watcher.current().on_launch, "stop")
        self.write('{"on_launch": "stop"}', 2000)
        self.write('{"on_launch": "ignore"}', 2000)
        self.assertEqual(watcher.current().on_launch, "ignore")

    def test_a_same_mtime_rewrite_with_another_size_is_noticed(self):
        self.write('{"on_launch": "stop"}', 1000)
        watcher = ConfigWatcher(self.path)
        watcher.current()
        self.write('{"on_launch": "ignore"}', 1000)
        self.assertEqual(watcher.current().on_launch, "ignore")

    def test_a_replaced_file_with_the_same_mtime_and_size_is_noticed(self):
        self.write('{"on_launch": "stop"}', 1000)
        watcher = ConfigWatcher(self.path)
        watcher.current()
        replacement = self.path.with_name("new.json")
        replacement.write_text('{"on_launch": "ask" }')
        os.utime(replacement, (1000, 1000))
        self.assertEqual(len('{"on_launch": "ask" }'), len('{"on_launch": "stop"}'))
        os.replace(replacement, self.path)
        self.assertEqual(watcher.current().on_launch, "ask")

    def test_a_briefly_missing_file_keeps_the_previous_settings(self):
        self.write('{"on_launch": "stop"}', 1000)
        watcher = ConfigWatcher(self.path)
        watcher.current()
        self.path.unlink()
        c, out = self.logged(watcher)
        self.assertEqual(c.on_launch, "stop")
        self.assertIn("missing", out)
        self.assertEqual(self.logged(watcher), (c, ""))
        self.write('{"on_launch": "ignore"}', 1000)
        self.assertEqual(watcher.current().on_launch, "ignore")

    def test_a_file_that_never_existed_gives_defaults(self):
        watcher = ConfigWatcher(self.path)
        self.assertEqual(watcher.current(), Config())
        self.assertEqual(watcher.current(), Config())

    def test_a_file_that_vanishes_between_stat_and_read_keeps_the_previous_settings(self):
        self.write('{"on_launch": "stop"}', 1000)
        watcher = ConfigWatcher(self.path)
        watcher.current()
        self.write('{"on_launch": "ignore"}', 2000)
        with mock.patch.object(Path, "read_text", side_effect=FileNotFoundError):
            self.assertEqual(watcher.current().on_launch, "stop")
        self.assertEqual(watcher.current().on_launch, "ignore")

    def test_invalid_utf8_keeps_the_previous_config_and_logs(self):
        self.write('{"on_launch": "stop"}', 1000)
        watcher = ConfigWatcher(self.path)
        watcher.current()
        self.path.write_bytes(b'{"on_launch": "\xff"}')
        os.utime(self.path, (2000, 2000))
        c, out = self.logged(watcher)
        self.assertEqual(c.on_launch, "stop")
        self.assertIn("UTF-8", out)

    def test_a_huge_integer_keeps_the_previous_config(self):
        self.write('{"on_launch": "stop"}', 1000)
        watcher = ConfigWatcher(self.path)
        watcher.current()
        self.write('{"poll_seconds": ' + "9" * 400 + "}", 2000)
        c, out = self.logged(watcher)
        self.assertEqual(c.on_launch, "stop")
        self.assertIn("poll_seconds", out)

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


SERVICE = {"id": "ollama", "kind": "service", "unit": "ollama.service"}
COMMAND = {"id": "unload", "kind": "command", "stop": ["a"], "start": ["b"]}
PROCESS = {"id": "obs", "kind": "process", "match": "obs"}


def problems_for(*targets):
    return cfg.validate({"targets": list(targets)})


class TargetTests(unittest.TestCase):
    def test_valid_targets_of_every_kind(self):
        self.assertEqual(problems_for(SERVICE, COMMAND, PROCESS), [])

    def test_defaults_are_filled_in(self):
        c = Config.from_json({"targets": [SERVICE, COMMAND, PROCESS]})
        service, command, process = c.targets
        self.assertEqual((service.label, service.enabled, service.scope), ("ollama", True, "system"))
        self.assertEqual((command.check, command.timeout_seconds), (None, 60.0))
        self.assertEqual((process.restart, process.enabled), (True, True))

    def test_round_trip_is_exact(self):
        c = Config(
            enabled=False, auto_containers=False, remember_choices=False, protect=["x*"],
            targets=[TargetSpec("ollama", "service", "Ollama", False, unit="ollama.service", scope="user"),
                     TargetSpec("unload", "command", "Unload", True, stop=["s", "1"], start=["true"], check=["true"],
                                timeout_seconds=12.5),
                     TargetSpec("obs", "process", "OBS", True, match="obs", restart=False)])
        again = Config.from_json(json.loads(json.dumps(c.to_json())))
        self.assertEqual(again, c)
        self.assertEqual(again.to_json(), c.to_json())

    def test_a_spec_writes_only_the_keys_of_its_kind(self):
        service = TargetSpec("a", "service", unit="a.service", stop=["x"], match="m").to_json()
        self.assertEqual(set(service), {"id", "kind", "label", "enabled", "unit", "scope"})
        command = TargetSpec("a", "command", stop=["x"], start=["y"], unit="u.service").to_json()
        self.assertEqual(set(command), {"id", "kind", "label", "enabled", "stop", "start", "timeout_seconds"})
        process = TargetSpec("a", "process", match="m", unit="u.service").to_json()
        self.assertEqual(set(process), {"id", "kind", "label", "enabled", "match", "restart"})

    def test_old_config_files_load_unchanged(self):
        c = Config.from_json({"on_launch": "stop", "runtimes": ["docker"], "games": {"steam:1": {"action": "ignore"}}})
        self.assertEqual((c.on_launch, c.runtimes, c.targets, c.enabled), ("stop", ["docker"], [], True))

    def test_messages_name_the_target_key_and_rule(self):
        found = problems_for(dict(SERVICE, unit="ollama"), dict(COMMAND, id="c2", stop=[]), dict(PROCESS, id="p3", match="a/b"))
        self.assertEqual(len(found), 3)
        self.assertIn('"targets"[0] (id \'ollama\')', found[0])
        self.assertIn('"unit" must be a unit name ending in ".service"', found[0])
        self.assertIn('"targets"[1] (id \'c2\')', found[1])
        self.assertIn('"stop"', found[1])
        self.assertIn("non-empty list", found[1])
        self.assertIn('"targets"[2] (id \'p3\')', found[2])
        self.assertIn('must not contain "/"', found[2])

    def test_id_rules(self):
        for bad in ("", "Obs", "-a", "a b", "x" * 41, 5, None):
            self.assertTrue(problems_for(dict(PROCESS, id=bad)), bad)
        self.assertEqual(problems_for(dict(PROCESS, id="x" * 40), dict(PROCESS, id="a_b-1")), [])
        self.assertTrue(problems_for({"kind": "process", "match": "a"}))

    def test_duplicate_ids(self):
        found = problems_for(PROCESS, dict(PROCESS, label="Other"))
        self.assertEqual(len(found), 1)
        self.assertIn('"targets"[1]', found[0])
        self.assertIn("duplicates", found[0])

    def test_kind_and_common_keys(self):
        self.assertIn('"kind" must be one of', problems_for(dict(PROCESS, kind="docker"))[0])
        self.assertIn('"kind"', problems_for({"id": "a"})[0])
        self.assertIn('"label" must be a string', problems_for(dict(PROCESS, label=3))[0])
        self.assertIn('"enabled" must be true or false', problems_for(dict(PROCESS, enabled="yes"))[0])
        self.assertIn("must be an object", problems_for("x")[0])
        self.assertIn('"targets" must be a list', cfg.validate({"targets": {}})[0])

    def test_service_rules(self):
        for unit in ("a.timer", "a b.service", "../a.service", "", 4):
            self.assertTrue(problems_for(dict(SERVICE, unit=unit)), unit)
        self.assertTrue(problems_for({"id": "a", "kind": "service"}))
        self.assertTrue(problems_for(dict(SERVICE, scope="global")))
        self.assertEqual(problems_for(dict(SERVICE, scope="user")), [])

    def test_command_rules(self):
        self.assertTrue(problems_for({"id": "a", "kind": "command", "stop": ["a"]}))
        self.assertTrue(problems_for(dict(COMMAND, start="true")))
        self.assertTrue(problems_for(dict(COMMAND, stop=["a", 3])))
        self.assertTrue(problems_for(dict(COMMAND, check=[])))
        self.assertEqual(problems_for(dict(COMMAND, check=["true"])), [])
        for bad in (0, 601, "5", True, float("nan")):
            self.assertTrue(problems_for(dict(COMMAND, timeout_seconds=bad)), bad)
        for good in (1, 600, 2.5):
            self.assertEqual(problems_for(dict(COMMAND, timeout_seconds=good)), [])

    def test_process_rules(self):
        self.assertTrue(problems_for({"id": "a", "kind": "process"}))
        self.assertTrue(problems_for(dict(PROCESS, match="")))
        self.assertTrue(problems_for(dict(PROCESS, restart="no")))

    def test_unknown_target_keys_get_a_hint(self):
        found = problems_for(dict(PROCESS, restrat=True))
        self.assertEqual(len(found), 1)
        self.assertIn("unknown key 'restrat'", found[0])
        self.assertIn("did you mean 'restart'", found[0])
        self.assertIn('"targets"[0] (id \'obs\')', found[0])

    def test_a_key_of_another_kind_is_unknown(self):
        found = problems_for(dict(PROCESS, unit="a.service"))
        self.assertIn("unknown key 'unit'", found[0])

    def test_load_accepts_unknown_target_keys_with_a_warning(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c = Config.from_json({"targets": [dict(PROCESS, extra=1)]})
        self.assertEqual(len(c.targets), 1)
        self.assertIn("targets[0].extra", err.getvalue())

    def test_invalid_target_makes_load_fail(self):
        with self.assertRaises(ConfigError) as ctx:
            Config.from_json({"targets": [dict(PROCESS, match="")]})
        self.assertIn("targets", ctx.exception.reason)


class NewKeyTests(unittest.TestCase):
    def test_booleans_must_be_booleans(self):
        for key in ("enabled", "auto_containers", "remember_choices"):
            self.assertIn(f'"{key}" must be true or false', cfg.validate({key: 1})[0])
            self.assertEqual(cfg.validate({key: False}), [])
            self.assertFalse(getattr(Config.from_json({key: False}), key))

    def test_protect_is_a_list_of_strings(self):
        self.assertTrue(cfg.validate({"protect": "a"}))
        self.assertEqual(Config.from_json({"protect": ["a*"]}).protect, ["a*"])

    def test_is_protected_is_case_insensitive_glob(self):
        c = Config(protect=["My-Editor*", "obs"])
        self.assertTrue(c.is_protected("my-editor-2"))
        self.assertTrue(c.is_protected("OBS"))
        self.assertFalse(c.is_protected("obs-studio"))
        self.assertFalse(Config().is_protected("anything"))

    def test_typo_in_a_new_key_gets_a_hint(self):
        self.assertIn("did you mean 'auto_containers'", cfg.validate({"auto_container": True})[0])


class CopyTests(unittest.TestCase):
    def test_with_target_appends_and_replaces_without_mutating(self):
        base = Config()
        one = base.with_target(TargetSpec("a", "process", match="a"))
        self.assertEqual((base.targets, [t.id for t in one.targets]), ([], ["a"]))
        two = one.with_target(TargetSpec("a", "process", match="b", enabled=False))
        self.assertEqual([(t.match, t.enabled) for t in two.targets], [("b", False)])
        self.assertEqual(one.targets[0].match, "a")
        self.assertEqual(one.target("a").id, "a")
        self.assertIsNone(one.target("zz"))

    def test_without_target(self):
        base = Config(targets=[TargetSpec("a", "process", match="a"), TargetSpec("b", "process", match="b")])
        less = base.without_target("a")
        self.assertEqual(([t.id for t in base.targets], [t.id for t in less.targets]), (["a", "b"], ["b"]))
        self.assertEqual(base.without_target("zz"), base)

    def test_with_game_sets_and_keeps_the_name(self):
        base = Config()
        one = base.with_game("steam:1", "One", "stop")
        self.assertEqual((base.games, one.games["steam:1"]), ({}, GameRule("One", "stop")))
        two = one.with_game("steam:1", "", "ignore")
        self.assertEqual(two.games["steam:1"], GameRule("One", "ignore"))
        self.assertEqual(one.games["steam:1"].action, "stop")
        with self.assertRaises(ValueError):
            base.with_game("steam:1", "One", "maybe")


class PreservationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "config.json"

    def write(self, data):
        self.path.write_text(json.dumps(data))

    def test_unknown_keys_survive_load_and_save(self):
        self.write({"future": {"a": 1}, "games": {"steam:1": {"action": "stop", "note": "x"}},
                    "targets": [dict(PROCESS, pinned=True)]})
        with contextlib.redirect_stderr(io.StringIO()):
            loaded = cfg.load(self.path)
        self.assertEqual(loaded.extra, {"future": {"a": 1}})
        self.assertEqual(loaded.games["steam:1"].extra, {"note": "x"})
        self.assertEqual(loaded.targets[0].extra, {"pinned": True})
        cfg.save(loaded, self.path)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["future"], {"a": 1})
        self.assertEqual(data["games"]["steam:1"], {"action": "stop", "name": "", "note": "x"})
        self.assertTrue(data["targets"][0]["pinned"])

    def test_load_warns_once_naming_the_unknown_keys(self):
        self.write({"future": 1, "games": {"steam:1": {"note": "x"}}})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg.load(self.path)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("future", err.getvalue())
        self.assertIn("steam:1.note", err.getvalue())

    def test_validate_still_reports_unknown_keys(self):
        self.assertTrue(cfg.validate({"future": 1}))

    def test_known_keys_win_over_extra(self):
        c = Config(extra={"enabled": "junk", "x": 1})
        self.assertIs(c.to_json()["enabled"], True)
        self.assertEqual(c.to_json()["x"], 1)

    def test_with_game_keeps_the_rules_extra(self):
        c = Config(games={"a": GameRule("A", "ask", {"k": 1})}).with_game("a", "", "stop")
        self.assertEqual(c.games["a"], GameRule("A", "stop", {"k": 1}))

    def test_extra_is_not_shared_between_copies(self):
        c = Config(extra={"k": [1]})
        c2 = c.with_game("a", "A", "stop")
        c2.extra["k"].append(2)
        self.assertEqual(c.extra, {"k": [1]})


class UpdateFileTests(PreservationTests):
    def test_missing_file_means_defaults(self):
        updated = cfg.update_file(lambda c: c.with_game("a", "A", "stop"), self.path)
        self.assertEqual(updated.games["a"].action, "stop")
        self.assertEqual(cfg.load(self.path).games["a"].name, "A")

    def test_unknown_keys_survive_an_update(self):
        self.write({"future": 5, "on_launch": "stop"})
        with contextlib.redirect_stderr(io.StringIO()):
            cfg.update_file(lambda c: c.with_game("a", "A", "ignore"), self.path)
        data = json.loads(self.path.read_text())
        self.assertEqual((data["future"], data["on_launch"], data["games"]["a"]["action"]), (5, "stop", "ignore"))

    def test_two_writers_do_not_lose_each_other(self):
        started = threading.Event()
        release = threading.Event()

        def slow(c):
            started.set()
            release.wait(5)
            return c.with_game("a", "A", "stop")

        first = threading.Thread(target=lambda: cfg.update_file(slow, self.path))
        first.start()
        self.assertTrue(started.wait(5))
        second = threading.Thread(target=lambda: cfg.update_file(lambda c: c.with_game("b", "B", "ignore"), self.path))
        second.start()
        time.sleep(0.2)
        release.set()
        first.join(10)
        second.join(10)
        self.assertEqual(sorted(cfg.load(self.path).games), ["a", "b"])

    def test_a_settings_save_made_meanwhile_survives(self):
        self.write({"on_launch": "ask"})

        def mutate(c):
            self.write({"on_launch": "stop", "heavy_vram_mib": 100})  # changed after the caller's own read
            return c.with_game("a", "A", "stop")

        # the mutation sees the file as it is when the lock is held, so a save made before it is kept
        cfg.update_file(lambda c: c.with_game("a", "A", "stop"), self.path)
        self.write(dict(json.loads(self.path.read_text()), on_launch="stop", heavy_vram_mib=100))
        cfg.update_file(lambda c: c.with_game("b", "B", "stop"), self.path)
        loaded = cfg.load(self.path)
        self.assertEqual((loaded.on_launch, loaded.heavy_vram_mib, sorted(loaded.games)), ("stop", 100, ["a", "b"]))

    def test_an_invalid_file_is_left_untouched(self):
        for text in ("{not json", json.dumps({"poll_seconds": -1})):
            self.path.write_text(text)
            with self.assertRaises(ConfigError):
                cfg.update_file(lambda c: c.with_game("a", "A", "stop"), self.path)
            self.assertEqual(self.path.read_text(), text)
        self.assertEqual([p.name for p in self.dir.iterdir() if ".broken" in p.name or p.name.endswith(".bak")], [])

    def test_an_invalid_result_raises_and_writes_nothing(self):
        self.write({"on_launch": "stop"})
        before = self.path.read_text()
        with self.assertRaises(ConfigError):
            cfg.update_file(lambda c: Config(poll_seconds=-5), self.path)
        self.assertEqual(self.path.read_text(), before)

    def test_the_lock_file_is_private_and_next_to_the_config(self):
        cfg.update_file(lambda c: c, self.path)
        lock = self.dir / "config.json.lock"
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_a_lock_that_cannot_be_had_is_logged_and_the_update_goes_ahead(self):
        with mock.patch.object(cfg, "_LOCK_WAIT_SECONDS", 0.1), \
                mock.patch.object(cfg.fcntl, "flock", side_effect=BlockingIOError(11, "busy")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                cfg.update_file(lambda c: c.with_game("a", "A", "stop"), self.path)
        self.assertIn("without the lock", err.getvalue())
        self.assertIn("a", cfg.load(self.path).games)

    def test_without_fcntl_the_update_goes_ahead(self):
        err = io.StringIO()
        with mock.patch.object(cfg, "fcntl", None), contextlib.redirect_stderr(err):
            cfg.update_file(lambda c: c.with_game("a", "A", "stop"), self.path)
        self.assertIn("locking is not available", err.getvalue())
        self.assertIn("a", cfg.load(self.path).games)


class BackupTests(PreservationTests):
    def test_the_previous_valid_file_is_kept_as_bak(self):
        cfg.save(Config(on_launch="stop"), self.path)
        self.assertFalse((self.dir / "config.json.bak").exists())
        cfg.save(Config(on_launch="ignore"), self.path)
        self.assertEqual(json.loads((self.dir / "config.json.bak").read_text())["on_launch"], "stop")
        cfg.save(Config(on_launch="ask"), self.path)
        self.assertEqual(json.loads((self.dir / "config.json.bak").read_text())["on_launch"], "ignore")

    def test_an_identical_save_leaves_the_backup_alone(self):
        cfg.save(Config(on_launch="stop"), self.path)
        cfg.save(Config(on_launch="ignore"), self.path)
        cfg.save(Config(on_launch="ignore"), self.path)
        self.assertEqual(json.loads((self.dir / "config.json.bak").read_text())["on_launch"], "stop")

    def test_an_unusable_file_is_copied_aside_first(self):
        self.path.write_text("{broken")
        cfg.save(Config(), self.path)
        copies = list(self.dir.glob("config.json.broken-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), "{broken")
        self.assertTrue(re.fullmatch(r"config\.json\.broken-\d{8}-\d{6}", copies[0].name))
        self.assertFalse((self.dir / "config.json.bak").exists())
        self.assertEqual(cfg.load(self.path), Config())

    def test_a_file_with_invalid_values_counts_as_broken(self):
        self.write({"poll_seconds": -1})
        cfg.save(Config(), self.path)
        self.assertEqual(len(list(self.dir.glob("config.json.broken-*"))), 1)

    def test_only_the_newest_five_broken_copies_are_kept(self):
        for day in range(1, 9):
            (self.dir / f"config.json.broken-2020010{day}-000000").write_text("old")
        self.path.write_text("{broken")
        cfg.save(Config(), self.path)
        names = sorted(p.name for p in self.dir.glob("config.json.broken-*"))
        self.assertEqual(len(names), 5)
        self.assertNotIn("config.json.broken-20200101-000000", names)

    def test_a_symlinked_config_is_written_through(self):
        real = self.dir / "real.json"
        real.write_text("{}")
        link = self.dir / "link.json"
        link.symlink_to(real)
        cfg.save(Config(on_launch="stop"), link)
        self.assertTrue(link.is_symlink())
        self.assertEqual(json.loads(real.read_text())["on_launch"], "stop")

    def test_a_failed_backup_does_not_stop_the_save(self):
        cfg.save(Config(on_launch="stop"), self.path)
        err = io.StringIO()
        with mock.patch.object(cfg.shutil, "copyfile", side_effect=OSError(28, "No space left")), \
                contextlib.redirect_stderr(err):
            cfg.save(Config(on_launch="ignore"), self.path)
        self.assertIn("could not back up", err.getvalue())
        self.assertEqual(cfg.load(self.path).on_launch, "ignore")


class Merge3Tests(unittest.TestCase):
    def test_a_field_changed_by_ours_wins_otherwise_theirs(self):
        base = Config()
        merged = cfg.merge3(base, Config(on_launch="stop"), Config(on_launch="ignore", heavy_vram_mib=1))
        self.assertEqual((merged.on_launch, merged.heavy_vram_mib), ("stop", 1))

    def test_nothing_changed_by_ours_takes_theirs(self):
        theirs = Config(dialog="zenity", protect=["x"])
        self.assertEqual(cfg.merge3(Config(), Config(), theirs), theirs)

    def test_games_merge_per_id(self):
        base = Config(games={"a": GameRule("A", "ask"), "b": GameRule("B", "ask"), "c": GameRule("C", "ask"),
                             "d": GameRule("D", "ask")})
        ours = Config(games={"a": GameRule("A", "stop"), "b": GameRule("B", "ask"), "d": GameRule("D", "ask"),
                             "n": GameRule("N", "stop")})
        theirs = Config(games={"a": GameRule("A", "ignore"), "b": GameRule("B", "ignore"), "c": GameRule("C", "ask"),
                               "d": GameRule("D", "ignore"), "t": GameRule("T", "stop")})
        merged = cfg.merge3(base, ours, theirs)
        self.assertEqual(merged.games["a"].action, "stop")  # changed on both sides: ours
        self.assertEqual(merged.games["b"].action, "ignore")  # changed only by theirs
        self.assertNotIn("c", merged.games)  # removed by ours, untouched by theirs
        self.assertEqual(merged.games["d"].action, "ignore")
        self.assertEqual((merged.games["n"].action, merged.games["t"].action), ("stop", "stop"))

    def test_an_entry_removed_by_ours_but_changed_by_theirs_is_kept(self):
        base = Config(games={"a": GameRule("A", "ask")})
        theirs = Config(games={"a": GameRule("A", "stop")})
        self.assertEqual(cfg.merge3(base, Config(), theirs).games["a"].action, "stop")

    def test_an_entry_removed_by_theirs_stays_removed(self):
        base = Config(games={"a": GameRule("A", "ask")})
        self.assertEqual(cfg.merge3(base, Config(games=dict(base.games), on_launch="stop"), Config()).games, {})

    def test_targets_merge_per_id(self):
        def spec(i, **kw):
            return TargetSpec(i, "process", i.upper(), match=i * 3, **kw)

        base = Config(targets=[spec("aaa"), spec("bbb"), spec("ccc")])
        ours = Config(targets=[spec("aaa", enabled=False), spec("ccc"), spec("ooo")])
        theirs = Config(targets=[spec("aaa", restart=False), spec("bbb", enabled=False), spec("ccc"), spec("ttt")])
        merged = cfg.merge3(base, ours, theirs)
        self.assertEqual([t.id for t in merged.targets], ["aaa", "ccc", "ooo", "bbb", "ttt"])
        self.assertFalse(merged.target("aaa").enabled)
        self.assertTrue(merged.target("aaa").restart)  # ours replaces the whole entry
        self.assertFalse(merged.target("bbb").enabled)  # ours removed it, but theirs changed it

    def test_a_target_changed_by_theirs_survives_its_removal_by_ours(self):
        base = Config(targets=[TargetSpec("bbb", "process", match="bbb")])
        theirs = Config(targets=[TargetSpec("bbb", "process", match="bbb", enabled=False)])
        merged = cfg.merge3(base, Config(on_launch="stop"), theirs)
        self.assertFalse(merged.target("bbb").enabled)
        merged = cfg.merge3(base, Config(targets=[]), theirs)
        self.assertFalse(merged.target("bbb").enabled)

    def test_extra_merges_per_key(self):
        base = Config(extra={"a": 1, "b": 2})
        merged = cfg.merge3(base, Config(extra={"a": 9}), Config(extra={"a": 1, "b": 3, "c": 4}))
        self.assertEqual(merged.extra, {"a": 9, "b": 3, "c": 4})

    def test_the_inputs_are_not_modified(self):
        base, ours, theirs = Config(), Config(on_launch="stop"), Config(games={"a": GameRule("A", "stop")})
        merged = cfg.merge3(base, ours, theirs)
        merged.games["a"].action = "ignore"
        self.assertEqual(theirs.games["a"].action, "stop")


class FileStampTests(PreservationTests):
    def test_stamp_changes_with_the_file(self):
        self.assertIsNone(cfg.file_stamp(self.path))
        self.write({})
        first = cfg.file_stamp(self.path)
        self.assertEqual(len(first), 3)
        self.write({"on_launch": "stop"})
        self.assertNotEqual(cfg.file_stamp(self.path), first)


class WatcherResetTests(PreservationTests):
    def test_the_error_resets_when_the_file_is_deleted(self):
        watcher = ConfigWatcher(self.path)
        self.path.write_text("{bad")
        with contextlib.redirect_stderr(io.StringIO()):
            watcher.current()
        self.assertTrue(watcher.error)
        self.path.unlink()
        with contextlib.redirect_stderr(io.StringIO()):
            watcher.current()
        self.assertEqual(watcher.error, "")


class StricterValidationTests(unittest.TestCase):
    def test_process_match_length_and_literals(self):
        for bad in ("ab", "x" * 16, "***", "a*?", "[a]*"):
            found = problems_for(dict(PROCESS, match=bad))
            self.assertEqual(len(found), 1, bad)
            self.assertIn('"match"', found[0])
        self.assertIn("15", problems_for(dict(PROCESS, match="x" * 16))[0])
        self.assertIn("kernel", problems_for(dict(PROCESS, match="x" * 16))[0])
        self.assertIn("glob", problems_for(dict(PROCESS, match="ab*"))[0])
        for good in ("obs", "x" * 15, "obs*", "*obs*", "firefox*"):
            self.assertEqual(problems_for(dict(PROCESS, match=good)), [], good)

    def test_service_unit_rules(self):
        for bad in ("-x.service", "a*.service", "a[1].service", "a?.service", "a b.service", "a/b.service",
                    ".service.sh", "a.service\n", "\u00e9.service"):
            found = problems_for(dict(SERVICE, unit=bad))
            self.assertEqual(len(found), 1, repr(bad))
        self.assertIn("must not start with", problems_for(dict(SERVICE, unit="-x.service"))[0])
        self.assertIn("not a glob", problems_for(dict(SERVICE, unit="a*.service"))[0])
        for good in ("ollama.service", "user@1000.service", "dev-sda.service", "a:b_c.d@e.service"):
            self.assertEqual(problems_for(dict(SERVICE, unit=good)), [], good)

    def test_labels_must_be_unique_ignoring_case(self):
        found = problems_for(dict(PROCESS, label="Media"), dict(PROCESS, id="other", label="media"))
        self.assertEqual(len(found), 1)
        self.assertIn('"targets"[1]', found[0])
        self.assertIn("labels must differ", found[0])
        self.assertIn('"targets"[0]', found[0])

    def test_a_missing_label_counts_as_the_id(self):
        found = problems_for(dict(PROCESS, id="obs"), dict(COMMAND, id="x", label="OBS"))
        self.assertEqual(len(found), 1)
        with self.assertRaises(ConfigError):
            Config.from_json({"targets": [dict(PROCESS, id="obs"), dict(COMMAND, id="x", label="OBS")]})

    def test_distinct_labels_are_fine(self):
        self.assertEqual(problems_for(dict(PROCESS, label="A"), dict(PROCESS, id="b", label="B")), [])


if __name__ == "__main__":
    unittest.main()
