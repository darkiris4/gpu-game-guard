"""The Raw config tab: the whole configuration as JSON in an editor. Valid text becomes part of the working
settings as it is typed, so the window's Apply button saves it with every other change; text that cannot be used is
reported and blocks saving until it is fixed or reset."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from .. import config as config_module
from .advanced_common import TabBase, mono_font, pick_open_file, pick_save_file
from .qt import QtWidgets
from .theme import SPACE, Callout, button, plain_tooltip, safe_text, styled_label

DEFAULT_EXPORT_NAME = "gpu-game-guard-config.json"


def parse_report(text: str) -> Tuple[Optional[dict], List[str], List[str]]:
    """The decoded configuration in `text`, the problems that make it unusable and the warnings about keys this
    version does not know (kept unchanged when saving). The object is None when the text is unusable."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError, OverflowError) as exc:
        return None, [f"Not valid JSON: {safe_text(exc)}"], []
    try:
        problems = config_module._problems(data, strict=False)
        warnings = [p for p in config_module.validate(data) if p not in problems] if not problems else []
    except (RecursionError, OverflowError, ValueError, TypeError) as exc:
        return None, [f"The configuration could not be checked: {safe_text(exc) or type(exc).__name__}"], []
    return (data if isinstance(data, dict) and not problems else None), problems, warnings


def parse_problems(text: str) -> "Tuple[Optional[dict], List[str]]":
    """The decoded configuration in `text` and its problems; the object is None when the text is unusable."""
    data, problems, _warnings = parse_report(text)
    return data, problems


class RawTab(TabBase):
    def __init__(self, model: Any, parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(model, parent)
        self.pick_open: Callable[[QtWidgets.QWidget], str] = pick_open_file
        self.pick_save: Callable[[QtWidgets.QWidget, str], str] = pick_save_file
        self._loaded = ""  # the text the model corresponds to, as last loaded or pushed
        self._applying = False
        self._loading = False
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2 * SPACE, 2 * SPACE, 2 * SPACE, 2 * SPACE)
        layout.setSpacing(SPACE + 4)
        layout.addWidget(styled_label("The full configuration as JSON. Valid text becomes one of your unsaved changes "
                                      "at once; the Apply button of the window saves everything.", "muted"))
        self.editor = QtWidgets.QPlainTextEdit()
        self.editor.setFont(mono_font())
        self.editor.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.editor.setTabStopDistance(28)
        self.editor.setAccessibleName("Configuration as JSON")
        plain_tooltip(self.editor, "Edit the configuration directly. Only valid text changes the settings.")
        layout.addWidget(self.editor, 1)
        self.report = Callout("", "")
        self.report.setVisible(False)
        layout.addWidget(self.report)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(SPACE)
        self.validate_button = button("Check")
        self.reset_button = button("Reset")
        self.import_button = button("Import...")
        self.export_button = button("Export...")
        tips = {
            self.validate_button: "Check the text and report every problem. Nothing is saved.",
            self.reset_button: "Discard edits made here and show the current working settings.",
            self.import_button: "Load a JSON file into the editor.",
            self.export_button: "Write the text in the editor to a JSON file.",
        }
        for widget, tip in tips.items():
            plain_tooltip(widget, tip)
        row.addWidget(self.validate_button)
        row.addWidget(self.reset_button)
        row.addStretch(1)
        row.addWidget(self.import_button)
        row.addWidget(self.export_button)
        layout.addLayout(row)
        self.validate_button.clicked.connect(self.validate)
        self.reset_button.clicked.connect(self.reset)
        self.import_button.clicked.connect(self.import_file)
        self.export_button.clicked.connect(self.export_file)
        self.reset()
        self.editor.textChanged.connect(self._typed)
        if getattr(self.model, "load_error", ""):
            self._say("The settings file could not be loaded. Fix the problems below, then press Apply:\n"
                      + "\n".join(f"- {p}" for p in getattr(self.model, "load_problems", [])), "warn")

    # Text.
    def text(self) -> str:
        return self.editor.toPlainText()

    def set_text(self, text: str) -> None:
        self.editor.setPlainText(text)

    def edited(self) -> bool:
        """True when the editor holds text that differs from the working settings as last shown."""
        return self.text() != self._loaded

    def _model_text(self) -> str:
        if getattr(self.model, "load_error", ""):
            return getattr(self.model, "raw_file_text", "")
        return json.dumps(self.model.config.to_json(), indent=2) + "\n"

    def refresh(self) -> None:
        if self._applying or self._loading:
            return
        text = self._model_text()
        if getattr(self.model, "raw_problems", []):
            if text != self._loaded:
                self._say("The settings changed elsewhere. Reset loads the new values and discards these edits.", "warn")
            return
        if text != self._loaded:
            self._load(text)

    def _load(self, text: str) -> None:
        self._loaded = text
        if self.text() != text:
            self._loading = True
            try:
                self.editor.setPlainText(text)
            finally:
                self._loading = False
        self._hide_report()

    def reset(self) -> None:
        self.model.set_raw_problems([])
        self._load(self._model_text())

    def _typed(self) -> None:
        """Hand the editor's text to the model: valid text replaces the working settings, anything else is
        recorded as a problem that blocks saving."""
        if self._loading:
            return
        data, problems, warnings = parse_report(self.text())
        if data is None:
            self.model.set_raw_problems(problems or ["The top level must be a JSON object."])
            self._say("\n".join(f"- {p}" for p in self.model.raw_problems), "danger")
            return
        self._applying = True
        try:
            self.model.set_config(config_module.Config.from_json(data))
            self._loaded = self._model_text()
        except config_module.ConfigError as exc:
            self.model.set_raw_problems([exc.reason])
            self._say(f"- {exc.reason}", "danger")
            return
        finally:
            self._applying = False
        if warnings:
            self._say("Kept unchanged:\n" + "\n".join(f"- {w}" for w in warnings), "warn")
        else:
            self._hide_report()

    # Reports.
    def _say(self, text: str, kind: str) -> None:
        self.report.label.setText(safe_text(text, keep_newlines=True))
        self.report.setKind(kind)
        self.report.setVisible(True)

    def _hide_report(self) -> None:
        self.report.setVisible(False)

    def validate(self) -> List[str]:
        """Check the editor's text and show the result; returns the problems."""
        data, problems, warnings = parse_report(self.text())
        if data is None:
            problems = problems or ["The top level must be a JSON object."]
            self._say("\n".join(f"- {p}" for p in problems), "danger")
        elif warnings:
            self._say("The configuration is valid. Kept unchanged:\n" + "\n".join(f"- {w}" for w in warnings), "warn")
        else:
            self._say("The configuration is valid.", "")
        return problems

    # Files.
    def import_file(self) -> None:
        path = self.pick_open(self)
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self._say(f"Cannot read {path}: {getattr(exc, 'strerror', None) or exc}", "danger")
            return
        self.editor.setPlainText(text)
        problems = self.validate()
        if not problems:
            self._say(f"Loaded {Path(path).name}. It is valid and is now one of your unsaved changes.", "")

    def export_file(self) -> None:
        data, problems = parse_problems(self.text())
        if data is None or problems:
            self._say("Fix the problems before exporting:\n" + "\n".join(f"- {p}" for p in problems), "danger")
            return
        path = self.pick_save(self, DEFAULT_EXPORT_NAME)
        if not path:
            return
        try:
            Path(path).write_text(self.text(), encoding="utf-8")
        except OSError as exc:
            self._say(f"Cannot write {path}: {exc.strerror or exc}", "danger")
            return
        self._say(f"Exported to {Path(path).name}.", "")
