"""The real guard, config watcher and state working together; only the outside world is faked."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from gpu_game_guard.config import ConfigWatcher
from gpu_game_guard.guard import Guard
from gpu_game_guard.state import State
from tests.fakes import FakePrompter, FakeRuntime, FakeTrigger, make_launch


class Rig:
    def __init__(self, test, config=None, running=("ollama", "whisper"), **answers):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.json"
        if config is not None:
            self.write(config)
        self.now = 1000.0
        self.trigger = FakeTrigger()
        self.runtime = FakeRuntime("docker", running)
        self.prompter = FakePrompter(**answers)
        self.state_path = Path(self.tmp.name) / "state.json"
        self.guard = Guard(ConfigWatcher(self.config_path), [self.trigger], [self.runtime], self.prompter,
                           State(self.state_path), clock=lambda: self.now, wall=lambda: self.now,
                           status_dir=Path(self.tmp.name) / "status", dialog_name="fake")

    def write(self, config):
        self.config_path.write_text(json.dumps(config))
        stat = self.config_path.stat()
        os.utime(self.config_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

    def step(self, seconds=1.0):
        self.now += seconds
        self.guard.step()


class IntegrationTests(unittest.TestCase):
    def test_a_whole_session_with_defaults_and_no_config_file(self):
        rig = Rig(self)
        rig.trigger.launches = [make_launch(10)]
        rig.step()
        self.assertEqual(rig.prompter.launch_asked, [["ollama", "whisper"]])
        self.assertEqual(sorted(rig.runtime.stopped), ["ollama", "whisper"])
        self.assertEqual(rig.trigger.relaunched, [make_launch(10)])
        saved = json.loads(rig.state_path.read_text())
        self.assertEqual(sorted(r["name"] for r in saved["stopped"]), ["ollama", "whisper"])

        rig.trigger.launches = [make_launch(11)]
        rig.step()
        rig.trigger.launches = []
        rig.step(30)
        self.assertEqual(rig.prompter.restart_asked, [["ollama", "whisper"]])
        self.assertEqual(sorted(rig.runtime.started), ["ollama", "whisper"])
        self.assertFalse(rig.state_path.exists())

    def test_editing_the_config_file_while_running_changes_behaviour(self):
        rig = Rig(self, config={})
        rig.trigger.launches = []
        rig.step()
        rig.write({"games": {"steam:730": {"name": "CS", "action": "ignore"}}})
        rig.trigger.launches = [make_launch(10, 730)]
        rig.step()
        self.assertEqual((rig.prompter.launch_asked, rig.trigger.ended), ([], []), "ignored game is left alone")
        rig.write({"games": {"steam:440": {"name": "TF2", "action": "stop"}}, "after_game": "restart"})
        rig.trigger.launches = [make_launch(10, 730), make_launch(12, 440)]
        rig.step()
        self.assertEqual(rig.prompter.launch_asked, [], "stop action does not prompt")
        self.assertEqual(sorted(rig.runtime.stopped), ["ollama", "whisper"])
        rig.trigger.launches = [make_launch(10, 730), make_launch(13, 440)]
        rig.step()
        rig.trigger.launches = []
        rig.step(30)
        self.assertEqual(sorted(rig.runtime.started), ["ollama", "whisper"])
        self.assertEqual(rig.prompter.restart_asked, [], "after_game restart does not prompt")

    def test_a_broken_edit_keeps_the_previous_settings_until_fixed(self):
        rig = Rig(self, config={"on_launch": "ignore"})
        rig.step()
        rig.write({"on_launch": "perhaps"})
        rig.trigger.launches = [make_launch(10)]
        rig.step()
        self.assertEqual(rig.prompter.launch_asked, [], "still on the previous 'ignore' setting")
        rig.write({"on_launch": "ask"})
        rig.trigger.launches = [make_launch(11)]
        rig.step()
        self.assertEqual(len(rig.prompter.launch_asked), 1)

    def test_ignored_containers_in_the_config_are_never_stopped(self):
        rig = Rig(self, config={"ignore_containers": ["whis*"]})
        rig.trigger.launches = [make_launch(10)]
        rig.step()
        self.assertEqual(rig.prompter.launch_asked, [["ollama"]])
        self.assertEqual(rig.runtime.stopped, ["ollama"])

    def test_nothing_happens_when_no_gpu_container_is_running(self):
        rig = Rig(self, running=())
        rig.trigger.launches = [make_launch(10)]
        rig.step()
        self.assertEqual((rig.prompter.launch_asked, rig.trigger.ended), ([], []))

    def test_a_restart_in_the_middle_of_a_game_leaves_it_running(self):
        rig = Rig(self)
        rig.trigger.launches = [make_launch(10)]
        restarted = Guard(ConfigWatcher(rig.config_path), [rig.trigger], [rig.runtime], rig.prompter,
                          State(rig.state_path), clock=lambda: rig.now, wall=lambda: rig.now,
                          status_dir=Path(rig.tmp.name) / "status")
        restarted.step()
        self.assertEqual((rig.prompter.launch_asked, rig.trigger.ended), ([], []))


class PreservationEndToEndTests(unittest.TestCase):
    def test_a_remembered_choice_keeps_unknown_keys_and_a_concurrent_save(self):
        rig = Rig(self, {"future": [1, 2], "games": {"steam:5": {"action": "stop", "note": "n"}}},
                  launch="stop", remember="stop")
        rig.trigger.launches = [make_launch(10)]
        rig.step()
        data = json.loads(rig.config_path.read_text())
        self.assertEqual(data["future"], [1, 2])
        self.assertEqual(data["games"]["steam:5"]["note"], "n")
        self.assertEqual(data["games"]["steam:730"]["action"], "stop")
        rig.step()  # the watcher picks up the guard's own write without complaint
        self.assertEqual(rig.guard.config_error, None)

    def test_an_invalid_file_is_left_alone_and_the_game_still_starts(self):
        rig = Rig(self, {"on_launch": "ask"}, launch="stop", remember="stop")
        rig.step()
        rig.config_path.write_text("{broken")
        rig.trigger.launches = [make_launch(10)]
        rig.step()
        self.assertEqual(rig.config_path.read_text(), "{broken")
        self.assertEqual(len(rig.trigger.relaunched), 1)


if __name__ == "__main__":
    unittest.main()
