from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gpu_game_guard import status
from gpu_game_guard.status import Status


class StatusTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "state"


def sample(**changes):
    base = dict(version="2.0", pid=42, updated=1000.0, state="game-running", paused_until=None, paused=False,
                game={"id": "steam:730", "title": "CS"},
                stopped=[{"kind": "container", "runtime": "docker", "name": "ollama"}],
                config_ok=False, config_error="bad", dialog="zenity", can_ask=False, targets=3)
    base.update(changes)
    return Status(**base)


class StatusFileTests(StatusTestCase):
    def test_round_trip(self):
        original = sample()
        status.write_status(original, self.dir)
        with mock.patch.object(status.time, "time", return_value=1005.0):
            self.assertEqual(status.read_status(self.dir), original)

    def test_missing_corrupt_and_wrong_shape_read_as_none(self):
        self.assertIsNone(status.read_status(self.dir))
        self.dir.mkdir(parents=True)
        for text in ("{nope", "[]", '"x"', ""):
            (self.dir / "status.json").write_text(text)
            self.assertIsNone(status.read_status(self.dir, max_age=1e12), text)

    def test_stale_status_reads_as_none(self):
        status.write_status(sample(updated=1000.0), self.dir)
        with mock.patch.object(status.time, "time", return_value=1014.0):
            self.assertIsNotNone(status.read_status(self.dir))
        with mock.patch.object(status.time, "time", return_value=1016.0):
            self.assertIsNone(status.read_status(self.dir))
            self.assertIsNotNone(status.read_status(self.dir, max_age=60))

    def test_paused_until_a_time_and_until_resumed(self):
        status.write_status(sample(paused=True, paused_until=2000.0), self.dir)
        with mock.patch.object(status.time, "time", return_value=1001.0):
            got = status.read_status(self.dir)
        self.assertEqual((got.paused, got.paused_until), (True, 2000.0))
        status.write_status(sample(paused=True, paused_until=math.inf), self.dir)
        raw = json.loads((self.dir / "status.json").read_text())
        self.assertEqual((raw["paused"], raw["paused_until"]), (True, None))
        with mock.patch.object(status.time, "time", return_value=1001.0):
            got = status.read_status(self.dir)
        self.assertEqual((got.paused, got.paused_until), (True, math.inf))

    def test_not_paused_has_no_end(self):
        status.write_status(sample(), self.dir)
        with mock.patch.object(status.time, "time", return_value=1001.0):
            got = status.read_status(self.dir)
        self.assertEqual((got.paused, got.paused_until), (False, None))

    def test_bad_field_types_are_tolerated(self):
        self.dir.mkdir(parents=True)
        (self.dir / "status.json").write_text(json.dumps(
            {"updated": 1000, "stopped": "x", "game": 5, "pid": "7", "config_error": 3, "paused": "yes"}))
        with mock.patch.object(status.time, "time", return_value=1001.0):
            got = status.read_status(self.dir)
        self.assertEqual((got.stopped, got.game, got.pid, got.config_error, got.paused), ([], None, 0, None, False))

    def test_default_directory_follows_xdg(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.dir)}):
            status.write_status(sample())
            self.assertTrue((self.dir / "gpu-game-guard" / "status.json").exists())
            with mock.patch.object(status.time, "time", return_value=1001.0):
                self.assertEqual(status.read_status().pid, 42)

    def test_the_file_is_readable_by_others_and_no_temp_files_remain(self):
        status.write_status(sample(), self.dir)
        mode = stat.S_IMODE((self.dir / "status.json").stat().st_mode)
        self.assertTrue(mode & stat.S_IRUSR)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["status.json"])


class EventTests(StatusTestCase):
    def test_append_and_read_newest_last(self):
        status.append_event("stop", "stopped ollama", self.dir, game="steam:730")
        status.append_event("restart", "started ollama", self.dir)
        events = status.read_events(directory=self.dir)
        self.assertEqual([e["kind"] for e in events], ["stop", "restart"])
        self.assertEqual((events[0]["message"], events[0]["game"]), ("stopped ollama", "steam:730"))
        self.assertIsInstance(events[0]["t"], float)

    def test_the_time_key_wins_over_a_field(self):
        status.append_event("a", "m", self.dir, t="x")
        event = status.read_events(directory=self.dir)[0]
        self.assertEqual((event["kind"], event["message"]), ("a", "m"))
        self.assertIsInstance(event["t"], float)

    def test_limit_returns_the_newest(self):
        for n in range(10):
            status.append_event("k", str(n), self.dir)
        self.assertEqual([e["message"] for e in status.read_events(3, self.dir)], ["7", "8", "9"])
        self.assertEqual(status.read_events(0, self.dir), [])

    def test_trimmed_to_the_last_300_lines(self):
        for n in range(320):
            status.append_event("k", str(n), self.dir)
        lines = (self.dir / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 300)
        events = status.read_events(1000, self.dir)
        self.assertEqual((events[0]["message"], events[-1]["message"]), ("20", "319"))

    def test_corrupt_lines_are_skipped_and_do_not_stop_appending(self):
        self.dir.mkdir(parents=True)
        (self.dir / "events.jsonl").write_text('{"kind": "a", "message": "1"}\n{broken\n[1]\n\n{"kind": "b", "message": "2"}')
        self.assertEqual([e["kind"] for e in status.read_events(directory=self.dir)], ["a", "b"])
        status.append_event("c", "3", self.dir)
        self.assertEqual([e["kind"] for e in status.read_events(directory=self.dir)], ["a", "b", "c"])

    def test_missing_file_reads_empty(self):
        self.assertEqual(status.read_events(directory=self.dir), [])


class PauseTests(StatusTestCase):
    def test_timed_pause_expires(self):
        with mock.patch.object(status.time, "time", return_value=1000.0):
            status.pause(60, self.dir)
        self.assertEqual(status.paused(self.dir, now=1030.0), (True, 1060.0))
        self.assertEqual(status.paused(self.dir, now=1060.0), (False, None))
        self.assertEqual(status.paused(self.dir, now=2000.0), (False, None))

    def test_pause_without_an_end_lasts_until_resumed(self):
        status.pause(None, self.dir)
        self.assertEqual(status.paused(self.dir, now=1e12), (True, None))
        status.resume(self.dir)
        self.assertEqual(status.paused(self.dir), (False, None))

    def test_infinite_seconds_means_until_resumed(self):
        status.pause(math.inf, self.dir)
        self.assertEqual(status.paused(self.dir), (True, None))

    def test_resume_without_a_pause_is_fine(self):
        status.resume(self.dir)
        self.assertEqual(status.paused(self.dir), (False, None))

    def test_unreadable_pause_file_is_not_paused(self):
        self.dir.mkdir(parents=True)
        for text in ("{nope", "[]", "{}", '{"until": "x"}', '{"until": true}'):
            (self.dir / "pause.json").write_text(text)
            self.assertEqual(status.paused(self.dir), (False, None), text)

    def test_now_defaults_to_the_wall_clock(self):
        status.pause(3600, self.dir)
        flag, until = status.paused(self.dir)
        self.assertTrue(flag)
        self.assertIsNotNone(until)

    def test_a_second_pause_replaces_the_first(self):
        with mock.patch.object(status.time, "time", return_value=1000.0):
            status.pause(60, self.dir)
            status.pause(600, self.dir)
        self.assertEqual(status.paused(self.dir, now=1001.0), (True, 1600.0))


if __name__ == "__main__":
    unittest.main()
