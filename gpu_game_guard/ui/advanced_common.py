"""Building blocks shared by the tabs of the Advanced settings view: labelled setting rows, a list editor for
glob lists, a table delegate that paints kind badges, scrolling pages, and the injectable dialog hooks."""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import html
import time
import unicodedata
from typing import Any, Callable, Iterator, List, Optional

from .. import config as config_module
from .qt import QtCore, QtGui, QtWidgets
from .theme import SPACE, Callout, button, current_colors, kind_color, plain_tooltip, safe_text, styled_label

Qt = QtCore.Qt

# The ranges the configuration accepts, read from the configuration module so that the editors never drift.
NUMBER_RANGES = config_module._NUMBERS
TIMEOUT_RANGE = config_module._TIMEOUT_RANGE

KIND_ROLE = int(Qt.UserRole) + 1  # item data holding a badge kind (`ok`, `warn`, `danger` or `muted`)
RISK_BADGE = {"low": "ok", "medium": "warn", "high": "danger"}
KIND_RISK = {"container": "low", "service": "low", "command": "medium", "process": "high"}
KIND_TITLE = {"container": "Container", "service": "Service", "command": "Command", "process": "Process"}
KIND_TIP = {
    "low": "Low risk: stopped and started again cleanly.",
    "medium": "Medium risk: runs the commands as written, so they decide what happens.",
    "high": "High risk: a program is asked to close, so unsaved work in it can be lost.",
}


def run_dialog(dialog: "QtWidgets.QDialog") -> bool:
    """Run a modal dialog and report whether it was accepted. Tabs call this through an attribute that tests replace."""
    return dialog.exec_() == QtWidgets.QDialog.Accepted


def confirm(parent: "QtWidgets.QWidget", text: str) -> bool:
    """Ask a yes or no question; tabs call this through an attribute that tests replace."""
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Question)
    box.setWindowTitle("GPU Game Guard")
    box.setTextFormat(Qt.PlainText)
    box.setText(safe_text(text, keep_newlines=True))
    box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel)
    box.setDefaultButton(QtWidgets.QMessageBox.Cancel)
    return box.exec_() == QtWidgets.QMessageBox.Yes


def pick_open_file(parent: "QtWidgets.QWidget") -> str:
    path, _filter = QtWidgets.QFileDialog.getOpenFileName(parent, "Import settings", "", "JSON (*.json);;All files (*)")
    return path


def pick_save_file(parent: "QtWidgets.QWidget", suggested: str) -> str:
    path, _filter = QtWidgets.QFileDialog.getSaveFileName(parent, "Export settings", suggested,
                                                          "JSON (*.json);;All files (*)")
    return path


@contextlib.contextmanager
def quiet(*widgets: "QtCore.QObject") -> Iterator[None]:
    """Block the signals of `widgets` while they are loaded from the model, so loading never writes back."""
    previous = [(w, w.blockSignals(True)) for w in widgets]
    try:
        yield
    finally:
        for widget, state in previous:
            widget.blockSignals(state)


def set_switch(switch: "QtWidgets.QAbstractButton", checked: bool) -> None:
    """Show a toggle switch as `checked` without emitting `toggled`, knob position included."""
    with quiet(switch):
        switch.setChecked(checked)
    switch.setProperty("offset", 1.0 if checked else 0.0)
    switch.update()


def mono_font() -> "QtGui.QFont":
    font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    if not QtGui.QFontInfo(font).fixedPitch():
        font = QtGui.QFont("monospace")
        font.setStyleHint(QtGui.QFont.Monospace)
    font.setPointSizeF(max(QtWidgets.QApplication.font().pointSizeF() - 0.5, 8.0))
    return font


def bold_label(text: str, tooltip: str = "") -> "QtWidgets.QLabel":
    """A semi-bold label that shows `text` literally."""
    label = QtWidgets.QLabel()
    label.setTextFormat(Qt.PlainText)
    label.setText(safe_text(text))
    font = label.font()
    font.setWeight(QtGui.QFont.DemiBold)
    label.setFont(font)
    if tooltip:
        plain_tooltip(label, tooltip)
    return label


def plain_tip(text: str) -> str:
    """Tooltip text for an item of a table or list that Qt shows literally: item tooltips are read as rich text
    when they look like markup, so the text is escaped and wrapped in `<qt>`."""
    text = safe_text(text, keep_newlines=True)
    return f"<qt>{html.escape(text, quote=False).replace(chr(10), '<br>')}</qt>" if text else ""


def divider() -> "QtWidgets.QFrame":
    line = QtWidgets.QFrame()
    line.setObjectName("Divider")
    line.setFrameShape(QtWidgets.QFrame.NoFrame)
    return line


def error_label() -> "QtWidgets.QLabel":
    """A hidden, word-wrapped message in the danger colour, for a field's validation problem."""
    label = QtWidgets.QLabel()
    label.setTextFormat(Qt.PlainText)
    label.setProperty("badge", "danger")
    label.setStyleSheet("padding: 0px; font-weight: normal;")
    label.setWordWrap(True)
    label.setVisible(False)
    return label


def setting_row(title: str, explanation: str, control: Optional["QtWidgets.QWidget"] = None, tooltip: str = "",
                wide: bool = False) -> "QtWidgets.QWidget":
    """A labelled setting: a title and a muted explanation line, with the control beside them or, when `wide`,
    underneath. The tooltip is set on every part so that hovering anywhere on the row explains it."""
    row = QtWidgets.QWidget()
    texts = QtWidgets.QVBoxLayout()
    texts.setSpacing(2)
    texts.setContentsMargins(0, 0, 0, 0)
    heading = bold_label(title)
    note = styled_label(explanation, "small")
    texts.addWidget(heading)
    texts.addWidget(note)
    if wide:
        layout: "QtWidgets.QLayout" = QtWidgets.QVBoxLayout(row)
        layout.setSpacing(SPACE)
        layout.addLayout(texts)
        if control is not None:
            layout.addWidget(control)
    else:
        layout = QtWidgets.QHBoxLayout(row)
        layout.setSpacing(2 * SPACE)
        layout.addLayout(texts, 1)
        if control is not None:
            layout.addWidget(control, 0, Qt.AlignVCenter | Qt.AlignRight)
    layout.setContentsMargins(0, 2, 0, 2)
    tip = tooltip or explanation
    for widget in (row, heading, note, control):
        if widget is not None and not widget.toolTip():
            widget.setToolTip(tip)
    return row


def centered(widget: "QtWidgets.QWidget") -> "QtWidgets.QWidget":
    """`widget` centred in a transparent holder, for a table cell."""
    holder = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget, 0, Qt.AlignCenter)
    return holder


def clear_layout(layout: "QtWidgets.QLayout") -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def mib_text(mib: Optional[int]) -> str:
    """GPU memory for a table cell: unknown, none, MiB or GiB."""
    if mib is None:
        return "unknown"
    if mib <= 0:
        return "none"
    return f"{mib / 1024:.1f} GiB" if mib >= 1024 else f"{mib} MiB"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def clock_text(when: float, now: Optional[float] = None) -> str:
    """A wall time as HH:MM:SS (24 hours, whatever the locale), with the date when it is more than twelve hours
    old. A value that is not a representable time gives "-"."""
    try:
        moment = time.localtime(when)
        reference = time.time() if now is None else now
    except (OverflowError, OSError, ValueError, TypeError):
        return "-"
    if moment.tm_year > 9999:
        return "-"
    if reference - when > 12 * 3600:
        return f"{_MONTHS[moment.tm_mon - 1]} {moment.tm_mday:02d} {moment.tm_hour:02d}:{moment.tm_min:02d}:{moment.tm_sec:02d}"
    return f"{moment.tm_hour:02d}:{moment.tm_min:02d}:{moment.tm_sec:02d}"


def managed_now(candidate: Any, config: "config_module.Config") -> bool:
    """Whether the guard would manage `candidate` under `config` as it is now, not as it was when found."""
    if candidate.kind == "container":
        return bool(config.auto_containers) and not config.is_ignored(candidate.name)
    scope = (candidate.spec or {}).get("scope", "system")
    for spec in config.targets:
        if not spec.enabled or spec.kind != candidate.kind:
            continue
        if candidate.kind == "service" and spec.unit == candidate.name and spec.scope == scope:
            return True
        if candidate.kind == "process" and fnmatch.fnmatchcase(candidate.name, spec.match):
            return True
    return False


def slug(text: str, taken: "set[str]") -> str:
    """A target id derived from a label, made unique against `taken`. Accented letters become their plain
    letters; a label with no usable letters at all gets an id made from a hash of it."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    base = "".join(c if c.isalnum() and c.isascii() else "-" for c in plain)
    base = "-".join(part for part in base.split("-") if part)[:40].strip("-_")
    if not base:
        base = "target-" + hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8] if text.strip() else "target"
    candidate, number = base, 1
    while candidate in taken:
        number += 1
        suffix = f"-{number}"
        candidate = base[:40 - len(suffix)] + suffix
    return candidate


def glob_problem(text: str) -> str:
    """Why `text` cannot be a name glob, or an empty string."""
    if not text.strip():
        return "Enter a name or a pattern such as backup-*."
    if text != text.strip():
        return "Remove the spaces around the pattern."
    return ""


def first_match(name: str, patterns: List[str]) -> str:
    """The first glob in `patterns` that matches `name` exactly (case-sensitive, as the guard does), or ''."""
    return next((p for p in patterns if fnmatch.fnmatchcase(name, p)), "")


class BadgeDelegate(QtWidgets.QStyledItemDelegate):
    """Paints an item's text as a tinted badge in the colour of the kind stored under `KIND_ROLE`."""

    def paint(self, painter: "QtGui.QPainter", option: "QtWidgets.QStyleOptionViewItem",
              index: "QtCore.QModelIndex") -> None:
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        if not text:
            return
        palette = current_colors()
        color = QtGui.QColor(kind_color(palette, str(index.data(KIND_ROLE) or "muted")))
        font = QtGui.QFont(opt.font)
        font.setPixelSize(12)
        font.setWeight(QtGui.QFont.DemiBold)
        metrics = QtGui.QFontMetrics(font)
        height = metrics.height() + 4
        width = min(metrics.horizontalAdvance(text) + 16, opt.rect.width() - 12)
        rect = QtCore.QRectF(opt.rect.left() + 8, opt.rect.center().y() - height / 2.0 + 0.5, width, height)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        tint = QtGui.QColor(color)
        tint.setAlphaF(0.16)
        painter.setPen(Qt.NoPen)
        painter.setBrush(tint)
        painter.drawRoundedRect(rect, 6, 6)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(rect, int(Qt.AlignCenter), metrics.elidedText(text, Qt.ElideRight, int(rect.width()) - 8))
        painter.restore()


def make_table(headers: List[str], row_height: int = 40) -> "QtWidgets.QTableWidget":
    """A read-only, row-selecting table in the house style."""
    table = QtWidgets.QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(row_height)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.ElideRight)
    table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
    table.setFocusPolicy(Qt.StrongFocus)
    return table


def table_item(text: str, tooltip: str = "", muted: bool = False, align: int = 0,
               kind: str = "") -> "QtWidgets.QTableWidgetItem":
    item = QtWidgets.QTableWidgetItem(safe_text(text))
    if tooltip:
        item.setToolTip(plain_tip(tooltip))
    if muted:
        item.setForeground(QtGui.QColor(current_colors().muted))
    if align:
        item.setTextAlignment(align | int(Qt.AlignVCenter))
    if kind:
        item.setData(KIND_ROLE, kind)
    return item


def focused_cell(table: "QtWidgets.QTableWidget") -> Optional[tuple]:
    """(row, column) of the cell whose widget holds the keyboard focus, or None."""
    widget = QtWidgets.QApplication.focusWidget()
    while widget is not None and widget is not table:
        if widget.parentWidget() is table.viewport():
            index = table.indexAt(widget.pos())
            return (index.row(), index.column()) if index.isValid() else None
        widget = widget.parentWidget()
    return None


def focus_cell(table: "QtWidgets.QTableWidget", row: int, column: int) -> None:
    """Give the keyboard focus to the widget in a cell, when it has one."""
    holder = table.cellWidget(row, column)
    if holder is None:
        return
    inner = holder if holder.focusPolicy() != Qt.NoFocus else holder.findChild(QtWidgets.QAbstractButton)
    if inner is not None:
        inner.setFocus()


class ScrollPage(QtWidgets.QScrollArea):
    """A vertically scrolling page whose content is `column`, a layout with the standard margins and spacing."""

    def __init__(self, parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content = QtWidgets.QWidget()
        self.column = QtWidgets.QVBoxLayout(self.content)
        self.column.setContentsMargins(2 * SPACE, 2 * SPACE, 2 * SPACE, 2 * SPACE)
        self.column.setSpacing(2 * SPACE)
        self.setWidget(self.content)

    def finish(self) -> None:
        self.column.addStretch(1)


class ListEditor(QtWidgets.QWidget):
    """An editable list of strings: the rows, a field with an Add button, and a Remove button for the selected row.
    `changed(list)` is emitted for every edit made by the user, never for `set_items`."""

    changed = QtCore.pyqtSignal(list)

    def __init__(self, placeholder: str = "", validator: Optional[Callable[[str], str]] = None,
                 parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(parent)
        self._validator = validator or glob_problem
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE)
        self.list = QtWidgets.QListWidget()
        self._fit()
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        entry = QtWidgets.QHBoxLayout()
        entry.setSpacing(SPACE)
        self.input = QtWidgets.QLineEdit()
        self.input.setPlaceholderText(placeholder)
        self.add_button = button("Add")
        self.remove_button = button("Remove")
        self.remove_button.setEnabled(False)
        entry.addWidget(self.input, 1)
        entry.addWidget(self.add_button)
        entry.addWidget(self.remove_button)
        self.error = error_label()
        layout.addWidget(self.list)
        layout.addLayout(entry)
        layout.addWidget(self.error)
        self.add_button.clicked.connect(self.add_current)
        self.input.returnPressed.connect(self.add_current)
        self.remove_button.clicked.connect(self.remove_selected)
        self.list.itemSelectionChanged.connect(lambda: self.remove_button.setEnabled(bool(self.list.selectedItems())))

    def _fit(self) -> None:
        """Size the list to its rows, between two and five rows tall."""
        rows = min(max(self.list.count(), 2), 5)
        self.list.setFixedHeight(rows * 30 + 6)

    def items(self) -> List[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_items(self, items: List[str]) -> None:
        if items == self.items():
            return
        with quiet(self.list):
            self.list.clear()
            self.list.addItems(items)
        self._fit()

    def add_current(self) -> None:
        text = self.input.text().strip()
        problem = self._validator(text)
        if not problem and text in self.items():
            problem = "That entry is already in the list."
        self.error.setText(problem)
        self.error.setVisible(bool(problem))
        if problem:
            return
        self.input.clear()
        self.list.addItem(text)
        self._fit()
        self.changed.emit(self.items())

    def remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._fit()
        self.error.setVisible(False)
        self.changed.emit(self.items())


class RestartNote(Callout):
    """A warning that the guard service must restart for a setting to apply, with a Restart service button. It is
    visible only while the model says a restart is needed."""

    def __init__(self, model: "QtCore.QObject", parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__("", "warn", parent)
        self.model = model
        self.label.setText("These settings are read when the service starts. Restart it to apply them.")
        self.button = button("Restart service", "primary")
        self.result = styled_label("", "small")
        self.result.setVisible(False)
        layout = self.layout()
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.button, 0, Qt.AlignLeft)
        row.addWidget(self.result, 1)
        layout.addLayout(row)
        self.button.clicked.connect(self.restart)
        self.refresh()

    def refresh(self) -> None:
        self.setVisible(bool(self.model.restart_needed()))

    def restart(self) -> None:
        ok, message = self.model.restart_service()
        self.result.setText(safe_text(message) or ("Restarted." if ok else "The restart failed."))
        self.result.setVisible(True)
        self.refresh()


class TabBase(QtWidgets.QWidget):
    """A tab of the Advanced view: it keeps the model, and reloads its controls whenever the model changes."""

    def __init__(self, model: "QtCore.QObject", parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(parent)
        self.model = model
        self.run_dialog = run_dialog
        self.confirm = confirm
        model.changed.connect(self.refresh)

    def refresh(self) -> None:
        """Load the controls from the model without writing anything back."""


class ScrolledTab(TabBase):
    """A tab whose content scrolls: cards are added to `column`, and `finish()` closes the page."""

    def __init__(self, model: "QtCore.QObject", parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(model, parent)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.page = ScrollPage()
        self.column = self.page.column
        outer.addWidget(self.page)

    def finish(self) -> None:
        self.page.finish()


class CandidateSource:
    """Hands the tabs the model's discovered candidates. Discovery can take a while, so it runs in the model's
    worker thread: while `deferred` the tabs see an empty list until `load()` runs (the view schedules it right
    after the window is built), and afterwards until the scan finishes."""

    def __init__(self, model: Any, deferred: bool = False):
        self.model = model
        self._deferred = deferred

    @property
    def loaded(self) -> bool:
        """True once a scan has delivered a result."""
        return bool(self.model.candidates_loaded)

    @property
    def scanning(self) -> bool:
        return bool(self.model.scanning)

    def get(self, refresh: bool = False) -> list:
        if refresh:
            self.model.scan(True)
        elif not self._deferred and not self.model.candidates_loaded:
            self.model.scan(False)
        return list(self.model.cached_candidates())

    def load(self) -> None:
        self._deferred = False
        self.model.scan(False)
