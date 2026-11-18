"""The Raw config tab: the whole configuration as JSON in an editor, validated before it can replace the working
settings. Invalid text never reaches the model."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, List, Optional

from .. import config as config_module
from .advanced_common import TabBase, mono_font, pick_open_file, pick_save_file
from .qt import QtWidgets
from .theme import SPACE, Callout, button, styled_label

DEFAULT_EXPORT_NAME = "gpu-game-guard-config.json"


def parse_problems(text: str) -> "tuple[Optional[dict], List[str]]":
    """The decoded configuration in `text` and its problems; the object is None when the JSON is unusable."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, [f"Not valid JSON: {exc}"]
    problems = config_module.validate(data)
    return (data if isinstance(data, dict) else None), problems


class RawTab(TabBase):
    def __init__(self, model: Any, parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(model, parent)
        self.pick_open: Callable[[QtWidgets.QWidget], str] = pick_open_file
        self.pick_save: Callable[[QtWidgets.QWidget, str], str] = pick_save_file
        self._loaded = ""
        self._applying = False
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2 * SPACE, 2 * SPACE, 2 * SPACE, 2 * SPACE)
        layout.setSpacing(SPACE + 4)
        layout.addWidget(styled_label("The full configuration as JSON. Apply puts it into the working settings; the "
                                      "window's own Apply button saves them to disk.", "muted"))
        self.editor = QtWidgets.QPlainTextEdit()
        self.editor.setFont(mono_font())
        self.editor.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.editor.setTabStopDistance(28)
        self.editor.setToolTip("Edit the configuration directly. Nothing changes until Apply, and only valid text "
                               "is accepted.")
        layout.addWidget(self.editor, 1)
        self.report = Callout("", "")
        self.report.setVisible(False)
        layout.addWidget(self.report)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(SPACE)
        self.validate_button = button("Validate")
        self.apply_button = button("Apply", "primary")
        self.reset_button = button("Reset")
        self.import_button = button("Import...")
        self.export_button = button("Export...")
        tips = {
            self.validate_button: "Check the text without changing anything.",
            self.apply_button: "Replace the working settings with this text, when it is valid.",
            self.reset_button: "Discard edits and show the current working settings.",
            self.import_button: "Load a JSON file into the editor. Apply it afterwards.",
            self.export_button: "Write the text in the editor to a JSON file.",
        }
        for widget, tip in tips.items():
            widget.setToolTip(tip)
        row.addWidget(self.validate_button)
        row.addWidget(self.apply_button)
        row.addWidget(self.reset_button)
        row.addStretch(1)
        row.addWidget(self.import_button)
        row.addWidget(self.export_button)
        layout.addLayout(row)
        self.validate_button.clicked.connect(self.validate)
        self.apply_button.clicked.connect(self.apply)
        self.reset_button.clicked.connect(self.reset)
        self.import_button.clicked.connect(self.import_file)
        self.export_button.clicked.connect(self.export_file)
        self.reset()

    # Text.
    def text(self) -> str:
        return self.editor.toPlainText()

    def set_text(self, text: str) -> None:
        self.editor.setPlainText(text)

    def edited(self) -> bool:
        """True when the editor holds text the user changed since it was last loaded from the model."""
        return self.text() != self._loaded

    def _model_text(self) -> str:
        return json.dumps(self.model.config.to_json(), indent=2) + "\n"

    def refresh(self) -> None:
        if self._applying:
            return
        text = self._model_text()
        if self.edited():
            if text != self._loaded:
                self._say("The settings changed elsewhere. Reset loads the new values and discards these edits.", "warn")
            return
        self._load(text)

    def _load(self, text: str) -> None:
        self._loaded = text
        if self.text() != text:
            self.editor.setPlainText(text)
        self._hide_report()

    def reset(self) -> None:
        self._load(self._model_text())

    # Reports.
    def _say(self, text: str, kind: str) -> None:
        self.report.label.setText(text)
        self.report.setKind(kind)
        self.report.setVisible(True)

    def _hide_report(self) -> None:
        self.report.setVisible(False)

    def validate(self) -> List[str]:
        """Check the editor's text and show the result; returns the problems."""
        _data, problems = parse_problems(self.text())
        if problems:
            self._say("\n".join(f"- {p}" for p in problems), "danger")
        else:
            self._say("The configuration is valid.", "")
        return problems

    def apply(self) -> List[str]:
        """Put the editor's text into the model when it is valid; returns the problems otherwise."""
        data, problems = parse_problems(self.text())
        if problems or data is None:
            self._say("\n".join(f"- {p}" for p in problems) or "The top level must be a JSON object.", "danger")
            return problems or ["not an object"]
        self._applying = True
        try:
            self.model.set_config(config_module.Config.from_json(data))
        finally:
            self._applying = False
        self.reset()
        self._say("Applied to the working settings.", "")
        return []

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
            self._say(f"Loaded {Path(path).name}. It is valid; press Apply to use it.", "")

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
