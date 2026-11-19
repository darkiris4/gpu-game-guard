from __future__ import annotations

import os
import platform
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from gpu_game_guard import __version__  # noqa: E402
from gpu_game_guard.ui import qt  # noqa: E402

if qt.available():
    from gpu_game_guard.ui import about, theme

    QtCore, QtGui, QtWidgets = qt.QtCore, qt.QtGui, qt.QtWidgets


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class AboutTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()

    def test_names_the_version_and_the_runtimes(self):
        text = about.about_text()
        lines = text.splitlines()
        self.assertEqual(lines[0], f"GPU Game Guard {__version__}")
        self.assertIn(f"Python {platform.python_version()}", lines)
        self.assertTrue(any(line.startswith("Qt ") and QtCore.QT_VERSION_STR in line
                            and QtCore.PYQT_VERSION_STR in line for line in lines))
        self.assertTrue(any(line.startswith("Dialog backend: ") for line in lines))
        self.assertTrue(any(line.startswith("Install: ") for line in lines))

    def test_names_no_user_host_or_home_directory(self):
        text = about.about_text()
        for private in (str(Path.home()), socket.gethostname(), os.environ.get("USER", ""),
                        os.environ.get("LOGNAME", "")):
            if private and private != "/":
                self.assertNotIn(private, text)

    def test_the_dialog_backend_is_the_one_choose_picks(self):
        with mock.patch("gpu_game_guard.dialogs.choose", return_value=mock.Mock()) as choose:
            choose.return_value.name = "zenity"
            self.assertIn("Dialog backend: zenity", about.about_text())
        choose.assert_called_once_with("auto")

    def test_a_failing_backend_lookup_does_not_break_the_text(self):
        with mock.patch("gpu_game_guard.dialogs.choose", side_effect=RuntimeError("boom")):
            self.assertIn("Dialog backend: unknown", about.about_text())

    def test_install_kind_follows_the_path(self):
        with mock.patch.object(about, "__file__", "/home/someone/.local/lib/gpu_game_guard/ui/about.py"):
            with mock.patch.object(sys, "argv", ["/home/someone/.local/bin/gpu-game-guard"]):
                self.assertEqual(about._install_kind(), "user install")
            with mock.patch.object(sys, "argv", ["/usr/bin/gpu-game-guard"]):
                self.assertEqual(about._install_kind(), "package")
            with mock.patch.object(sys, "argv", []):
                self.assertEqual(about._install_kind(), "user install")
        with mock.patch.object(about, "__file__", "/usr/lib/python3/dist-packages/gpu_game_guard/ui/about.py"), \
                mock.patch.object(sys, "argv", ["python3"]):
            self.assertEqual(about._install_kind(), "package")


@unittest.skipUnless(qt.available(), "PyQt5 is not installed")
class AboutDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt.app()

    def setUp(self):
        theme.apply_theme(self.app, dark=False)
        self.dialog = about.AboutDialog()
        self.addCleanup(self.dialog.deleteLater)

    def test_shows_name_version_description_licence_and_address(self):
        d = self.dialog
        self.assertEqual(d.name_label.text(), "GPU Game Guard")
        self.assertEqual(d.version_label.text(), f"Version {__version__}")
        self.assertEqual(d.description_label.text(), about.DESCRIPTION)
        self.assertEqual(d.licence_label.text(), "MIT licence")
        self.assertEqual(d.url_label.text(), "https://github.com/darkiris4/gpu-game-guard")
        self.assertTrue(d.url_label.textInteractionFlags() & QtCore.Qt.TextSelectableByMouse)
        self.assertTrue(d.isModal())

    def test_no_label_reads_its_text_as_markup(self):
        labels = self.dialog.findChildren(QtWidgets.QLabel)
        self.assertGreaterEqual(len(labels), 5)
        for label in labels:
            self.assertEqual(label.textFormat(), QtCore.Qt.PlainText, label.text())
            self.assertFalse(label.openExternalLinks())

    def test_buttons_and_accessible_names(self):
        d = self.dialog
        self.assertEqual([b.text() for b in d.findChildren(QtWidgets.QPushButton)], ["Copy details", "Close"])
        self.assertTrue(d.close_button.isDefault())
        for widget in (d.copy_button, d.close_button, d.url_label, d.version_label, d.description_label, d.logo):
            self.assertTrue(widget.accessibleName(), widget)

    def test_copy_details_puts_exactly_the_details_on_the_clipboard(self):
        clipboard = QtWidgets.QApplication.clipboard()
        clipboard.setText("before")
        self.dialog.copy_button.click()
        self.assertEqual(clipboard.text(), about.about_text())

    def test_close_and_escape_close_the_dialog(self):
        self.dialog.show()
        self.dialog.close_button.click()
        self.assertTrue(self.dialog.result() == QtWidgets.QDialog.Accepted)
        self.assertFalse(self.dialog.isVisible())
        self.dialog.show()
        for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
            QtWidgets.QApplication.sendEvent(self.dialog, QtGui.QKeyEvent(kind, QtCore.Qt.Key_Escape,
                                                                          QtCore.Qt.NoModifier))
        self.assertFalse(self.dialog.isVisible())

    def test_show_about_runs_the_dialog_modally_with_the_parent(self):
        parent = QtWidgets.QWidget()
        self.addCleanup(parent.deleteLater)
        with mock.patch.object(about.AboutDialog, "exec_", return_value=0) as run:
            about.show_about(parent)
            about.show_about()
        self.assertEqual(run.call_count, 2)

    def test_scales_with_the_font(self):
        original = QtWidgets.QApplication.font()
        self.addCleanup(QtWidgets.QApplication.setFont, original)
        self.addCleanup(theme.apply_theme, self.app, False)
        normal = self.dialog.sizeHint()
        large = QtGui.QFont(original)
        large.setPointSizeF(16)
        QtWidgets.QApplication.setFont(large)
        theme.apply_theme(self.app, dark=False)
        bigger = about.AboutDialog()
        self.addCleanup(bigger.deleteLater)
        self.assertGreater(bigger.sizeHint().width(), normal.width())
        self.assertGreater(bigger.sizeHint().height(), normal.height())
        self.assertGreater(bigger.logo.width(), self.dialog.logo.width())
        bigger.resize(bigger.sizeHint())
        bigger.layout().activate()
        for widget in bigger.findChildren(QtWidgets.QWidget):
            self.assertLessEqual(widget.geometry().right(), bigger.width(), widget)
            self.assertLessEqual(widget.geometry().bottom(), bigger.height(), widget)

if __name__ == "__main__":
    unittest.main()
