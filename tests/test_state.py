from __future__ import annotations

import contextlib
import io
import json
import math
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gpu_game_guard import util
from gpu_game_guard.state import State


def ref(name, runtime="docker"):
    return {"kind": "container", "runtime": runtime, "name": name}


class StateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "deep" / "state.json"

    def test_default_path_follows_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/x/state"}):
            self.assertEqual(State().path, Path("/x/state/gpu-game-guard/state.json"))

    def test_missing_or_corrupt_file_is_empty(self):
        self.assertEqual(State(self.path).stopped, [])
        self.path.parent.mkdir(parents=True)
        for text in ("{nope", "[]", '{"stopped": 5, "stopped_at": 1, "game_seen": false}', '{"stopped": "ollama"}',
                     '{"stopped": {"name": "a"}}'):
            self.path.write_text(text)
            s = State(self.path)
            self.assertEqual((s.stopped, s.stopped_at, s.game_seen), ([], 0.0, False), text)

    def test_add_unions_by_kind_runtime_name_and_resets_progress(self):
        s = State(self.path)
        s.add([ref("b"), ref("a")], 10.0)
        s.game_seen = True
        s.add([ref("a"), ref("a", "podman"), ref("c")], 20.0)
        self.assertEqual(s.stopped, [ref("a"), ref("b"), ref("c"), ref("a", "podman")])
        self.assertEqual((s.stopped_at, s.game_seen), (20.0, False))

    def test_add_copies_and_strips_extra_fields(self):
        s = State(self.path)
        original = dict(ref("a"), extra="x")
        s.add([original], 1.0)
        self.assertEqual(s.stopped, [ref("a")])

    def test_save_and_reload(self):
        s = State(self.path)
        s.add([ref("a"), ref("b", "podman")], 5.5)
        s.game_seen = True
        s.save()
        r = State(self.path)
        self.assertEqual((r.stopped, r.stopped_at, r.game_seen), (s.stopped, 5.5, True))

    def test_saving_nothing_removes_the_file(self):
        s = State(self.path)
        s.add([ref("a")], 1.0)
        s.save()
        self.assertTrue(self.path.exists())
        s.clear()
        self.assertFalse(self.path.exists())
        s.save()  # no file, still fine

    def test_old_format_is_migrated(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"stopped": ["ollama", "whisper"], "stopped_at": 7, "game_seen": True}))
        s = State(self.path)
        self.assertEqual(s.stopped, [ref("ollama"), ref("whisper")])
        self.assertEqual((s.stopped_at, s.game_seen), (7.0, True))
        s.save()
        self.assertEqual(json.loads(self.path.read_text())["stopped"], [ref("ollama"), ref("whisper")])

    def test_malformed_entries_are_dropped(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"stopped": [ref("a"), {"name": "x"}, 3, None, ""], "stopped_at": 1, "game_seen": False}))
        self.assertEqual(State(self.path).stopped, [ref("a")])

    def test_save_is_atomic(self):
        s = State(self.path)
        s.add([ref("a")], 1.0)
        s.save()
        before = self.path.read_text()
        s.add([ref("b")], 2.0)
        with mock.patch("gpu_game_guard.util.json.dumps", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                s.save()
        self.assertEqual(self.path.read_text(), before)
        s.save()
        self.assertEqual([p.name for p in self.path.parent.glob("*.json")], ["state.json"])

    def test_save_goes_through_atomic_write_json(self):
        s = State(self.path)
        s.add([ref("a")], 1.0)
        with mock.patch("gpu_game_guard.state.atomic_write_json", wraps=util.atomic_write_json) as write:
            s.save()
        write.assert_called_once()
        self.assertEqual(write.call_args[0][0], self.path)

    def load(self, **fields):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"stopped": [ref("a")], "stopped_at": 1.0, "game_seen": False}
        data.update(fields)
        self.path.write_text(json.dumps(data))
        return State(self.path)

    def test_stopped_must_be_a_list(self):
        self.assertEqual(self.load(stopped="ollama").stopped, [])
        self.assertEqual(self.load(stopped=None).stopped, [])

    def test_game_seen_must_be_a_bool(self):
        for bad in ("false", "true", 1, None, []):
            self.assertFalse(self.load(game_seen=bad).game_seen, bad)
        self.assertTrue(self.load(game_seen=True).game_seen)

    def test_stopped_at_must_be_a_finite_number_not_in_the_future(self):
        before = time.time()
        for bad in (float("nan"), float("inf"), before + 3600, "5", None, True, [1]):
            text = json.dumps({"stopped": [ref("a")], "stopped_at": bad, "game_seen": False}).replace("Infinity", "1e999")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text)
            at = State(self.path).stopped_at
            self.assertTrue(before <= at <= time.time(), bad)
        self.assertEqual(self.load(stopped_at=before - 5).stopped_at, before - 5)
        soon = time.time() + 30
        self.assertEqual(self.load(stopped_at=soon).stopped_at, soon)

    def test_huge_integer_timestamp_is_replaced(self):
        at = self.load(stopped_at=10 ** 400).stopped_at
        self.assertTrue(math.isfinite(at))
        self.assertLessEqual(at, time.time())


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "out.json"

    def test_a_failed_write_leaves_no_temporary_file(self):
        with mock.patch("gpu_game_guard.util.json.dumps", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                util.atomic_write_json(self.path, {"a": 1})
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_a_failed_replace_leaves_no_temporary_file(self):
        with mock.patch("gpu_game_guard.util.os.replace", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                util.atomic_write_json(self.path, {"a": 1})
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_a_new_file_gets_the_default_mode_under_the_umask(self):
        old = os.umask(0o022)
        try:
            util.atomic_write_json(self.path, {"a": 1})
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_an_existing_file_keeps_its_mode(self):
        self.path.write_text("{}")
        os.chmod(self.path, 0o664)
        util.atomic_write_json(self.path, {"a": 1})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o664)
        self.assertEqual(json.loads(self.path.read_text()), {"a": 1})

    def test_the_file_is_synced_before_it_replaces_the_target(self):
        with mock.patch("gpu_game_guard.util.os.fsync") as fsync:
            util.atomic_write_json(self.path, {"a": 1})
        self.assertGreaterEqual(fsync.call_count, 1)

    def test_log_writes_to_stderr(self):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            util.log("hello")
        self.assertEqual((out.getvalue(), err.getvalue()), ("", "hello\n"))


class OptionalKeyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.json"

    def load(self, stopped):
        self.path.write_text(json.dumps({"stopped": stopped, "stopped_at": 1.0, "game_seen": False}))
        return State(self.path).stopped

    def test_valid_optional_keys_survive_a_round_trip(self):
        full = {"kind": "process", "runtime": "process", "name": "obs", "scope": "user", "unit": "obs.service",
                "id": "obs", "cmdline": ["/usr/bin/obs", "--minimized"], "cwd": "/home/u"}
        s = State(self.path)
        s.add([full], 100.0)
        s.save()
        self.assertEqual(State(self.path).stopped, [full])

    def test_invalid_optional_keys_and_unknown_keys_are_dropped(self):
        bad = {"kind": "process", "runtime": "process", "name": "obs", "scope": 3, "unit": None, "id": [],
               "cmdline": ["a", 1], "cwd": 5, "extra": "x"}
        self.assertEqual(self.load([bad]), [{"kind": "process", "runtime": "process", "name": "obs"}])
        self.assertEqual(self.load([dict(bad, cmdline="obs")]), [{"kind": "process", "runtime": "process", "name": "obs"}])

    def test_empty_cmdline_and_strings_are_kept(self):
        got = self.load([{"kind": "process", "runtime": "process", "name": "a", "cmdline": [], "cwd": ""}])
        self.assertEqual(got[0]["cmdline"], [])
        self.assertEqual(got[0]["cwd"], "")

    def test_add_drops_unknown_keys_and_stays_unique(self):
        s = State(self.path)
        s.add([dict(ref("a"), junk=1, id="x")], 1.0)
        s.add([dict(ref("a"), id="y"), ref("b"), dict(ref("a"), id="x", scope="user")], 2.0)
        self.assertEqual(s.stopped, [ref("b"), dict(ref("a"), id="x"), dict(ref("a"), id="y")])

    def test_stored_duplicates_collapse(self):
        got = self.load([ref("a"), ref("a"), ref("a", "podman")])
        self.assertEqual([(r["runtime"], r["name"]) for r in got], [("docker", "a"), ("podman", "a")])
        self.assertNotIn("id", got[0])

    def test_optional_keys_are_not_part_of_the_identity(self):
        s = State(self.path)
        s.add([dict(ref("a"), unit="a.service")], 1.0)
        s.add([dict(ref("a"), unit="other.service")], 2.0)
        self.assertEqual(len(s.stopped), 1)


class KeyAndPermissionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "fresh" / "state.json"

    def test_two_targets_with_the_same_label_are_both_recorded(self):
        s = State(self.path)
        s.add([dict(ref("same"), id="a"), dict(ref("same"), id="b")], 1.0)
        s.save()
        self.assertEqual([r["id"] for r in State(self.path).stopped], ["a", "b"])

    def test_remove_forgets_only_the_given_refs(self):
        s = State(self.path)
        s.add([dict(ref("same"), id="a"), dict(ref("same"), id="b")], 1.0)
        s.remove([dict(ref("same"), id="a")])
        self.assertEqual([r["id"] for r in s.stopped], ["b"])
        s.remove([dict(ref("same"), id="b")])
        self.assertEqual((s.stopped, s.stopped_at, s.game_seen), ([], 0.0, False))

    def test_a_corrupt_file_logs_one_line_and_reads_as_empty(self):
        self.path.parent.mkdir(parents=True)
        for content in (b"{nope", b"\xff\xfe", b"[1]"):
            self.path.write_bytes(content)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                s = State(self.path)
            self.assertEqual(s.stopped, [])
            self.assertEqual(len(err.getvalue().strip().splitlines()), 1, content)

    def test_a_missing_file_is_silent(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            State(self.path)
        self.assertEqual(err.getvalue(), "")

    def test_the_file_is_read_as_utf8(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"stopped": [ref("caf\u00e9")], "stopped_at": 1.0}, ensure_ascii=False),
                             encoding="utf-8")
        with mock.patch.dict(os.environ, {"LC_ALL": "C", "LANG": "C"}):
            self.assertEqual(State(self.path).stopped[0]["name"], "caf\u00e9")

    def test_new_files_are_private_and_the_directory_too(self):
        s = State(self.path)
        s.add([ref("a")], 1.0)
        s.save()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_an_existing_file_and_directory_keep_their_modes(self):
        self.path.parent.mkdir(parents=True)
        os.chmod(self.path.parent, 0o755)
        self.path.write_text("{}")
        os.chmod(self.path, 0o640)
        s = State(self.path)
        s.add([ref("a")], 1.0)
        s.save()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o755)

    def test_atomic_write_json_private_option(self):
        target = self.path.parent / "x.json"
        util.atomic_write_json(target, {"a": 1})
        self.assertNotEqual(stat.S_IMODE(target.stat().st_mode), 0o600 if os.umask(0o022) == 0o077 else 0)
        private = self.path.parent / "y.json"
        util.atomic_write_json(private, {"a": 1}, private=True)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()


class SymlinkedWriteTests(unittest.TestCase):
    def test_a_symlinked_file_is_written_through_and_stays_a_symlink(self):
        import tempfile
        from pathlib import Path

        from gpu_game_guard.util import atomic_write_json

        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "managed" / "real.json"
            target.parent.mkdir()
            target.write_text("{}")
            link = Path(root) / "link.json"
            link.symlink_to(target)
            atomic_write_json(link, {"a": 1})
            self.assertTrue(link.is_symlink())
            self.assertIn('"a": 1', target.read_text())
