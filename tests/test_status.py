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
    base = dict(version="2.0", pid=os.getpid(), updated=1000.0, state="game-running", paused_until=None, paused=False,
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
                self.assertEqual(status.read_status().pid, os.getpid())

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


class ValidForAndPermissionTests(StatusTestCase):
    def test_valid_for_round_trips_and_defaults_to_15(self):
        self.assertEqual(Status().valid_for, 15.0)
        status.write_status(sample(valid_for=900.0, updated=1000.0), self.dir)
        with mock.patch.object(status.time, "time", return_value=1001.0):
            self.assertEqual(status.read_status(self.dir).valid_for, 900.0)

    def test_a_long_valid_for_extends_freshness(self):
        status.write_status(sample(valid_for=900.0, updated=1000.0), self.dir)
        with mock.patch.object(status.time, "time", return_value=1800.0):
            self.assertIsNotNone(status.read_status(self.dir))
        with mock.patch.object(status.time, "time", return_value=1901.0):
            self.assertIsNone(status.read_status(self.dir))

    def test_max_age_still_applies_when_larger(self):
        status.write_status(sample(valid_for=15.0, updated=1000.0), self.dir)
        with mock.patch.object(status.time, "time", return_value=1100.0):
            self.assertIsNone(status.read_status(self.dir))
            self.assertIsNotNone(status.read_status(self.dir, max_age=200))

    def test_a_missing_or_bad_valid_for_means_15(self):
        for value in ("x", None, float("nan"), -5, True):
            (self.dir).mkdir(parents=True, exist_ok=True)
            (self.dir / "status.json").write_text(json.dumps({"updated": 1000.0, "valid_for": value}))
            with mock.patch.object(status.time, "time", return_value=1016.0):
                self.assertIsNone(status.read_status(self.dir), value)

    def test_a_huge_valid_for_is_capped(self):
        status.write_status(sample(valid_for=1e12, updated=1000.0), self.dir)
        with mock.patch.object(status.time, "time", return_value=1000.0 + 4000):
            self.assertIsNone(status.read_status(self.dir))

    def test_new_files_are_private_and_the_directory_too(self):
        status.write_status(sample(), self.dir)
        status.append_event("stop", "x", self.dir)
        status.pause(60, self.dir)
        for name in ("status.json", "events.jsonl", "pause.json"):
            self.assertEqual(stat.S_IMODE((self.dir / name).stat().st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)

    def test_an_existing_events_file_is_tightened(self):
        self.dir.mkdir(parents=True)
        events = self.dir / "events.jsonl"
        events.write_text("")
        os.chmod(events, 0o644)
        status.append_event("stop", "x", self.dir)
        self.assertEqual(stat.S_IMODE(events.stat().st_mode), 0o600)


class LivenessTests(StatusTestCase):
    def read(self, **changes):
        status.write_status(sample(updated=1000.0, **changes), self.dir)
        with mock.patch.object(status.time, "time", return_value=1001.0):
            return status.read_status(self.dir)

    def test_a_live_pid_reads_as_running(self):
        self.assertEqual(self.read(pid=os.getpid()).pid, os.getpid())

    def test_a_dead_pid_reads_as_not_running_even_when_the_status_is_fresh(self):
        with mock.patch.object(status.os, "kill", side_effect=ProcessLookupError):
            self.assertIsNone(self.read(pid=4242, valid_for=900.0))

    def test_a_pid_of_another_user_counts_as_alive(self):
        with mock.patch.object(status.os, "kill", side_effect=PermissionError):
            self.assertIsNotNone(self.read(pid=4242))

    def test_pid_alive_probes_with_signal_zero(self):
        with mock.patch.object(status.os, "kill") as kill:
            self.assertTrue(status.pid_alive(77))
        kill.assert_called_once_with(77, 0)
        self.assertFalse(status.pid_alive(0))
        self.assertFalse(status.pid_alive(-3))
        with mock.patch.object(status.os, "kill", side_effect=OverflowError):
            self.assertFalse(status.pid_alive(10 ** 30))

    def test_a_reused_pid_is_detected_by_its_start_time(self):
        start = status.process_start(os.getpid())
        if start == 0:
            self.skipTest("no /proc on this system")
        self.assertIsNotNone(self.read(pid=os.getpid(), pid_start=start))
        self.assertIsNone(self.read(pid=os.getpid(), pid_start=start + 1000))

    def test_a_reused_pid_with_a_faked_start_time(self):
        with mock.patch.object(status, "process_start", return_value=500):
            self.assertIsNone(self.read(pid=os.getpid(), pid_start=400))
            self.assertIsNotNone(self.read(pid=os.getpid(), pid_start=500))

    def test_without_a_readable_start_time_a_live_pid_is_trusted(self):
        with mock.patch.object(status, "process_start", return_value=0):
            self.assertIsNotNone(self.read(pid=os.getpid(), pid_start=123))
        self.assertIsNotNone(self.read(pid=os.getpid(), pid_start=0))

    def test_process_start_reads_field_22_even_with_odd_command_names(self):
        stat_line = "123 (we ird) name) S " + " ".join(str(n) for n in range(4, 22)) + " 987654 23 24\n"
        with mock.patch.object(Path, "read_text", return_value=stat_line):
            self.assertEqual(status.process_start(123), 987654)
        with mock.patch.object(Path, "read_text", side_effect=FileNotFoundError):
            self.assertEqual(status.process_start(123), 0)
        with mock.patch.object(Path, "read_text", return_value="garbage"):
            self.assertEqual(status.process_start(123), 0)

    def test_pid_start_round_trips(self):
        self.assertEqual(self.read(pid=os.getpid(), pid_start=0).pid_start, 0)
        with mock.patch.object(status, "process_start", return_value=77):
            self.assertEqual(self.read(pid=os.getpid(), pid_start=77).pid_start, 77)

    def test_a_status_without_a_pid_is_not_judged_by_liveness(self):
        self.dir.mkdir(parents=True)
        (self.dir / "status.json").write_text(json.dumps({"updated": 1000.0}))
        with mock.patch.object(status.time, "time", return_value=1001.0):
            self.assertIsNotNone(status.read_status(self.dir))

    def test_remove_status(self):
        status.write_status(sample(pid=os.getpid()), self.dir)
        status.remove_status(self.dir, pid=os.getpid() + 1)
        self.assertTrue((self.dir / "status.json").exists())
        status.remove_status(self.dir, pid=os.getpid())
        self.assertFalse((self.dir / "status.json").exists())
        status.remove_status(self.dir)  # already gone

    def test_remove_status_removes_an_unreadable_file(self):
        self.dir.mkdir(parents=True)
        (self.dir / "status.json").write_text("{nope")
        status.remove_status(self.dir, pid=1)
        self.assertFalse((self.dir / "status.json").exists())

    def test_existing_status_and_pause_files_are_tightened(self):
        self.dir.mkdir(parents=True)
        os.chmod(self.dir, 0o755)
        for name in ("status.json", "pause.json"):
            (self.dir / name).write_text("{}")
            os.chmod(self.dir / name, 0o644)
        status.write_status(sample(), self.dir)
        status.pause(60, self.dir)
        for name in ("status.json", "pause.json"):
            self.assertEqual(stat.S_IMODE((self.dir / name).stat().st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
