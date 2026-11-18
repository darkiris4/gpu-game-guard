from __future__ import annotations

import contextlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gpu_game_guard import config as configmod, status
from gpu_game_guard.config import Config, GameRule
from gpu_game_guard.guard import Guard, Plan, plan
from gpu_game_guard.model import Target
from gpu_game_guard.state import State

from .fakes import FakePrompter, FakeRuntime, FakeTrigger, FakeWatcher, make_launch


def ref(name, runtime="docker"):
    return {"kind": "container", "runtime": runtime, "name": name}


class Env:
    def __init__(self, running=("ollama",), launch="stop", restart=True, config=None, podman=None, can_ask=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.trigger = FakeTrigger()
        self.docker = FakeRuntime("docker", running)
        self.podman = FakeRuntime("podman", podman or ())
        self.runtimes = [self.docker] + ([self.podman] if podman is not None else [])
        self.prompter = FakePrompter(launch, restart, can_ask)
        self.watcher = FakeWatcher(config)
        self.state = State(Path(self.tmp.name) / "state.json")
        self.status_dir = Path(self.tmp.name) / "status"
        self.guard = self.make_guard()

    def make_guard(self):
        return Guard(self.watcher, [self.trigger], self.runtimes, self.prompter, self.state,
                     gpu_totals=lambda: (5120, 16311), clock=lambda: self.now, wall=lambda: self.now,
                     status_dir=self.status_dir, dialog_name="fake")

    @property
    def config(self):
        return self.watcher.config

    def advance(self, seconds):
        self.now += seconds
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.guard.step()
        return out.getvalue()


class GuardCase(unittest.TestCase):
    def env(self, **options):
        env = Env(**options)
        self.addCleanup(env.tmp.cleanup)
        return env


class FlowTests(GuardCase):
    def test_no_gpu_workloads_lets_the_game_start(self):
        env = self.env(running=())
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual((env.prompter.launch_asked, env.trigger.ended), ([], []))

    def test_every_game_id_is_intercepted(self):
        env = self.env()
        launch = make_launch(10, app_id=3000000000)
        env.trigger.launches = [launch]
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])
        self.assertEqual(env.trigger.ended, [launch])
        self.assertEqual(env.prompter.totals_seen, [(5120, 16311)])

    def test_launches_already_running_at_startup_are_left_alone(self):
        env = self.env()
        env.trigger.launches = [make_launch(10)]
        restarted = env.make_guard()
        restarted.step()
        self.assertEqual((env.prompter.launch_asked, env.trigger.ended), ([], []))
        env.trigger.launches = [make_launch(10), make_launch(11, app_id=440)]
        restarted.step()
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])

    def test_stop_then_relaunch_then_restart_after_exit(self):
        env = self.env()
        launch = make_launch(10)
        env.trigger.launches = [launch]
        env.advance(1)
        self.assertEqual(env.trigger.ended, [launch])
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])
        self.assertEqual(env.docker.stopped, ["ollama"])
        self.assertEqual(env.trigger.relaunched, [launch])
        self.assertEqual(env.state.stopped, [ref("ollama")])
        self.assertTrue(env.state.path.exists())

        env.trigger.launches = []
        env.advance(2)
        self.assertEqual(env.prompter.restart_asked, [], "no restart question before the game has run")

        env.trigger.launches = [make_launch(11)]
        env.advance(2)
        self.assertEqual(env.prompter.launch_asked, [["ollama"]], "the relaunch is not asked about again")
        self.assertTrue(env.state.game_seen)

        env.trigger.launches = []
        env.advance(env.config.exit_grace_seconds - 1)
        self.assertEqual(env.prompter.restart_asked, [])
        env.advance(2)
        self.assertEqual(env.prompter.restart_asked, [["ollama"]])
        self.assertEqual(env.docker.started, ["ollama"])
        self.assertEqual(env.state.stopped, [])
        self.assertFalse(env.state.path.exists())

    def test_launch_anyway_relaunches_without_stopping(self):
        env = self.env(launch="anyway")
        launch = make_launch(10)
        env.trigger.launches = [launch]
        env.advance(1)
        self.assertEqual(env.docker.stopped, [])
        self.assertEqual(env.trigger.relaunched, [launch])
        env.trigger.launches = [make_launch(11)]
        env.advance(1)
        env.trigger.launches = []
        env.advance(env.config.exit_grace_seconds + 1)
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])
        self.assertEqual(env.prompter.restart_asked, [])

    def test_cancel_ends_the_launch_and_does_nothing_else(self):
        env = self.env(launch="cancel")
        launch = make_launch(10)
        env.trigger.launches = [launch]
        env.advance(1)
        self.assertEqual(env.trigger.ended, [launch])
        self.assertEqual((env.docker.stopped, env.trigger.relaunched), ([], []))
        self.assertEqual(env.state.stopped, [])

    def test_a_lingering_ended_launcher_does_not_ask_twice(self):
        env = self.env(launch="cancel")
        env.trigger.launches = [make_launch(10)]
        for _ in range(3):
            env.advance(1)
        self.assertEqual(len(env.prompter.launch_asked), 1)
        self.assertEqual(len(env.trigger.ended), 1)

    def test_a_lingering_launcher_does_not_count_as_the_game_running(self):
        env = self.env(launch="stop")
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.advance(1)  # the killed launcher is still listed
        self.assertFalse(env.state.game_seen)

    def test_leaving_containers_stopped_clears_the_state(self):
        env = self.env(restart=False)
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.trigger.launches = [make_launch(11)]
        env.advance(1)
        env.trigger.launches = []
        env.advance(env.config.exit_grace_seconds + 1)
        self.assertEqual(env.docker.started, [])
        self.assertEqual(env.state.stopped, [])
        self.assertFalse(env.state.path.exists())

    def test_restart_is_asked_when_the_relaunched_game_never_appears(self):
        env = self.env()
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.trigger.launches = []
        env.advance(env.config.no_game_timeout_seconds - 5)
        self.assertEqual(env.prompter.restart_asked, [])
        env.advance(10)
        self.assertEqual(env.prompter.restart_asked, [["ollama"]])

    def test_state_left_by_an_earlier_run_is_asked_about_at_startup(self):
        env = self.env(running=())
        env.state.add([ref("ollama")], env.now - 3600)
        env.state.save()
        env.guard.state = State(env.state.path)
        env.advance(1)
        self.assertEqual(env.prompter.restart_asked, [["ollama"]])
        self.assertEqual(env.docker.started, ["ollama"])

    def test_old_format_state_is_restarted_through_docker(self):
        env = self.env(running=())
        env.state.path.write_text('{"stopped": ["ollama"], "stopped_at": 1.0, "game_seen": false}')
        env.guard.state = State(env.state.path)
        env.advance(1)
        self.assertEqual(env.docker.started, ["ollama"])

    def test_several_stops_in_a_row_keep_every_container(self):
        env = self.env(running=("a",))
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.docker.running = ["b"]
        env.trigger.launches = [make_launch(11, app_id=440)]
        env.advance(1)
        self.assertEqual(env.state.stopped, [ref("a"), ref("b")])

    def test_the_relaunch_is_let_through_only_for_the_bypass_window(self):
        env = self.env(launch="anyway")
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.trigger.launches = [make_launch(11)]
        env.advance(1)
        self.assertEqual(len(env.prompter.launch_asked), 1)
        env.trigger.launches = []
        env.advance(5)
        env.advance(env.config.bypass_seconds + 1)
        env.trigger.launches = [make_launch(12)]
        env.advance(1)
        self.assertEqual(len(env.prompter.launch_asked), 2)

    def test_bypass_is_per_game(self):
        env = self.env(launch="anyway")
        env.trigger.launches = [make_launch(10, 730)]
        env.advance(1)
        env.trigger.launches = [make_launch(10, 730), make_launch(12, 440)]
        env.advance(1)
        self.assertEqual(len(env.prompter.launch_asked), 2)


class MissingDialogTests(GuardCase):
    def test_launch_answer_anyway_or_none_lets_the_game_start(self):
        for answer in ("anyway", None, "bogus"):
            env = self.env(launch=answer)
            launch = make_launch(10)
            env.trigger.launches = [launch]
            env.advance(1)
            self.assertEqual(env.trigger.relaunched, [launch], answer)
            self.assertEqual((env.docker.stopped, env.state.stopped), ([], []), answer)

    def test_a_prompter_that_raises_lets_the_game_start(self):
        env = self.env()
        env.prompter.launch = lambda *a: 1 / 0
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(len(env.trigger.relaunched), 1)
        self.assertEqual(env.docker.stopped, [])

    def test_no_restart_dialog_keeps_the_state_and_asks_again_later(self):
        env = self.env(running=("a",), restart=None)
        env.state.add([ref("a")], env.now - 3600)
        env.state.save()
        env.advance(1)
        env.advance(1)
        self.assertEqual(len(env.prompter.restart_asked), 1, "no repeated asking within the retry delay")
        self.assertEqual(env.state.stopped, [ref("a")])
        self.assertTrue(env.state.path.exists())
        env.advance(env.config.dialog_retry_seconds + 1)
        self.assertEqual(len(env.prompter.restart_asked), 2)
        env.prompter.restart_answer = True
        env.advance(env.config.dialog_retry_seconds + 1)
        self.assertEqual(env.docker.started, ["a"])
        self.assertEqual(env.state.stopped, [])

    def test_a_restart_dialog_that_raises_is_retried(self):
        env = self.env()
        env.prompter.restart = lambda refs: 1 / 0
        env.state.add([ref("a")], env.now - 3600)
        env.advance(1)
        self.assertEqual(env.state.stopped, [ref("a")])


class ActionTests(GuardCase):
    def rule(self, env, action, game="steam:730", **top):
        env.config.games[game] = GameRule("Game", action)
        for key, value in top.items():
            setattr(env.config, key, value)

    def test_ignore_never_touches_the_launch(self):
        env = self.env()
        self.rule(env, "ignore")
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.advance(1)
        self.assertEqual((env.trigger.ended, env.trigger.relaunched, env.prompter.launch_asked), ([], [], []))
        self.assertEqual((env.docker.stopped, env.state.stopped, env.prompter.notifications), ([], [], []))

    def test_ignored_game_counts_as_running_so_no_restart_prompt_meanwhile(self):
        env = self.env()
        env.state.add([ref("a")], env.now - 3600)
        env.state.game_seen = True
        self.rule(env, "ignore")
        env.trigger.launches = [make_launch(10)]
        env.advance(100)
        self.assertEqual(env.prompter.restart_asked, [])

    def test_ignore_as_on_launch_applies_to_every_game_except_overrides(self):
        env = self.env()
        env.config.on_launch = "ignore"
        self.rule(env, "ask", game="steam:440")
        env.trigger.launches = [make_launch(10, 730)]
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [])
        env.trigger.launches = [make_launch(10, 730), make_launch(11, 440)]
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])

    def test_stop_acts_without_a_dialog_and_notifies(self):
        env = self.env(launch="cancel")
        self.rule(env, "stop")
        launch = make_launch(10)
        env.trigger.launches = [launch]
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [])
        self.assertEqual(env.trigger.ended, [launch])
        self.assertEqual(env.docker.stopped, ["ollama"])
        self.assertEqual(env.trigger.relaunched, [launch])
        self.assertEqual(env.state.stopped, [ref("ollama")])
        self.assertEqual(len(env.prompter.notifications), 1)
        self.assertIn("ollama", env.prompter.notifications[0][1])

    def test_ask_prompts_even_when_on_launch_is_stop(self):
        env = self.env(launch="cancel")
        env.config.on_launch = "stop"
        self.rule(env, "ask")
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])
        self.assertEqual(env.docker.stopped, [])

    def test_on_launch_stop_applies_to_games_without_a_rule(self):
        env = self.env(launch="cancel")
        env.config.on_launch = "stop"
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual((env.prompter.launch_asked, env.docker.stopped), ([], ["ollama"]))

    def test_a_rule_added_while_running_takes_effect(self):
        env = self.env(launch="cancel")
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(len(env.prompter.launch_asked), 1)
        env.trigger.launches = []
        env.advance(1)
        self.rule(env, "ignore")
        env.trigger.launches = [make_launch(11)]
        env.advance(1)
        self.assertEqual(len(env.prompter.launch_asked), 1)
        self.rule(env, "stop")
        env.trigger.launches = [make_launch(11)]
        env.advance(1)
        self.assertEqual(env.docker.stopped, [], "the earlier launch was already looked at")
        env.trigger.launches = [make_launch(11), make_launch(12)]
        env.advance(1)
        self.assertEqual(env.docker.stopped, ["ollama"])

    def test_config_changes_between_steps_apply_to_timing(self):
        env = self.env()
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.trigger.launches = [make_launch(11)]
        env.advance(1)
        env.trigger.launches = []
        env.config.exit_grace_seconds = 60
        env.advance(30)
        self.assertEqual(env.prompter.restart_asked, [])
        env.config.exit_grace_seconds = 5
        env.advance(1)
        self.assertEqual(env.prompter.restart_asked, [["ollama"]])

    def test_after_game_restart_starts_without_a_dialog_and_notifies(self):
        env = self.env(restart=False)
        env.config.after_game = "restart"
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.trigger.launches = [make_launch(11)]
        env.advance(1)
        env.trigger.launches = []
        env.advance(env.config.exit_grace_seconds + 1)
        self.assertEqual(env.prompter.restart_asked, [])
        self.assertEqual(env.docker.started, ["ollama"])
        self.assertEqual(env.state.stopped, [])
        self.assertFalse(env.state.path.exists())
        self.assertEqual(len(env.prompter.notifications), 1)
        self.assertIn("ollama", env.prompter.notifications[0][1])

    def test_after_game_leave_forgets_without_asking_or_starting(self):
        env = self.env(restart=True)
        env.config.after_game = "leave"
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.trigger.launches = [make_launch(11)]
        env.advance(1)
        env.trigger.launches = []
        env.advance(env.config.exit_grace_seconds + 1)
        self.assertEqual((env.prompter.restart_asked, env.docker.started, env.prompter.notifications), ([], [], []))
        self.assertEqual(env.state.stopped, [])
        self.assertFalse(env.state.path.exists())


class IgnoreContainersTests(GuardCase):
    def test_ignored_containers_are_never_stopped_or_listed(self):
        env = self.env(running=("ollama", "backup-nightly", "backup-x"))
        env.config.ignore_containers = ["backup-*"]
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])
        self.assertEqual(env.docker.stopped, ["ollama"])
        self.assertEqual(env.state.stopped, [ref("ollama")])

    def test_when_only_ignored_containers_run_nothing_happens(self):
        env = self.env(running=("backup-1",))
        env.config.ignore_containers = ["backup-*"]
        launch = make_launch(10)
        env.trigger.launches = [launch]
        env.advance(1)
        self.assertEqual((env.trigger.ended, env.prompter.launch_asked, env.trigger.relaunched), ([], [], []))
        self.assertEqual(env.state.stopped, [])


class RuntimeTests(GuardCase):
    def test_two_runtimes_are_combined_and_each_stops_its_own(self):
        env = self.env(running=("ollama",), podman=("comfy",))
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [["ollama", "comfy"]])
        self.assertEqual((env.docker.stopped, env.podman.stopped), (["ollama"], ["comfy"]))
        self.assertEqual(sorted(r["runtime"] for r in env.state.stopped), ["docker", "podman"])
        env.trigger.launches = [make_launch(11)]
        env.advance(1)
        env.trigger.launches = []
        env.advance(env.config.exit_grace_seconds + 1)
        self.assertEqual((env.docker.started, env.podman.started), (["ollama"], ["comfy"]))

    def test_a_runtime_that_raises_is_skipped_for_that_pass(self):
        env = self.env(running=("ollama",), podman=("comfy",))
        env.podman.fail_scan = True
        env.trigger.launches = [make_launch(10)]
        out = env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])
        self.assertIn("podman", out)
        self.assertEqual(env.state.stopped, [ref("ollama")])

    def test_every_runtime_failing_does_nothing(self):
        env = self.env(running=("ollama",))
        env.docker.fail_scan = True
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual((env.trigger.ended, env.prompter.launch_asked), ([], []))

    def test_a_runtime_that_recovers_is_used_on_the_next_launch(self):
        env = self.env()
        env.docker.fail_scan = True
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.docker.fail_scan = False
        env.trigger.launches = [make_launch(11, 440)]
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])

    def test_a_ref_for_a_missing_runtime_is_logged_and_kept(self):
        env = self.env(running=())
        env.state.add([ref("x", "podman")], env.now - 3600)
        out = env.advance(1)
        self.assertIn("podman", out)
        self.assertEqual(env.state.stopped, [ref("x", "podman")])

    def test_a_failing_trigger_does_not_make_its_launches_look_new(self):
        env = self.env()
        env.trigger.launches = [make_launch(10)]
        env.guard = env.make_guard()
        env.trigger.fail_scan = True
        env.advance(1)
        env.trigger.fail_scan = False
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [])


class RunTests(GuardCase):
    def test_run_survives_errors_and_sleeps_the_poll_interval(self):
        env = self.env()
        env.config.poll_seconds = 0.25
        calls = []

        def step():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            if len(calls) == 3:
                raise KeyboardInterrupt

        env.guard.step = step
        sleeps = []
        with mock.patch("gpu_game_guard.guard.time.sleep", side_effect=sleeps.append):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    env.guard.run()
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.25, 0.25])

    def test_run_clamps_the_delay_and_survives_a_failing_sleep(self):
        env = self.env()
        env.guard.step = lambda: None
        for configured, expected in ((10 ** 400, 1.0), (0.0, 0.2), (math.nan, 1.0), (1e300, 60.0), (3600.0, 60.0)):
            env.config.poll_seconds = configured
            sleeps = []

            def sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) == 1:
                    raise OverflowError("too large")
                raise KeyboardInterrupt

            with mock.patch("gpu_game_guard.guard.time.sleep", side_effect=sleep):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(KeyboardInterrupt):
                        env.guard.run()
            self.assertEqual(sleeps, [expected, 1.0], configured)


class StateFailureTests(GuardCase):
    def test_a_failing_save_still_relaunches_and_keeps_the_state_in_memory(self):
        env = self.env()
        launch = make_launch(10)
        env.trigger.launches = [launch]
        with mock.patch.object(State, "save", side_effect=OSError("read-only")):
            out = env.advance(1)
        self.assertIn("could not save the state", out)
        self.assertEqual(env.docker.stopped, ["ollama"])
        self.assertEqual(env.state.stopped, [ref("ollama")])
        self.assertEqual(env.trigger.relaunched, [launch])
        self.assertTrue(env.guard.bypass)

    def test_a_failing_save_with_the_stop_action_still_notifies(self):
        env = self.env()
        env.config.on_launch = "stop"
        env.trigger.launches = [make_launch(10)]
        with mock.patch.object(State, "save", side_effect=OSError("full")):
            env.advance(1)
        self.assertEqual(len(env.trigger.relaunched), 1)
        self.assertEqual(len(env.prompter.notifications), 1)

    def test_a_failing_clear_after_the_restart_is_logged_and_forgotten(self):
        env = self.env(running=())
        env.state.add([ref("a")], env.now - 3600)
        with mock.patch.object(State, "save", side_effect=OSError("read-only")):
            out = env.advance(1)
        self.assertIn("could not clear", out)
        self.assertEqual(env.docker.started, ["a"])
        self.assertEqual(env.state.stopped, [])
        env.advance(1)
        self.assertEqual(env.prompter.restart_asked, [["a"]], "the restart is not offered again")

    def test_a_failing_save_of_game_seen_is_logged(self):
        env = self.env()
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.trigger.launches = [make_launch(11)]
        with mock.patch.object(State, "save", side_effect=OSError("full")):
            out = env.advance(1)
        self.assertIn("could not save", out)
        self.assertTrue(env.state.game_seen)


class AdoptionTests(GuardCase):
    def test_a_first_scan_that_fails_at_construction_still_adopts_running_games(self):
        env = self.env()
        env.trigger.launches = [make_launch(10)]
        env.trigger.fail_scan = True
        env.guard = env.make_guard()
        env.advance(1)
        env.trigger.fail_scan = False
        env.advance(1)
        self.assertEqual((env.prompter.launch_asked, env.trigger.ended), ([], []))
        env.trigger.launches = [make_launch(10), make_launch(11, 440)]
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])

    def test_each_trigger_is_adopted_separately(self):
        env = self.env()
        other = FakeTrigger("other")
        other.launches = [make_launch(20, 5, trigger="other")]
        other.fail_scan = True
        env.guard = Guard(env.watcher, [env.trigger, other], env.runtimes, env.prompter, env.state,
                          clock=lambda: env.now, wall=lambda: env.now, status_dir=env.status_dir)
        other.fail_scan = False
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [])


class GameSeenTests(GuardCase):
    def test_another_running_launch_does_not_count_as_the_relaunched_game(self):
        env = self.env()
        env.trigger.launches = [make_launch(5, 999)]
        env.guard = env.make_guard()  # an adopted launch is already running
        env.trigger.launches = [make_launch(5, 999), make_launch(10)]
        env.advance(1)
        self.assertEqual(env.state.stopped, [ref("ollama")])
        self.assertFalse(env.state.game_seen)
        env.trigger.launches = [make_launch(5, 999)]
        env.advance(1)
        self.assertFalse(env.state.game_seen)
        env.trigger.launches = [make_launch(5, 999), make_launch(11)]
        env.advance(1)
        self.assertTrue(env.state.game_seen)

    def test_an_ignored_launch_present_at_the_stop_does_not_count(self):
        env = self.env()
        env.config.games["steam:440"] = GameRule("TF2", "ignore")
        env.trigger.launches = [make_launch(6, 440)]
        env.advance(1)
        env.trigger.launches = [make_launch(6, 440), make_launch(10)]
        env.advance(1)
        self.assertFalse(env.state.game_seen)


class StopVerificationTests(GuardCase):
    def test_only_containers_that_stopped_are_recorded(self):
        env = self.env(running=("a", "b"))
        env.docker.refuse_stop = ["b"]
        env.trigger.launches = [make_launch(10)]
        out = env.advance(1)
        self.assertEqual(env.state.stopped, [ref("a")])
        self.assertIn("still running", out)
        self.assertEqual(len(env.trigger.relaunched), 1)

    def test_nothing_recorded_when_nothing_stopped(self):
        env = self.env(running=("a",))
        env.docker.refuse_stop = ["a"]
        env.config.on_launch = "stop"
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(env.state.stopped, [])
        self.assertFalse(env.state.path.exists())
        self.assertIn("Could not stop", env.prompter.notifications[0][1])


class HandlerTests(GuardCase):
    def test_end_raising_still_asks_and_relaunches(self):
        env = self.env()
        env.trigger.end = lambda launch: 1 / 0
        env.trigger.launches = [make_launch(10)]
        out = env.advance(1)
        self.assertIn("could not end", out)
        self.assertEqual(env.docker.stopped, ["ollama"])
        self.assertEqual(len(env.trigger.relaunched), 1)

    def test_relaunch_raising_notifies_the_user(self):
        env = self.env()
        env.trigger.relaunch = lambda launch: 1 / 0
        env.trigger.launches = [make_launch(10, title="Portal")]
        out = env.advance(1)
        self.assertIn("could not relaunch", out)
        self.assertEqual(len(env.prompter.notifications), 1)
        self.assertEqual(env.prompter.notifications[0][1], "Could not start Portal again.")

    def test_start_raising_is_logged_and_the_state_kept(self):
        env = self.env(running=())
        env.docker.start = lambda refs: 1 / 0
        env.state.add([ref("a")], env.now - 3600)
        out = env.advance(1)
        self.assertIn("start through docker failed", out)
        self.assertEqual(env.state.stopped, [ref("a")])

    def test_notify_raising_is_logged_and_does_not_stop_the_flow(self):
        env = self.env()
        env.config.on_launch = "stop"
        env.prompter.notify = lambda title, body: 1 / 0
        env.trigger.launches = [make_launch(10)]
        out = env.advance(1)
        self.assertIn("notification failed", out)
        self.assertEqual(len(env.trigger.relaunched), 1)
        self.assertEqual(env.state.stopped, [ref("ollama")])


class CanAskTests(GuardCase):
    def test_ask_without_a_dialog_leaves_the_launch_alone_and_logs_once(self):
        env = self.env(can_ask=False)
        env.trigger.launches = [make_launch(10)]
        out = env.advance(1)
        self.assertIn("cannot ask", out)
        self.assertEqual((env.trigger.ended, env.trigger.relaunched, env.prompter.launch_asked), ([], [], []))
        self.assertEqual((env.docker.stopped, env.state.stopped), ([], []))
        env.trigger.launches = [make_launch(10), make_launch(11)]
        self.assertNotIn("cannot ask", env.advance(1))
        env.trigger.launches = [make_launch(10), make_launch(11), make_launch(12, 440)]
        self.assertIn("steam:440", env.advance(1))

    def test_the_stop_action_works_without_a_dialog(self):
        env = self.env(can_ask=False)
        env.config.on_launch = "stop"
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(env.docker.stopped, ["ollama"])
        self.assertEqual(len(env.trigger.relaunched), 1)

    def test_a_per_game_stop_rule_works_without_a_dialog(self):
        env = self.env(can_ask=False)
        env.config.games["steam:730"] = GameRule("CS", "stop")
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(env.docker.stopped, ["ollama"])

    def test_after_game_ask_without_a_dialog_restarts(self):
        env = self.env(can_ask=False)
        env.config.on_launch = "stop"
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.trigger.launches = [make_launch(11)]
        env.advance(1)
        env.trigger.launches = []
        env.advance(env.config.exit_grace_seconds + 1)
        self.assertEqual(env.prompter.restart_asked, [])
        self.assertEqual(env.docker.started, ["ollama"])
        self.assertEqual(env.state.stopped, [])
        self.assertEqual(len(env.prompter.notifications), 2)


def container(name, runtime="docker"):
    return Target("container", runtime, name, "image:1", None, name)


def host(name, kind="service", runtime="service"):
    return Target(kind, runtime, name)


class PlanTests(unittest.TestCase):
    def test_ask_and_stop(self):
        targets = [container("a"), host("svc")]
        asked = plan(Config(), "steam:1", targets, True)
        self.assertEqual((asked.action, asked.will_stop, asked.ignored, asked.can_ask), ("ask", targets, [], True))
        stopped = plan(Config(on_launch="stop"), "steam:1", targets, False)
        self.assertEqual((stopped.action, stopped.will_stop, stopped.can_ask), ("stop", targets, False))

    def test_a_game_rule_overrides_on_launch(self):
        c = Config(on_launch="stop", games={"steam:1": GameRule("One", "ignore")})
        result = plan(c, "steam:1", [container("a")], True)
        self.assertEqual((result.action, result.will_stop), ("ignore", []))
        self.assertEqual(plan(c, "steam:2", [container("a")], True).action, "stop")

    def test_ignored_targets_are_listed_and_not_stopped(self):
        c = Config(ignore_containers=["plex*"])
        result = plan(c, "steam:1", [container("plex-1"), container("ollama")], True)
        self.assertEqual(([t.name for t in result.will_stop], [t.name for t in result.ignored]), (["ollama"], ["plex-1"]))

    def test_nothing_running(self):
        self.assertEqual(plan(Config(), "steam:1", [], True).action, "nothing-running")
        only_ignored = plan(Config(ignore_containers=["a"]), "steam:1", [container("a")], True)
        self.assertEqual((only_ignored.action, len(only_ignored.ignored)), ("nothing-running", 1))

    def test_paused_and_disabled_stop_nothing(self):
        targets = [container("a")]
        paused = plan(Config(), "steam:1", targets, True, paused=True)
        disabled = plan(Config(enabled=False), "steam:1", targets, True)
        self.assertEqual((paused.action, paused.will_stop, disabled.action, disabled.will_stop),
                         ("paused", [], "disabled", []))
        self.assertEqual(plan(Config(enabled=False), "steam:1", targets, True, paused=True).action, "disabled")

    def test_asking_without_a_dialog_stops_nothing(self):
        result = plan(Config(), "steam:1", [container("a")], False)
        self.assertEqual((result.action, result.will_stop, result.can_ask), ("ask", [], False))
        self.assertIn("no dialog", result.note)

    def test_auto_containers_off_leaves_containers_out(self):
        c = Config(auto_containers=False)
        result = plan(c, "steam:1", [container("a"), container("b", "podman"), host("svc")], True)
        self.assertEqual([t.name for t in result.will_stop], ["svc"])
        self.assertEqual(plan(c, "steam:1", [container("a")], True).action, "nothing-running")

    def test_plan_is_pure(self):
        targets = [container("a")]
        c = Config()
        before = c.to_json()
        self.assertIsInstance(plan(c, "steam:1", targets, True), Plan)
        self.assertEqual((c.to_json(), targets), (before, [container("a")]))


class WatchOnlyTests(GuardCase):
    def test_paused_adopts_launches_and_leaves_them_alone(self):
        env = self.env()
        status.pause(None, env.status_dir)
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual((env.prompter.launch_asked, env.trigger.ended, env.docker.stopped), ([], [], []))
        written = status.read_status(env.status_dir, max_age=1e12)
        self.assertEqual((written.state, written.paused), ("paused", True))
        status.resume(env.status_dir)
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [], "the adopted launch is not asked about after the resume")
        env.trigger.launches.append(make_launch(11, 440))
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])

    def test_a_timed_pause_ends(self):
        env = self.env()
        with mock.patch.object(status.time, "time", return_value=env.now):
            status.pause(30, env.status_dir)
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [])
        env.advance(60)
        env.trigger.launches.append(make_launch(11, 440))
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [["ollama"]])

    def test_disabled_config_adopts_and_reports_disabled(self):
        env = self.env(config=Config(enabled=False))
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual((env.prompter.launch_asked, env.trigger.ended), ([], []))
        self.assertEqual(status.read_status(env.status_dir, max_age=1e12).state, "disabled")

    def test_a_pending_restart_is_still_asked_while_paused(self):
        env = self.env()
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.trigger.launches = [make_launch(11, 440)]
        env.advance(1)
        status.pause(None, env.status_dir)
        env.trigger.launches = []
        env.advance(30)
        self.assertEqual(env.prompter.restart_asked, [["ollama"]])
        self.assertEqual(env.docker.started, ["ollama"])

    def test_auto_containers_false_skips_container_runtimes(self):
        env = self.env(config=Config(auto_containers=False), podman=["p"])
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(env.prompter.launch_asked, [])

    def test_auto_containers_false_keeps_host_runtimes(self):
        env = self.env(config=Config(auto_containers=False), running=())
        host_runtime = FakeRuntime("service", ())
        host_runtime.running = ["svc"]
        env.runtimes.append(host_runtime)
        env.guard = env.make_guard()
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(len(env.prompter.launch_asked), 1)


class RememberTests(GuardCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.json"

    def env_with(self, remember, config=None, **options):
        env = self.env(**options)
        env.prompter.remember = remember
        env.watcher.path = self.path
        if config is not None:
            env.watcher.config = config
        return env

    def test_remembered_stop_is_saved(self):
        env = self.env_with("stop")
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        saved = configmod.load(self.path)
        self.assertEqual(saved.games["steam:730"], GameRule("Game 730", "stop"))

    def test_remembered_ignore_is_saved_with_launch_anyway(self):
        env = self.env_with("ignore", launch="anyway")
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(configmod.load(self.path).games["steam:730"].action, "ignore")
        self.assertEqual(env.docker.stopped, [])

    def test_nothing_remembered_writes_nothing(self):
        env = self.env_with(None)
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertFalse(self.path.exists())

    def test_disabled_by_config(self):
        env = self.env_with("stop", config=Config(remember_choices=False))
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertFalse(self.path.exists())
        self.assertEqual(env.prompter.remember_asked, 0)

    def test_a_save_failure_is_logged_and_never_raised(self):
        env = self.env_with("stop")
        env.trigger.launches = [make_launch(10)]
        with mock.patch("gpu_game_guard.config.save", side_effect=OSError("disk full")):
            out = env.advance(1)
        self.assertIn("could not remember", out)
        self.assertEqual(env.docker.stopped, ["ollama"])

    def test_a_prompter_without_the_method_is_fine(self):
        env = self.env_with("stop")
        env.prompter = type("P", (), {"name": "p", "can_ask": True, "launch": lambda s, *a: "stop",
                                      "restart": lambda s, r: True, "notify": lambda s, *a: None})()
        env.guard = env.make_guard()
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertFalse(self.path.exists())

    def test_the_automatic_stop_action_does_not_ask_to_remember(self):
        env = self.env_with("stop", config=Config(on_launch="stop"))
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertFalse(self.path.exists())


class StatusChannelTests(GuardCase):
    def read(self, env):
        return status.read_status(env.status_dir, max_age=1e12)

    def test_status_after_a_pass(self):
        env = self.env(config=Config(targets=[configmod.TargetSpec("o", "process", match="o")]))
        env.advance(1)
        got = self.read(env)
        self.assertEqual((got.state, got.pid > 0, got.dialog, got.can_ask, got.targets, got.config_ok),
                         ("watching", True, "fake", True, 1, True))
        self.assertEqual(got.updated, env.now)

    def test_game_running_and_restart_pending(self):
        env = self.env()
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(self.read(env).state, "restart-pending", "the intercepted launch was ended")
        env.trigger.launches = [make_launch(11)]
        env.advance(1)
        got = self.read(env)
        self.assertEqual(got.state, "game-running")
        self.assertEqual(got.game, {"id": "steam:730", "title": "Game 730"})
        self.assertEqual([r["name"] for r in got.stopped], ["ollama"])
        env.trigger.launches = []
        env.prompter.restart_answer = None
        env.advance(2)
        env.trigger.launches = [make_launch(11, 440)]
        env.advance(1)
        env.trigger.launches = []
        env.advance(30)
        self.assertEqual(self.read(env).state, "restart-pending")

    def test_the_state_is_asking_while_the_dialog_is_open(self):
        env = self.env()
        seen = []
        original = env.prompter.launch

        def launch(*args):
            seen.append(self.read(env))
            return original(*args)
        env.prompter.launch = launch
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual((seen[0].state, seen[0].game["id"]), ("asking", "steam:730"))

    def test_events_for_the_whole_story(self):
        env = self.env()
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        env.trigger.launches = [make_launch(11, 440)]
        env.advance(1)
        env.trigger.launches = []
        env.advance(30)
        kinds = [e["kind"] for e in status.read_events(directory=env.status_dir)]
        self.assertEqual(kinds, ["intercept", "answer", "stop", "restart"])

    def test_failures_become_events(self):
        env = self.env(launch="stop")
        env.docker.refuse_stop = ["ollama"]
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertIn("failure", [e["kind"] for e in status.read_events(directory=env.status_dir)])

    def test_a_config_problem_is_reported_once_and_recovery_too(self):
        env = self.env()
        env.watcher.error = "poll_seconds is wrong"
        env.advance(1)
        env.advance(1)
        got = self.read(env)
        self.assertEqual((got.config_ok, got.config_error), (False, "poll_seconds is wrong"))
        env.watcher.error = ""
        env.advance(1)
        self.assertTrue(self.read(env).config_ok)
        messages = [e["message"] for e in status.read_events(directory=env.status_dir) if e["kind"] == "config"]
        self.assertEqual(len(messages), 2)

    def test_a_failing_status_write_is_logged_once_and_does_not_stop_the_guard(self):
        env = self.env()
        env.trigger.launches = [make_launch(10)]
        with mock.patch.object(status, "write_status", side_effect=OSError("read-only")), \
                mock.patch.object(status, "append_event", side_effect=OSError("read-only")):
            first = env.advance(1)
            second = env.advance(1)
        self.assertEqual(env.docker.stopped, ["ollama"])
        self.assertEqual(first.count("cannot write the status files"), 1)
        self.assertNotIn("cannot write the status files", second)

    def test_a_status_directory_that_is_a_file_does_not_stop_the_guard(self):
        env = self.env()
        env.status_dir.write_text("not a directory")
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(env.docker.stopped, ["ollama"])

    def test_cannot_ask_is_reported(self):
        env = self.env(can_ask=False)
        env.advance(1)
        self.assertFalse(self.read(env).can_ask)


def view(env):
    """The status as written, read as of the fake clock."""
    with mock.patch.object(status.time, "time", return_value=env.now):
        return status.read_status(env.status_dir)


class ConfigPreservationTests(RememberTests):
    def test_unknown_keys_survive_a_remembered_choice(self):
        self.path.write_text('{"future": {"a": 1}, "games": {"steam:9": {"action": "stop", "note": "n"}}}')
        env = self.env_with("stop")
        env.trigger.launches = [make_launch(10)]
        with contextlib.redirect_stderr(io.StringIO()):
            env.advance(1)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["future"], {"a": 1})
        self.assertEqual(data["games"]["steam:9"]["note"], "n")
        self.assertEqual(data["games"]["steam:730"]["action"], "stop")

    def test_a_settings_save_made_while_the_dialog_was_open_survives(self):
        env = self.env_with("stop")
        configmod.save(Config(heavy_vram_mib=777), self.path)
        original = env.prompter.launch

        def launch(*args):
            configmod.save(Config(heavy_vram_mib=777, dialog="zenity"), self.path)  # saved while the dialog is open
            return original(*args)

        env.prompter.launch = launch
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        saved = configmod.load(self.path)
        self.assertEqual((saved.heavy_vram_mib, saved.dialog, saved.games["steam:730"].action), (777, "zenity", "stop"))

    def test_an_invalid_file_on_disk_is_not_overwritten(self):
        self.path.write_text("{broken")
        env = self.env_with("stop")
        env.trigger.launches = [make_launch(10)]
        out = env.advance(1)
        self.assertEqual(self.path.read_text(), "{broken")
        self.assertIn("could not remember", out)
        self.assertEqual(env.docker.stopped, ["ollama"])
        self.assertEqual(list(self.path.parent.glob("*.broken-*")), [])


class FailedStartTests(GuardCase):
    def stopped_env(self, **options):
        env = self.env(running=(), **options)
        env.state.add([ref("a"), ref("b")], env.now - 3600)
        return env

    def test_failed_refs_are_kept_and_reported(self):
        env = self.stopped_env()
        env.docker.fail_start = ["b"]
        out = env.advance(1)
        self.assertEqual(env.docker.started, ["a"])
        self.assertEqual(env.state.stopped, [ref("b")])
        self.assertIn("could not start b", out)
        event = [e for e in status.read_events(directory=env.status_dir) if e["kind"] == "restart"][-1]
        self.assertEqual(event["message"], "started a again; could not start b")
        self.assertEqual(view(env).stopped, [ref("b")])
        self.assertEqual(State(env.state.path).stopped, [ref("b")])
        self.assertEqual(env.prompter.notifications[-1][1], "Started a again; could not start b.")

    def test_it_asks_again_after_the_retry_interval_and_clears_what_started(self):
        env = self.stopped_env()
        env.docker.fail_start = ["b"]
        env.advance(1)
        env.advance(10)
        self.assertEqual(env.prompter.restart_asked, [["a", "b"]])
        env.docker.fail_start = []
        env.advance(env.config.dialog_retry_seconds)
        self.assertEqual(env.prompter.restart_asked[-1], ["b"])
        self.assertEqual(env.docker.started, ["a", "b"])
        self.assertEqual(env.state.stopped, [])

    def test_with_after_game_restart_the_same_holds(self):
        env = self.stopped_env(config=Config(after_game="restart"))
        env.docker.fail_start = ["a", "b"]
        env.advance(1)
        self.assertEqual(len(env.state.stopped), 2)
        event = [e for e in status.read_events(directory=env.status_dir) if e["kind"] == "restart"][-1]
        self.assertEqual(event["message"], "could not start a, b")
        env.docker.fail_start = []
        env.advance(env.config.dialog_retry_seconds)
        self.assertEqual((env.state.stopped, env.docker.started), ([], ["a", "b"]))

    def test_a_ref_whose_provider_is_missing_counts_as_failed(self):
        env = self.env(running=())
        env.state.add([ref("a"), ref("x", "podman")], env.now - 3600)
        env.advance(1)
        self.assertEqual(env.state.stopped, [ref("x", "podman")])

    def test_a_provider_returning_none_means_nothing_failed(self):
        env = self.stopped_env()
        env.docker.start = lambda refs: None
        env.advance(1)
        self.assertEqual(env.state.stopped, [])

    def test_a_ref_of_a_disabled_or_removed_target_is_still_passed_to_its_provider(self):
        env = self.env(running=())
        service = FakeRuntime("service")
        env.guard.runtimes.append(service)
        gone = {"kind": "service", "runtime": "service", "name": "Old", "unit": "old.service", "scope": "user"}
        env.state.add([gone], env.now - 3600)
        env.advance(1)
        self.assertEqual(service.started, ["Old"])
        self.assertEqual(env.state.stopped, [])

    def test_two_targets_with_one_label_are_both_stopped_and_restarted(self):
        env = self.env(running=())
        runtime = FakeRuntime("service")
        runtime.running_gpu_targets = lambda: [
            Target("service", "service", "Same", extra={"id": i}) for i in ("a", "b") if i in runtime.running]
        runtime.running = ["a", "b"]
        runtime.stop = lambda refs: [runtime.running.remove(r["id"]) for r in refs]
        runtime.start = lambda refs: runtime.started.extend(r["id"] for r in refs)
        env.guard.runtimes.append(runtime)
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(sorted(r["id"] for r in env.state.stopped), ["a", "b"])
        env.trigger.launches = []
        env.advance(200)
        self.assertEqual(sorted(runtime.started), ["a", "b"])


class StatusWriteRateTests(GuardCase):
    def count_writes(self, env, steps, seconds):
        with mock.patch.object(status, "write_status", wraps=status.write_status) as write:
            for _ in range(steps):
                env.advance(seconds)
        return write.call_count

    def test_an_idle_guard_writes_at_most_twelve_times_a_minute(self):
        env = self.env(running=())
        env.advance(1)
        self.assertLessEqual(self.count_writes(env, 60, 1), 12)

    def test_an_idle_guard_still_writes_now_and_then(self):
        env = self.env(running=())
        env.advance(1)
        self.assertGreaterEqual(self.count_writes(env, 60, 1), 10)

    def test_a_change_is_written_at_once(self):
        env = self.env(running=())
        env.advance(1)
        with mock.patch.object(status, "write_status", wraps=status.write_status) as write:
            env.trigger.launches = [make_launch(10)]
            env.advance(0.2)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(view(env).game["id"], "steam:730")

    def test_valid_for_is_long_while_asking_and_while_a_restart_is_pending(self):
        env = self.env(launch="stop")
        seen = []
        original = env.prompter.launch

        def launch(*args):
            seen.append(view(env))
            return original(*args)

        env.prompter.launch = launch
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual((seen[0].state, seen[0].valid_for), ("asking", 900.0))
        after = view(env)
        self.assertEqual((after.state, after.valid_for), ("restart-pending", 900.0))
        env.trigger.launches = []
        env.state.clear()
        env.advance(10)
        self.assertEqual(view(env).valid_for, 15.0)

    def test_the_state_leaves_asking_after_the_dialog_returns(self):
        env = self.env(launch="cancel")
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertNotEqual(view(env).state, "asking")


if __name__ == "__main__":
    unittest.main()


class OutageAndFailedStopTests(GuardCase):
    def test_a_game_started_during_a_scan_outage_is_adopted_when_scanning_resumes(self):
        env = self.env()
        env.trigger.launches = []
        env.advance(1)
        env.trigger.fail_scan = True
        env.advance(1)
        env.trigger.launches = [make_launch(10)]  # started while the scan was failing
        env.trigger.fail_scan = False
        env.advance(1)
        self.assertEqual((env.prompter.launch_asked, env.trigger.ended), ([], []))
        env.trigger.launches = [make_launch(10), make_launch(11, 440)]
        env.advance(1)
        self.assertEqual(len(env.prompter.launch_asked), 1, "a launch after recovery is intercepted again")

    def test_a_failed_stop_after_a_dialog_answer_is_reported(self):
        env = self.env(launch="stop")
        env.docker.refuse_stop = ["ollama"]
        env.trigger.launches = [make_launch(10)]
        env.advance(1)
        self.assertEqual(env.state.stopped, [])
        self.assertTrue(any("Could not stop" in body for _title, body in env.prompter.notifications))
        self.assertEqual(env.trigger.relaunched, [make_launch(10)], "the game still starts")
