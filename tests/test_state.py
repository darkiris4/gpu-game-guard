from __future__ import annotations

import json
import os
import tempfile
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
        for text in ("{nope", "[]", '{"stopped": 5, "stopped_at": 1, "game_seen": false}', '{"stopped": ["a"]}'):
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
        with mock.patch("gpu_game_guard.util.json.dump", side_effect=RuntimeError("disk")):
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


if __name__ == "__main__":
    unittest.main()
