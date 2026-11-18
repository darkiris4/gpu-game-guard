"""The single doorway to PyQt5. Every module under `ui/` imports Qt from here, so the rest of the program runs
without PyQt5 installed and the import cost is paid only by processes that open a window.

`QtCore`, `QtGui` and `QtWidgets` are resolved on first access (module `__getattr__`), which keeps
`import gpu_game_guard.ui.qt` cheap inside the long-running watcher.
"""
from __future__ import annotations

import importlib
import importlib.util
from typing import Any, List, Optional

APP_NAME = "GPU Game Guard"
DESKTOP_FILE = "gpu-game-guard"
_MODULES = ("QtCore", "QtGui", "QtWidgets")

_available: Optional[bool] = None


def available() -> bool:
    """True when PyQt5's widget module can be imported on this system. The answer is computed once."""
    global _available
    if _available is None:
        try:
            _available = importlib.util.find_spec("PyQt5.QtWidgets") is not None
        except (ImportError, ValueError):
            _available = False
    return _available


def __getattr__(name: str) -> Any:
    if name in _MODULES:
        module = importlib.import_module(f"PyQt5.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def app(argv: Optional[List[str]] = None) -> Any:
    """The QApplication singleton, created on first use with a desktop-neutral application name. High-DPI scaling
    and high-resolution pixmaps are switched on before it is created, which is the only time Qt accepts them."""
    from PyQt5 import QtCore as core
    from PyQt5 import QtWidgets as widgets

    existing = widgets.QApplication.instance()
    if existing is not None:
        return existing
    for attribute in ("AA_EnableHighDpiScaling", "AA_UseHighDpiPixmaps"):  # only effective before the application exists
        if hasattr(core.Qt, attribute):
            core.QCoreApplication.setAttribute(getattr(core.Qt, attribute), True)
    instance = widgets.QApplication(list(argv) if argv else [APP_NAME])
    instance.setApplicationName(APP_NAME)
    instance.setApplicationDisplayName(APP_NAME)
    instance.setDesktopFileName(DESKTOP_FILE)
    instance.setQuitOnLastWindowClosed(True)
    return instance
