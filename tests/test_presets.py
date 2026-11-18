from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from gpu_game_guard import gpu, presets

ID_RULE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
PRESET_DIR = Path(presets.__file__).parent


def by_id():
    return {p.id: p for p in presets.load_presets()}


def argv_ok(value):
    return isinstance(value, list) and value and all(isinstance(x, str) and x for x in value)


class ShippedTests(unittest.TestCase):
    def test_required_presets_exist_and_files_match_the_list(self):
        found = by_id()
        for name in ("ollama-system-service", "ollama-user-service", "ollama-unload"):
            self.assertIn(name, found)
        self.assertEqual(sorted(p.name for p in PRESET_DIR.glob("*.json")), sorted(presets.FILES))

    def test_unit_presets(self):
        found = by_id()
        self.assertEqual(found["ollama-system-service"].spec["unit"], "ollama.service")
        self.assertEqual(found["ollama-system-service"].spec["scope"], "system")
        self.assertEqual(found["ollama-user-service"].spec["scope"], "user")

    def test_every_spec_satisfies_the_target_schema(self):
        ids = set()
        for preset in presets.load_presets():
            spec = preset.spec
            self.assertRegex(spec["id"], ID_RULE)
            self.assertNotIn(spec["id"], ids)
            ids.add(spec["id"])
            self.assertIsInstance(spec.get("label", ""), str)
            if spec["kind"] == "service":
                self.assertTrue(spec["unit"].endswith(".service"))
                self.assertFalse(set(spec["unit"]) & {" ", "/"})
                self.assertIn(spec.get("scope", "system"), ("system", "user"))
            elif spec["kind"] == "command":
                self.assertTrue(argv_ok(spec["stop"]) and argv_ok(spec["start"]))
                self.assertTrue("check" not in spec or argv_ok(spec["check"]))
                self.assertTrue(1 <= spec.get("timeout_seconds", 60) <= 600)
            elif spec["kind"] == "process":
                self.assertTrue(spec["match"] and "/" not in spec["match"])
            else:
                self.fail(spec["kind"])

    def test_every_spec_is_accepted_by_the_real_config(self):
        try:
            from gpu_game_guard import config as config_module
            from gpu_game_guard.config import Config, TargetSpec  # noqa: F401
        except ImportError:
            self.skipTest("TargetSpec is not available")
        for preset in presets.load_presets():
            with self.subTest(preset.id):
                config = Config.from_json({"targets": [preset.spec]})
                self.assertEqual(config.targets[0].id, preset.spec["id"])
                self.assertEqual(config_module.validate({"targets": [preset.spec]}), [])

    def test_unload_command_shape(self):
        spec = by_id()["ollama-unload"].spec
        self.assertEqual(spec["kind"], "command")
        self.assertEqual(spec["start"], ["true"])
        self.assertEqual(spec["stop"][:2], ["python3", "-c"])
        self.assertEqual(spec["check"][:2], ["python3", "-c"])
        self.assertIn("keep_alive", spec["stop"][2])


class FakeOllama(BaseHTTPRequestHandler):
    models = []
    unloaded = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"models": [{"model": m, "name": m} for m in self.models]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.unloaded.append((self.path, payload))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")


class UnloadScriptTests(unittest.TestCase):
    def setUp(self):
        FakeOllama.models, FakeOllama.unloaded = [], []
        self.server = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"127.0.0.1:{self.server.server_address[1]}"

    def script(self, key, address=None):
        text = by_id()["ollama-unload"].spec[key][2]
        self.assertIn("127.0.0.1:11434", text)
        return text.replace("127.0.0.1:11434", address or self.address)

    def run_script(self, key, address=None):
        return subprocess.run([sys.executable, "-c", self.script(key, address)], capture_output=True, timeout=30).returncode

    def test_stop_unloads_every_model(self):
        FakeOllama.models = ["llama3:8b", "qwen2:7b"]
        self.assertEqual(self.run_script("stop"), 0)
        self.assertEqual(FakeOllama.unloaded, [("/api/generate", {"model": "llama3:8b", "keep_alive": 0}),
                                               ("/api/generate", {"model": "qwen2:7b", "keep_alive": 0})])

    def test_stop_exits_zero_when_ollama_is_not_running(self):
        self.assertEqual(self.run_script("stop", "127.0.0.1:1"), 0)

    def test_check_exits_zero_only_with_loaded_models(self):
        self.assertEqual(self.run_script("check"), 1)
        FakeOllama.models = ["m"]
        self.assertEqual(self.run_script("check"), 0)
        self.assertEqual(self.run_script("check", "127.0.0.1:1"), 1)


class LoadTests(unittest.TestCase):
    def test_malformed_files_are_skipped_and_logged(self):
        good = json.dumps({"id": "a", "label": "A", "description": "d",
                           "spec": {"id": "a", "kind": "process", "match": "a"}}).encode()
        files = {"good.json": good, "bad.json": b"{", "list.json": b"[]", "nospec.json": b'{"id":"x","label":"x","description":"d"}',
                 "binary.json": b"\xff\xfe"}

        def get_data(package, name):
            return files.get(name)

        with mock.patch("gpu_game_guard.util.log") as log:
            loaded = presets.load_presets(["bad.json", "good.json", "list.json", "nospec.json", "binary.json", "missing.json"], get_data)
        self.assertEqual([p.id for p in loaded], ["a"])
        self.assertEqual(log.call_count, 5)

    def test_loads_through_pkgutil(self):
        with mock.patch("pkgutil.get_data", wraps=__import__("pkgutil").get_data) as spy:
            presets.load_presets(["ollama-unload.json"], spy)
        self.assertEqual(spy.call_args[0][0], "gpu_game_guard.presets")


class ApplicableTests(unittest.TestCase):
    def test_unit_preset_uses_systemctl_cat(self):
        preset = by_id()["ollama-system-service"]
        calls = []

        def run(args, **_):
            calls.append(args)
            return gpu.CommandResult(0, "[Unit]", "")

        self.assertTrue(presets.applicable(preset, which=lambda n: "/bin/" + n, run=run))
        self.assertEqual(calls, [["systemctl", "cat", "ollama.service"]])
        self.assertFalse(presets.applicable(preset, which=lambda n: "/bin/" + n,
                                            run=lambda args, **_: gpu.CommandResult(1, "", "not found")))
        self.assertFalse(presets.applicable(preset, which=lambda n: "/bin/" + n, run=lambda args, **_: None))
        self.assertFalse(presets.applicable(preset, which=lambda n: None, run=run))

    def test_user_scope_and_command_detection(self):
        calls = []
        user = by_id()["ollama-user-service"]
        presets.applicable(user, which=lambda n: "/x", run=lambda args, **_: calls.append(args) or None)
        self.assertEqual(calls[0][:2], ["systemctl", "--user"])
        unload = by_id()["ollama-unload"]
        self.assertTrue(presets.applicable(unload, which=lambda n: "/usr/bin/ollama" if n == "ollama" else None))
        self.assertFalse(presets.applicable(unload, which=lambda n: None))


if __name__ == "__main__":
    unittest.main()
