from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

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
        self.assertIn("subject.user", text)
        self.assertIn("polkit.Result.YES", text)
        self.assertIn("hasOwnProperty", text)

    def test_invalid_names_are_refused(self):
        for unit in ("a.timer", "a b.service", "../x.service", "a/b.service", "x.service; rm", '"q".service', "", ".service\n"):
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
        self.assertEqual([p.name for p in self.dir.iterdir()], [polkit.RULE_NAME])

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
        self.assertIn("Removed", out)
        self.assertFalse(self.file.exists())

    def test_errors_and_usage(self):
        code, _, err = self.call(["allow", "a.service", "--user", "alice"], euid=1000)
        self.assertEqual(code, 1)
        self.assertIn("sudo", err)
        code, _, err = self.call(["allow", "bad", "--user", "alice"], **ROOT)
        self.assertEqual(code, 1)
        self.assertEqual(self.call([])[0], 2)
        self.assertEqual(self.call(["frobnicate", "a.service"])[0], 2)


if __name__ == "__main__":
    unittest.main()
