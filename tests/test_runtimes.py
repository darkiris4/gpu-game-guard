from __future__ import annotations

import json
import unittest
from unittest import mock

from gpu_game_guard import gpu, runtimes
from gpu_game_guard.model import Target

ID_A = "a5c7e80f694bd5b70957b072d117a7340b123c47a16ceadbc6b1c7868633bec2"
ID_B = "4f96b9e7ddac377fc068d30686c8f0e1d2264f2fed8afdc72b49a7ebdee5f18d"
ID_C = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

NVIDIA_REQUEST = {"HostConfig": {"Runtime": "runc", "DeviceRequests": [
    {"Driver": "nvidia", "Count": -1, "DeviceIDs": None, "Capabilities": [["gpu"]], "Options": {}}]}}
NVIDIA_RUNTIME = {"HostConfig": {"Runtime": "nvidia", "DeviceRequests": None}}
PLAIN = {"HostConfig": {"Runtime": "runc", "DeviceRequests": None}}
AMD = {"HostConfig": {"Runtime": "runc", "Devices": [
    {"PathOnHost": "/dev/kfd", "PathInContainer": "/dev/kfd", "CgroupPermissions": "rwm"},
    {"PathOnHost": "/dev/dri", "PathInContainer": "/dev/dri", "CgroupPermissions": "rwm"}]}}
DRI_DIRECTORY = {"HostConfig": {"Devices": [
    {"PathOnHost": "/dev/dri", "PathInContainer": "/dev/dri", "CgroupPermissions": "rwm"}]}}
DRI_NODE = {"HostConfig": {"Devices": [
    {"PathOnHost": "/dev/dri/renderD128", "PathInContainer": "/dev/dri/renderD128", "CgroupPermissions": "rwm"}]}}
NVIDIA_NODE = {"HostConfig": {"Devices": [
    {"PathOnHost": "/dev/nvidia0", "PathInContainer": "/dev/nvidia0", "CgroupPermissions": "rwm"}]}}
PODMAN_CDI = {"HostConfig": {"Runtime": "crun", "Devices": [
    {"PathOnHost": "nvidia.com/gpu=all", "PathInContainer": "", "CgroupPermissions": ""}]}}
DOCKER_CDI = {"HostConfig": {"DeviceRequests": [
    {"Driver": "cdi", "Count": 0, "DeviceIDs": ["nvidia.com/gpu=all"], "Capabilities": None, "Options": {}}]}}
ANNOTATED = {"Config": {"Annotations": {"cdi.k8s.io/devices": "amd.com/gpu=0"}}, "HostConfig": {}}
HOST_ANNOTATED = {"Config": {}, "HostConfig": {"Annotations": {"cdi.k8s.io/x": "vendor.com/GPU=1"}}}
PRIVILEGED = {"HostConfig": {"Privileged": True, "Runtime": "runc", "DeviceRequests": None, "Devices": []}}


class DetectionTests(unittest.TestCase):
    def test_detected(self):
        for label, info in [("device request", NVIDIA_REQUEST), ("nvidia runtime", NVIDIA_RUNTIME), ("amd devices", AMD),
                            ("render node", DRI_NODE), ("dri directory only", DRI_DIRECTORY), ("nvidia node", NVIDIA_NODE), ("podman cdi", PODMAN_CDI),
                            ("docker cdi", DOCKER_CDI), ("config annotation", ANNOTATED),
                            ("host annotation", HOST_ANNOTATED)]:
            with self.subTest(label):
                self.assertTrue(runtimes.is_gpu_container(info))

    def test_not_detected(self):
        for label, info in [("plain", PLAIN), ("empty", {}), ("none fields", {"HostConfig": None, "Config": None}),
                            ("other capability", {"HostConfig": {"DeviceRequests": [{"Capabilities": [["video"]]}]}}),
                            ("cdi without gpu", {"HostConfig": {"DeviceRequests": [{"Driver": "cdi", "DeviceIDs": ["vendor.com/net=0"]}]}}),
                            ("other device", {"HostConfig": {"Devices": [{"PathOnHost": "/dev/fuse"}]}}),
                            ("other annotation", {"Config": {"Annotations": {"io.kubernetes/x": "gpu"}}}),
                            ("not a dict", [])]:
            with self.subTest(label):
                self.assertFalse(runtimes.is_gpu_container(info))

    def test_default_nvidia_runtime_with_hidden_devices_is_not_detected(self):
        for value in ("void", "none", "VOID"):
            info = {"Config": {"Env": ["A=b", f"NVIDIA_VISIBLE_DEVICES={value}"]}, "HostConfig": {"Runtime": "nvidia"}}
            with self.subTest(value):
                self.assertFalse(runtimes.is_gpu_container(info))
        visible = {"Config": {"Env": ["NVIDIA_VISIBLE_DEVICES=all"]}, "HostConfig": {"Runtime": "nvidia"}}
        self.assertTrue(runtimes.is_gpu_container(visible))
        explicit = {"Config": {"Env": ["NVIDIA_VISIBLE_DEVICES=void"]}, "HostConfig": json.loads(json.dumps(
            NVIDIA_REQUEST["HostConfig"]))}
        self.assertTrue(runtimes.is_gpu_container(explicit), "an explicit device request still counts")

    def test_malformed_records_are_not_detected(self):
        bad = [5, "x", ["y"], None, True]
        cases = []
        for value in bad:
            cases += [{"HostConfig": value}, {"Config": value}, {"HostConfig": {"DeviceRequests": value}},
                      {"HostConfig": {"Devices": value}}, {"HostConfig": {"DeviceRequests": [value]}},
                      {"HostConfig": {"DeviceRequests": [{"Capabilities": value}]}},
                      {"HostConfig": {"DeviceRequests": [{"Driver": "cdi", "DeviceIDs": value}]}},
                      {"HostConfig": {"Devices": [value]}, "Config": {"Env": value}},
                      {"HostConfig": {"Runtime": "nvidia"}, "Config": {"Env": value}},
                      {"Config": {"Annotations": value}, "HostConfig": {"Annotations": value}}]
        cases.append({"HostConfig": {"DeviceRequests": [{"Capabilities": [5, "gpu", None]}]}})
        cases.append({"HostConfig": {"Devices": [{"PathOnHost": 5}]}})
        for info in cases:
            if info.get("HostConfig") == {"Runtime": "nvidia"}:
                continue
            with self.subTest(info=info):
                self.assertFalse(runtimes.is_gpu_container(info))

    def test_privileged_container_is_not_detected(self):
        # Privileged access names no device or request, so nothing in the record says GPU.
        self.assertFalse(runtimes.is_gpu_container(PRIVILEGED))


def record(container_id, name, image, base):
    info = json.loads(json.dumps(base))
    info.update({"Id": container_id, "Name": "/" + name})
    info.setdefault("Config", {})["Image"] = image
    return info


class FakeRunner:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def __call__(self, args, timeout=None, keep_output=False):
        self.calls.append(list(args))
        answer = self.answers.get(tuple(args))
        return answer(args) if callable(answer) else answer


class RuntimeTests(unittest.TestCase):
    def runtime(self, command="docker", answers=None, vram=None):
        runner = FakeRunner(answers)
        return runtimes.DockerLikeRuntime(command, vram or (lambda: {}), runner), runner

    def test_name_is_command(self):
        self.assertEqual(self.runtime("podman")[0].name, "podman")

    def test_no_containers(self):
        rt, runner = self.runtime(answers={("docker", "ps", "-q"): ""})
        self.assertEqual(rt.running_gpu_targets(), [])
        self.assertEqual(runner.calls, [["docker", "ps", "-q"]])

    def test_ps_failure(self):
        rt, _ = self.runtime()
        self.assertEqual(rt.running_gpu_targets(), [])

    def test_mixed_containers(self):
        records = [record(ID_A, "zeta", "z:1", NVIDIA_REQUEST), record(ID_B, "plain", "p:1", PLAIN),
                   record(ID_C, "alpha", "a:2", AMD)]
        rt, runner = self.runtime(answers={
            ("docker", "ps", "-q"): f"{ID_A[:12]}\n{ID_B[:12]}\n{ID_C[:12]}\n",
            ("docker", "inspect", ID_A[:12], ID_B[:12], ID_C[:12]): json.dumps(records)},
            vram=lambda: {ID_A: 2048})
        self.assertEqual(rt.running_gpu_targets(), [
            Target("container", "docker", "alpha", "a:2", 0, ID_C),
            Target("container", "docker", "zeta", "z:1", 2048, ID_A)])
        self.assertEqual(len([c for c in runner.calls if c[1] == "inspect"]), 1)

    def test_unmeasurable_vram_is_none_for_every_target(self):
        rt, _ = self.runtime(answers={
            ("docker", "ps", "-q"): "x\n",
            ("docker", "inspect", "x"): json.dumps([record(ID_A, "a", "i", NVIDIA_REQUEST)])}, vram=lambda: None)
        self.assertEqual([t.vram_mib for t in rt.running_gpu_targets()], [None])

    def test_inspect_keeps_output_when_a_container_vanished(self):
        calls = []

        def runner(args, timeout=None, keep_output=False):
            calls.append((list(args), keep_output))
            if args[1] == "ps":
                return "id1\nid2\n"
            return json.dumps([record(ID_A, "a", "i", NVIDIA_REQUEST)]) if keep_output else None

        rt = runtimes.DockerLikeRuntime("docker", lambda: {}, runner)
        self.assertEqual([t.name for t in rt.running_gpu_targets()], ["a"])
        self.assertTrue(calls[-1][1])

    def test_only_plain_containers_skips_vram(self):
        vram = mock.Mock(return_value={})
        rt, _ = self.runtime(answers={("docker", "ps", "-q"): "x\n",
                                      ("docker", "inspect", "x"): json.dumps([record(ID_B, "plain", "p", PLAIN)])}, vram=vram)
        self.assertEqual(rt.running_gpu_targets(), [])
        vram.assert_not_called()

    def test_bad_inspect_output(self):
        for output in ("not json", None, '{"a": 1}'):
            with self.subTest(output=output), mock.patch("gpu_game_guard.util.log"):
                rt, _ = self.runtime(answers={("docker", "ps", "-q"): "x\n", ("docker", "inspect", "x"): output})
                self.assertEqual(rt.running_gpu_targets(), [])

    def test_vram_failure_keeps_containers(self):
        def broken():
            raise RuntimeError("boom")
        rt, _ = self.runtime(answers={("podman", "ps", "-q"): "x\n",
                                      ("podman", "inspect", "x"): json.dumps([record(ID_A, "a", "i", PODMAN_CDI)])},
                             command="podman", vram=broken)
        with mock.patch("gpu_game_guard.util.log") as log:
            targets = rt.running_gpu_targets()
        self.assertEqual([(t.name, t.runtime, t.vram_mib) for t in targets], [("a", "podman", None)])
        self.assertEqual(log.call_count, 1)

    def test_stop_only_own_refs(self):
        rt, runner = self.runtime("podman")
        rt.stop([{"kind": "container", "runtime": "podman", "name": "a"},
                 {"kind": "container", "runtime": "docker", "name": "b"},
                 {"kind": "other", "runtime": "podman", "name": "c"},
                 {"kind": "container", "runtime": "podman", "name": "d"}])
        self.assertEqual(sorted(runner.calls), [["podman", "stop", "--", "a"], ["podman", "stop", "--", "d"]])

    def test_names_use_double_dash_and_dashed_names_are_skipped(self):
        rt, runner = self.runtime("docker")
        refs = [{"kind": "container", "runtime": "docker", "name": "-v"},
                {"kind": "container", "runtime": "docker", "name": "ok"},
                {"kind": "container", "runtime": "docker", "name": "--all"}]
        with mock.patch("gpu_game_guard.util.log"):
            rt.stop(refs)
            rt.start(refs)
        self.assertEqual(runner.calls, [["docker", "stop", "--", "ok"], ["docker", "start", "--", "ok"]])

    def test_one_bad_ref_does_not_abort_the_rest(self):
        def run(args, timeout=None, keep_output=False):
            if args[-1] == "bad":
                raise ValueError("boom")
            done.append(args[-1])
            return ""

        done = []
        rt = runtimes.DockerLikeRuntime("docker", lambda: {}, run)
        refs = [{"kind": "container", "runtime": "docker", "name": name} for name in ("bad", "a", "b")]
        with mock.patch("gpu_game_guard.util.log") as log:
            rt.stop(refs)
            rt.start(refs)
        self.assertEqual(sorted(done), ["a", "a", "b", "b"])
        self.assertEqual(log.call_count, 2)

    def test_nul_in_a_name_does_not_raise(self):
        gpu._last_failure.clear()
        rt = runtimes.DockerLikeRuntime("docker", lambda: {})
        refs = [{"kind": "container", "runtime": "docker", "name": "a\x00b"}]
        with mock.patch("gpu_game_guard.util.log"), mock.patch("subprocess.run", side_effect=ValueError("embedded null byte")):
            rt.stop(refs)
            rt.start(refs)

    def test_start_only_own_refs_in_order(self):
        rt, runner = self.runtime("docker")
        rt.start([{"kind": "container", "runtime": "docker", "name": "a"},
                  {"kind": "container", "runtime": "podman", "name": "b"},
                  {"kind": "container", "runtime": "docker", "name": "c"}])
        self.assertEqual(runner.calls, [["docker", "start", "--", "a"], ["docker", "start", "--", "c"]])

    def test_nothing_to_do(self):
        rt, runner = self.runtime()
        rt.stop([])
        rt.start([{"kind": "container", "runtime": "podman", "name": "b"}])
        self.assertEqual(runner.calls, [])

    def test_failures_logged_not_raised(self):
        # The default runner reports a missing binary as a failure and logs it once, not on every call.
        gpu._last_failure.clear()
        rt = runtimes.DockerLikeRuntime("no-such-container-runtime-xyz")
        ref = {"kind": "container", "runtime": "no-such-container-runtime-xyz", "name": "a"}
        with mock.patch("gpu_game_guard.util.log") as log:
            rt.stop([ref])
            rt.start([ref])
            self.assertEqual(rt.running_gpu_targets(), [])
            self.assertEqual(log.call_count, 3, "one line per subcommand")
            rt.stop([ref])
            rt.start([ref])
            self.assertEqual(rt.running_gpu_targets(), [])
        self.assertEqual(log.call_count, 3)

    def test_installed_follows_the_command_on_path(self):
        with mock.patch("shutil.which", side_effect=lambda c: "/usr/bin/docker" if c == "docker" else None):
            self.assertTrue(runtimes.DockerLikeRuntime("docker").installed())
            self.assertFalse(runtimes.DockerLikeRuntime("podman").installed())

    def test_available(self):
        self.assertTrue(self.runtime(answers={("docker", "version"): "Client: x\n"})[0].available())
        self.assertFalse(self.runtime()[0].available())
        with mock.patch("gpu_game_guard.util.log"):
            self.assertFalse(runtimes.DockerLikeRuntime("no-such-container-runtime-xyz").available())


class BuildTests(unittest.TestCase):
    def test_known_and_unknown(self):
        with mock.patch("gpu_game_guard.util.log") as log:
            built = runtimes.build(["podman", "lxc", "docker"])
        self.assertEqual([r.name for r in built], ["podman", "docker"])
        self.assertEqual(log.call_count, 1)

    def test_empty(self):
        self.assertEqual(runtimes.build([]), [])


if __name__ == "__main__":
    unittest.main()
