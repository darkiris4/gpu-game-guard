from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gpu_game_guard.ui import singleton


class SingletonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch("gpu_game_guard.ui.singleton.log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_acquire_is_refused_until_release(self):
        first = singleton.acquire("settings", self.dir)
        self.assertIsNotNone(first)
        self.assertTrue(first.held)
        self.assertIsNone(singleton.acquire("settings", self.dir))
        first.release()
        again = singleton.acquire("settings", self.dir)
        self.assertIsNotNone(again)
        again.release()
        first.release()  # releasing twice is harmless

    def test_names_are_independent(self):
        one, two = singleton.acquire("one", self.dir), singleton.acquire("two", self.dir)
        self.assertTrue(one and two)
        one.release()
        two.release()

    def test_lock_file_location_and_modes(self):
        lock = singleton.acquire("settings", self.dir)
        path = self.dir / "gpu-game-guard" / "settings.lock"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        lock.release()

    def test_another_process_is_refused_and_exit_frees_the_lock(self):
        lock = singleton.acquire("settings", self.dir)
        code = ("import sys; from pathlib import Path; from gpu_game_guard.ui import singleton; "
                "sys.exit(0 if singleton.acquire('settings', Path(sys.argv[1])) is None else 3)")
        root = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONPATH=root)
        self.assertEqual(subprocess.call([sys.executable, "-c", code, str(self.dir)], env=env), 0)
        lock.release()
        self.assertEqual(subprocess.call([sys.executable, "-c", code, str(self.dir)], env=env), 3)

    def test_symlinked_directory_is_not_followed(self):
        target = self.dir / "elsewhere"
        target.mkdir()
        (self.dir / "gpu-game-guard").symlink_to(target)
        lock = singleton.acquire("settings", self.dir)
        self.assertFalse(lock.held)  # unusable directory: let the caller through
        self.assertEqual(list(target.iterdir()), [])

    def test_invalid_name(self):
        for name in ("", "../x", "a/b", ".hidden"):
            with self.assertRaises(ValueError):
                singleton.acquire(name, self.dir)

    def test_runtime_base_needs_an_own_absolute_directory(self):
        self.assertEqual(singleton.runtime_base({"XDG_RUNTIME_DIR": str(self.dir)}), self.dir)
        self.assertIsNone(singleton.runtime_base({"XDG_RUNTIME_DIR": "relative"}))
        self.assertIsNone(singleton.runtime_base({}))
        self.assertIsNone(singleton.runtime_base({"XDG_RUNTIME_DIR": str(self.dir / "missing")}))

    def test_private_directory_fixes_modes_and_rejects_symlinks(self):
        loose = self.dir / "loose"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        made = singleton.private_directory(self.dir, "loose", "inner")
        self.assertEqual(stat.S_IMODE(made.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(loose.stat().st_mode), 0o700)
        (self.dir / "link").symlink_to(loose)
        with self.assertRaises(OSError):
            singleton.private_directory(self.dir, "link")

    def test_default_location_uses_the_runtime_directory(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.dir)}):
            lock = singleton.acquire("settings")
        self.assertTrue((self.dir / "gpu-game-guard" / "settings.lock").exists())
        lock.release()

    def test_fallback_is_a_private_directory_under_the_temp_directory(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("gpu_game_guard.ui.singleton.runtime_base", return_value=None), \
                mock.patch("gpu_game_guard.ui.singleton.tempfile.gettempdir", return_value=str(self.dir)):
            lock = singleton.acquire("settings")
        fallback = self.dir / f"gpu-game-guard-{os.getuid()}"
        self.assertEqual(stat.S_IMODE(fallback.stat().st_mode), 0o700)
        self.assertTrue(lock.held)
        lock.release()

    def test_fallback_refuses_a_symlink_planted_in_the_temp_directory(self):
        planted = self.dir / f"gpu-game-guard-{os.getuid()}"
        planted.symlink_to(self.dir)
        with mock.patch("gpu_game_guard.ui.singleton.runtime_base", return_value=None), \
                mock.patch("gpu_game_guard.ui.singleton.tempfile.gettempdir", return_value=str(self.dir)):
            lock = singleton.acquire("settings")
        self.assertFalse(lock.held)
        self.assertFalse((self.dir / "gpu-game-guard").exists())


if __name__ == "__main__":
    unittest.main()
