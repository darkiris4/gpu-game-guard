from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gpu_game_guard import polkit

ROOT = {"euid": 0}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.file = self.dir / polkit.RULE_NAME

    def grants(self):
        text = self.file.read_text()
        return json.loads(text.split("var grants = ", 1)[1].split(";\n", 1)[0])


class RenderTests(unittest.TestCase):
    def test_rule_text(self):
        text = polkit.render_rule(["b.service", "a.service", "a.service"], "alice")
        self.assertIn('var grants = {"alice": ["a.service", "b.service"]};', text)
        self.assertIn("org.freedesktop.systemd1.manage-units", text)
        self.assertIn('action.lookup("unit")', text)
        self.assertIn('action.lookup("verb")', text)
        self.assertIn('var verbs = ["start", "stop", "restart"];', text)
        self.assertIn("!subject.local || !subject.active", text)
        self.assertIn("subject.user", text)
        self.assertIn("polkit.Result.YES", text)
        self.assertIn("hasOwnProperty", text)

    def test_the_grants_live_in_a_function_not_a_global(self):
        lines = polkit.render_rule(["a.service"], "alice").splitlines()
        code = [line for line in lines if not line.startswith("//")]
        self.assertEqual(code[0], "(function() {")
        self.assertEqual(code[-1], "})();")
        self.assertFalse([line for line in code if line.startswith("var ")])

    def test_invalid_names_are_refused(self):
        for unit in ("a.timer", "a b.service", "../x.service", "a/b.service", "x.service; rm", '"q".service', "", ".service\n", "-x.service", "a*.service"):
            with self.subTest(unit):
                with self.assertRaises(polkit.PolkitError):
                    polkit.render_rule([unit], "alice")
        for user in ("", "a b", 'x"y', "1abc"):
            with self.subTest(user):
                with self.assertRaises(polkit.PolkitError):
                    polkit.render_rule(["a.service"], user)

    def test_valid_unit_names(self):
        for unit in ("ollama.service", "getty@tty1.service", "a:b_c-d.service"):
            polkit.render_rule([unit], "alice")


class FileTests(Fixture):
    def test_allow_writes_atomically_with_mode_and_merges(self):
        path = polkit.allow(["a.service"], "alice", self.dir, **ROOT)
        self.assertEqual(path, self.file)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o644)
        polkit.allow(["b.service", "a.service"], "alice", self.dir, **ROOT)
        polkit.allow(["c.service"], "sam", self.dir, **ROOT)
        self.assertEqual(self.grants(), {"alice": ["a.service", "b.service"], "sam": ["c.service"]})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [polkit.LOCK_NAME, polkit.RULE_NAME])

    def test_default_user_comes_from_sudo(self):
        polkit.allow(["a.service"], None, self.dir, environ={"SUDO_USER": "alice", "USER": "root"}, **ROOT)
        self.assertEqual(list(self.grants()), ["alice"])
        with self.assertRaises(polkit.PolkitError):
            polkit.allow(["a.service"], None, self.dir, environ={"USER": "root"}, **ROOT)

    def test_not_root_is_a_clear_error_and_writes_nothing(self):
        with self.assertRaises(polkit.PolkitError) as caught:
            polkit.allow(["a.service"], "alice", self.dir, euid=1000)
        self.assertIn("sudo", str(caught.exception))
        self.assertFalse(self.file.exists())
        with self.assertRaises(polkit.PolkitError):
            polkit.revoke(["a.service"], "alice", self.dir, euid=1000)

    def test_missing_directory(self):
        with self.assertRaises(polkit.PolkitError):
            polkit.allow(["a.service"], "alice", self.dir / "nope", **ROOT)

    def test_revoke_removes_units_and_deletes_the_file_when_none_remain(self):
        polkit.allow(["a.service", "b.service"], "alice", self.dir, **ROOT)
        polkit.allow(["c.service"], "sam", self.dir, **ROOT)
        polkit.revoke(["a.service", "zzz.service"], "alice", self.dir, **ROOT)
        self.assertEqual(self.grants(), {"alice": ["b.service"], "sam": ["c.service"]})
        polkit.revoke(["b.service"], "alice", self.dir, **ROOT)
        self.assertEqual(self.grants(), {"sam": ["c.service"]})
        polkit.revoke(["c.service"], "sam", self.dir, **ROOT)
        self.assertFalse(self.file.exists())
        polkit.revoke(["c.service"], "sam", self.dir, **ROOT)  # nothing to do is not an error

    def test_status(self):
        polkit.allow(["a.service"], "alice", self.dir, **ROOT)
        self.assertEqual(polkit.status(["a.service", "b.service"], "alice", self.dir), {"a.service": True, "b.service": False})
        self.assertEqual(polkit.status(["a.service"], "sam", self.dir), {"a.service": False})
        self.assertEqual(polkit.status(["a.service"], "alice", self.dir / "none"), {"a.service": False})

    def test_a_foreign_rule_file_is_not_overwritten(self):
        self.file.write_text("polkit.addRule(function() {});\n")
        with self.assertRaises(polkit.PolkitError):
            polkit.allow(["a.service"], "alice", self.dir, **ROOT)
        self.assertEqual(self.file.read_text(), "polkit.addRule(function() {});\n")

    def test_a_rule_file_in_the_old_layout_is_still_read(self):
        self.file.write_text('// old\nvar grants = {"alice": ["a.service"]};\npolkit.addRule(function() {});\n')
        polkit.allow(["b.service"], "alice", self.dir, **ROOT)
        self.assertEqual(self.grants(), {"alice": ["a.service", "b.service"]})

    def test_concurrent_allows_lose_no_update(self):
        units = [f"u{number}.service" for number in range(12)]
        errors = []

        def work(unit):
            try:
                polkit.allow([unit], "alice", self.dir, **ROOT)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(unit,)) for unit in units]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.grants(), {"alice": sorted(units)})

    def test_the_lock_is_taken_exclusively_around_the_change(self):
        with mock.patch("fcntl.flock") as flock:
            polkit.allow(["a.service"], "alice", self.dir, **ROOT)
            polkit.revoke(["a.service"], "alice", self.dir, **ROOT)
        self.assertEqual(flock.call_count, 2)
        self.assertTrue(all(call[0][1] == polkit.fcntl.LOCK_EX for call in flock.call_args_list))

    def test_a_user_that_does_not_exist_is_refused_for_a_real_run_only(self):
        with mock.patch("pwd.getpwnam", side_effect=KeyError("ghost")) as lookup, \
                mock.patch.object(polkit.Path, "is_dir", return_value=True), \
                mock.patch("gpu_game_guard.polkit._locked") as locked, \
                self.assertRaises(polkit.PolkitError) as caught:
            polkit.allow(["a.service"], "ghost", polkit.RULES_DIR, **ROOT)
        lookup.assert_called_once_with("ghost")
        locked.assert_not_called()
        self.assertIn("not a user", str(caught.exception))
        with mock.patch("pwd.getpwnam") as lookup:
            polkit.allow(["a.service"], "ghost", self.dir, **ROOT)
        lookup.assert_not_called()

    def test_revoke_in_a_missing_directory_is_a_no_op(self):
        polkit.revoke(["a.service"], "alice", self.dir / "none", **ROOT)

    def test_rendered_file_round_trips_through_the_parser(self):
        self.file.write_text(polkit.render_rule(["a.service"], "alice"))
        self.assertEqual(self.grants(), {"alice": ["a.service"]})

    def test_written_file_has_no_unexpected_world_write(self):
        polkit.allow(["a.service"], "alice", self.dir, **ROOT)
        self.assertFalse(self.file.stat().st_mode & stat.S_IWOTH)
        self.assertTrue(os.access(self.file, os.R_OK))


class MainTests(Fixture):
    def call(self, argv, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = polkit.main(argv, rules_dir=self.dir, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_allow_status_revoke(self):
        code, out, _ = self.call(["allow", "a.service", "--user", "alice"], **ROOT)
        self.assertEqual(code, 0)
        self.assertIn(str(self.file), out)
        code, out, _ = self.call(["status", "a.service", "b.service", "--user", "alice"])
        self.assertEqual((code, out), (0, "a.service: allowed\nb.service: not allowed\n"))
        code, out, _ = self.call(["revoke", "a.service", "--user", "alice"], **ROOT)
        self.assertEqual(code, 0)
        self.assertIn("Removed a.service for alice", out)
        self.assertFalse(self.file.exists())

    def test_revoke_says_nothing_was_granted_when_nothing_was_removed(self):
        code, out, _ = self.call(["revoke", "a.service", "--user", "alice"], **ROOT)
        self.assertEqual(code, 0)
        self.assertNotIn("Removed", out)
        self.assertIn("Nothing was granted", out)
        self.call(["allow", "b.service", "--user", "alice"], **ROOT)
        code, out, _ = self.call(["revoke", "a.service", "--user", "alice"], **ROOT)
        self.assertNotIn("Removed", out)
        self.assertIn("Nothing was granted", out)
        self.assertEqual(self.grants(), {"alice": ["b.service"]})

    def test_errors_and_usage(self):
        code, _, err = self.call(["allow", "a.service", "--user", "alice"], euid=1000)
        self.assertEqual(code, 1)
        self.assertIn("sudo", err)
        code, _, err = self.call(["allow", "bad", "--user", "alice"], **ROOT)
        self.assertEqual(code, 1)
        self.assertEqual(self.call([])[0], 2)
        self.assertEqual(self.call(["frobnicate", "a.service"])[0], 2)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RuleThroughNodeTests(unittest.TestCase):
    """Runs the generated rule in node against a stand-in for polkit's JavaScript environment."""

    HARNESS = """
const fs = require("fs");
const rules = [];
global.polkit = {Result: {YES: "YES", NOT_HANDLED: "NOT_HANDLED"}, addRule: function(fn) { rules.push(fn); }};
const cases = JSON.parse(process.argv[3]);
eval(fs.readFileSync(process.argv[2], "utf8"));
if (typeof grants !== "undefined") { console.log("LEAK"); }
const out = cases.map(function(c) {
    const action = {id: c.id, lookup: function(k) { return c.attrs[k]; }};
    return rules[0](action, {user: c.user, local: c.local, active: c.active});
});
console.log(JSON.stringify(out));
"""

    def test_only_the_named_user_and_unit_get_yes_for_start_stop_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            rule = Path(tmp) / "rule.js"
            rule.write_text(polkit.render_rule(["a.service"], "alice"))
            harness = Path(tmp) / "harness.js"
            harness.write_text(self.HARNESS)
            manage = polkit.ACTION
            cases, expected = [], []
            for user in ("alice", "bob", "constructor", "__proto__"):
                for unit in ("a.service", "b.service"):
                    for verb in ("start", "stop", "restart", "reload", "kill", "enable"):
                        for local, active in ((True, True), (False, True), (True, False)):
                            cases.append({"id": manage, "user": user, "local": local, "active": active,
                                          "attrs": {"unit": unit, "verb": verb}})
                            yes = user == "alice" and unit == "a.service" and verb in polkit.VERBS and local and active
                            expected.append("YES" if yes else "NOT_HANDLED")
            cases.append({"id": "org.freedesktop.login1.reboot", "user": "alice", "local": True, "active": True,
                          "attrs": {"unit": "a.service", "verb": "start"}})
            expected.append("NOT_HANDLED")
            done = subprocess.run(["node", str(harness), str(rule), json.dumps(cases)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1]), expected)
        self.assertNotIn("LEAK", done.stdout)


if __name__ == "__main__":
    unittest.main()
