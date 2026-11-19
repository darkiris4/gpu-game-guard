"""The About dialog and the details text that goes into a bug report.

`about_text()` is plain text with the program version, the Python and Qt versions, the dialog backend that would be
chosen automatically and the kind of installation; it names no user, host or home directory. `show_about()` shows it
in a themed modal dialog next to the project address, with a button that copies the text to the clipboard.
"""
from __future__ import annotations

import platform
import sys
from typing import Optional

from .. import __version__
from . import theme
from .qt import APP_NAME, QtCore, QtWidgets

Qt = QtCore.Qt
SPACE = theme.SPACE
DESCRIPTION = "Asks before a game starts while GPU workloads are running."
LICENCE = "MIT licence"
PROJECT_URL = "https://github.com/darkiris4/gpu-game-guard"
SYSTEM_PREFIX = "/usr/"
LOGO_SIZE = 48  # in pixels at the base font; the dialog scales it with the font


def _install_kind() -> str:
    """"package" when the program or its modules live under /usr (the Debian package), otherwise "user install"."""
    paths = [sys.argv[0] if sys.argv else "", __file__]
    return "package" if any(str(path).startswith(SYSTEM_PREFIX) for path in paths) else "user install"


def _dialog_backend() -> str:
    """The name of the backend `dialogs.choose` picks for the automatic preference; "unknown" when that fails."""
    try:
        from .. import dialogs

        return str(dialogs.choose("auto").name) or "unknown"
    except Exception:  # the details text is informational and must never fail
        return "unknown"


def about_text() -> str:
    """Multi-line plain text describing this installation, suitable for pasting into a bug report."""
    return "\n".join([
        f"{APP_NAME} {__version__}",
        f"Python {platform.python_version()}",
        f"Qt {QtCore.QT_VERSION_STR} (PyQt {QtCore.PYQT_VERSION_STR})",
        f"Dialog backend: {_dialog_backend()}",
        f"Install: {_install_kind()}",
    ])


class AboutDialog(QtWidgets.QDialog):
    """A modal window with the name, version, description, licence and project address. Every label shows plain
    text. Escape and the Close button close it."""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setModal(True)
        self.setWindowIcon(theme.app_icon("ok"))
        scale = theme.font_scale()
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(3 * SPACE, 3 * SPACE, 3 * SPACE, 3 * SPACE)
        root.setSpacing(2 * SPACE)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(2 * SPACE)
        size = int(round(LOGO_SIZE * scale))
        self.logo = QtWidgets.QLabel()
        self.logo.setTextFormat(Qt.PlainText)
        self.logo.setPixmap(theme.app_icon("ok").pixmap(size, size))
        self.logo.setFixedSize(size, size)
        self.logo.setAccessibleName(f"{APP_NAME} logo")
        header.addWidget(self.logo, 0, Qt.AlignTop)
        titles = QtWidgets.QVBoxLayout()
        titles.setSpacing(2)
        self.name_label = theme.styled_label(APP_NAME, "headline", wrap=False)
        self.version_label = theme.styled_label(f"Version {__version__}", "muted", wrap=False)
        self.name_label.setAccessibleName(APP_NAME)
        self.version_label.setAccessibleName(f"Version {__version__}")
        titles.addWidget(self.name_label)
        titles.addWidget(self.version_label)
        header.addLayout(titles, 1)
        root.addLayout(header)

        self.description_label = theme.styled_label(DESCRIPTION)
        self.licence_label = theme.styled_label(LICENCE, "muted")
        self.url_label = theme.styled_label(PROJECT_URL, wrap=False)
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.url_label.setCursor(Qt.IBeamCursor)
        self.description_label.setAccessibleName("Description")
        self.licence_label.setAccessibleName("Licence")
        self.url_label.setAccessibleName("Project address")
        for label in (self.description_label, self.licence_label, self.url_label):
            root.addWidget(label)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(SPACE)
        row.addStretch(1)
        self.copy_button = theme.button("Copy details")
        self.copy_button.setAccessibleName("Copy details")
        self.copy_button.clicked.connect(self.copy_details)
        self.close_button = theme.button("Close", "primary")
        self.close_button.setAccessibleName("Close")
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        row.addWidget(self.copy_button)
        row.addWidget(self.close_button)
        root.addLayout(row)

    def copy_details(self) -> None:
        """Put `about_text()` on the clipboard."""
        QtWidgets.QApplication.clipboard().setText(about_text())


def show_about(parent: Optional[QtWidgets.QWidget] = None) -> None:
    """Show the About dialog and return when it is closed."""
    dialog = AboutDialog(parent)
    dialog.exec_()
    dialog.deleteLater()
