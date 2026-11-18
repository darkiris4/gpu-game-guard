"""Design system for every window of the program: colours that follow the system light or dark palette, one
stylesheet, and a few reusable widgets.

Layout rules the widgets and the stylesheet share: an 8 px spacing grid (`SPACE`), 12 px radius for cards and 8 px
for controls and buttons, one accent colour, the system font. Styling is addressed through properties so that plain
Qt widgets pick it up:

* `QPushButton` has the `role` property: `"secondary"` (default, outlined), `"primary"` or `"danger"`; use `button()`.
* `QLabel` has the `role` property: `"headline"`, `"title"`, `"muted"` or `"small"`; use `styled_label()`.
* `QFrame` with object name `Card` is a card; use `Card`.
* `QLabel` with the `badge` property (`ok`, `warn`, `danger`, `muted`) is a status label; use `Badge` or `Pill`.

`apply_theme(app)` installs palette and stylesheet and must run before windows are created. `demo_widget()` shows the
whole kit at once and is the reference for how the pieces fit together.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Callable, Dict, Optional

from .qt import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt

SPACE = 8  # the spacing grid, in pixels
RADIUS_CARD = 12
RADIUS_CONTROL = 8
ICON_SIZES = (16, 22, 24, 32, 48, 64, 128)
ICON_STATES = ("ok", "paused", "attention", "off")
BADGE_KINDS = ("ok", "warn", "danger", "muted")
BUTTON_ROLES = ("secondary", "primary", "danger")
LABEL_ROLES = ("headline", "title", "muted", "small")


@dataclass(frozen=True)
class Colors:
    """One palette, every value a `#rrggbb` string."""

    window: str  # window background
    card: str  # raised surfaces: cards, menus, buttons
    text: str
    muted: str  # secondary text
    accent: str
    accent_text: str  # text drawn on the accent colour
    border: str
    ok: str
    warn: str
    danger: str
    input: str  # background of text fields
    hover: str  # background of a hovered control


LIGHT = Colors(
    window="#f3f4f7", card="#ffffff", text="#1b2130", muted="#586274", accent="#2563eb", accent_text="#ffffff",
    border="#d5dae3", ok="#15803d", warn="#b45309", danger="#c62828", input="#ffffff", hover="#eef1f6",
)
DARK = Colors(
    window="#14171c", card="#1d2128", text="#e8ebf1", muted="#9ba4b4", accent="#7aa2ff", accent_text="#0b1220",
    border="#323946", ok="#4ade80", warn="#fbbf24", danger="#f87171", input="#171a20", hover="#272c35",
)

_current = LIGHT


def colors(dark: bool) -> Colors:
    return DARK if dark else LIGHT


def current_colors() -> Colors:
    """The palette installed by the last `apply_theme` (light before any)."""
    return _current


def is_dark(app: "QtWidgets.QApplication") -> bool:
    """True when the application's window colour is dark."""
    return app.palette().color(QtGui.QPalette.Window).lightness() < 128


def mix(first: str, second: str, amount: float) -> str:
    """The colour `amount` (0 to 1) of the way from `first` to `second`."""
    a, b = QtGui.QColor(first), QtGui.QColor(second)
    parts = [round(x + (y - x) * amount) for x, y in ((a.red(), b.red()), (a.green(), b.green()), (a.blue(), b.blue()))]
    return "#{:02x}{:02x}{:02x}".format(*parts)


def rgba(color: str, alpha: float) -> str:
    """A CSS `rgba()` value for a stylesheet."""
    c = QtGui.QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {int(round(alpha * 255))})"


def on_color(color: str) -> str:
    """Text colour (white or near black) that reads on a filled `color`."""
    return "#0b1220" if QtGui.QColor(color).lightness() > 140 else "#ffffff"


def kind_color(palette: Colors, kind: str) -> str:
    return {"ok": palette.ok, "warn": palette.warn, "danger": palette.danger}.get(kind, palette.muted)


_STYLE = Template("""
QWidget { color: $text; }
QDialog, QMainWindow, QWidget#Window { background: $window; }
QLabel { background: transparent; }
QLabel[role="headline"] { font-size: 20px; font-weight: 600; }
QLabel[role="title"] { font-size: 15px; font-weight: 600; }
QLabel[role="muted"] { color: $muted; }
QLabel[role="small"] { color: $muted; font-size: 12px; }
QToolTip { background: $card; color: $text; border: 1px solid $border; padding: 4px 8px; }

QFrame#Card { background: $card; border: 1px solid $border; border-radius: ${radius_card}px; }
QFrame#Callout { background: $hover; border: 1px solid $border; border-radius: ${radius_control}px; }
QFrame#Callout[kind="warn"] { background: $warn_soft; border: 1px solid $warn_line; }
QFrame#Callout[kind="danger"] { background: $danger_soft; border: 1px solid $danger_line; }
QFrame#Divider { background: $border; border: none; max-height: 1px; min-height: 1px; }

QLabel[badge] { padding: 1px 8px; font-size: 12px; font-weight: 600; }
QLabel[badge="muted"] { color: $muted; }
QLabel[badge="ok"] { color: $ok; }
QLabel[badge="warn"] { color: $warn; }
QLabel[badge="danger"] { color: $danger; }
QLabel[pill="true"] { padding: 1px 12px; }

QPushButton {
    background: $card; color: $text; border: 1px solid $border_strong; border-radius: ${radius_control}px;
    padding: 7px 16px; min-height: 20px;
}
QPushButton:hover { background: $hover; }
QPushButton:pressed { background: $border; }
QPushButton:focus { border: 2px solid $accent; padding: 6px 15px; }
QPushButton:disabled { color: $muted; background: $window; border: 1px solid $border; }
QPushButton[role="primary"] { background: $accent; color: $accent_text; border: 1px solid $accent; font-weight: 600; }
QPushButton[role="primary"]:hover { background: $accent_hover; border: 1px solid $accent_hover; }
QPushButton[role="primary"]:pressed { background: $accent_pressed; }
QPushButton[role="primary"]:focus { border: 2px solid $text; padding: 6px 15px; }
QPushButton[role="primary"]:disabled { background: $border; color: $muted; border: 1px solid $border; }
QPushButton[role="danger"] { background: $danger; color: $on_danger; border: 1px solid $danger; font-weight: 600; }
QPushButton[role="danger"]:hover { background: $danger_hover; border: 1px solid $danger_hover; }
QPushButton[role="danger"]:focus { border: 2px solid $text; padding: 6px 15px; }
QPushButton[flat="true"] { background: transparent; border: 1px solid transparent; }
QPushButton[flat="true"]:hover { background: $hover; }

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: $input; color: $text; border: 1px solid $border_strong; border-radius: ${radius_control}px;
    padding: 6px 10px; selection-background-color: $accent; selection-color: $accent_text;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 2px solid $accent; padding: 5px 9px;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled { color: $muted; background: $window; }
QComboBox { padding-right: 28px; }
QComboBox::drop-down { border: none; width: 28px; subcontrol-origin: padding; subcontrol-position: center right; }
$arrow
QComboBox QAbstractItemView {
    background: $card; border: 1px solid $border; selection-background-color: $accent_soft; selection-color: $text;
    outline: none; padding: 4px;
}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 18px; border: none; }

QCheckBox, QRadioButton { spacing: 8px; background: transparent; }
$indicators

QTabWidget::pane { border: none; border-top: 1px solid $border; top: -1px; }
QTabBar { background: transparent; }
QTabBar::tab {
    background: transparent; color: $muted; padding: 8px 16px; border: none; border-bottom: 2px solid transparent;
    margin-right: 4px;
}
QTabBar::tab:hover { color: $text; }
QTabBar::tab:selected { color: $accent; border-bottom: 2px solid $accent; }

QTableView, QTreeView, QListView {
    background: $card; alternate-background-color: $window; border: 1px solid $border;
    border-radius: ${radius_control}px; gridline-color: $border; outline: none;
    selection-background-color: $accent_soft; selection-color: $text;
}
QTableView::item, QTreeView::item, QListView::item { padding: 4px 8px; }
QHeaderView::section {
    background: $window; color: $muted; padding: 6px 10px; border: none; border-bottom: 1px solid $border;
    font-weight: 600;
}
QTableCornerButton::section { background: $window; border: none; }

QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 2px; }
QScrollBar::handle:vertical { background: $border_strong; border-radius: 4px; min-height: 32px; }
QScrollBar::handle:horizontal { background: $border_strong; border-radius: 4px; min-width: 32px; }
QScrollBar::handle:hover { background: $muted; }
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page { background: none; border: none; width: 0; height: 0; }

QMenu { background: $card; color: $text; border: 1px solid $border; border-radius: ${radius_control}px; padding: 4px; }
QMenu::item { padding: 6px 24px 6px 12px; border-radius: 6px; }
QMenu::item:selected { background: $accent_soft; color: $text; }
QMenu::item:disabled { color: $muted; }
QMenu::separator { height: 1px; background: $border; margin: 4px 8px; }

QProgressBar { background: $border; border: none; border-radius: 4px; max-height: 8px; text-align: center; }
QProgressBar::chunk { background: $accent; border-radius: 4px; }
""")


def stylesheet(palette: Colors, assets: Optional[Dict[str, str]] = None) -> str:
    """The application stylesheet for `palette`. `assets` maps `arrow` and `check` to image files used for the combo
    box arrow and the check box tick; without them those controls keep the style's own drawing."""
    p = palette
    assets = assets or {}
    arrow = (f"QComboBox::down-arrow {{ image: url({assets['arrow']}); width: 12px; height: 12px; }}"
             if "arrow" in assets else "")
    if "check" in assets:
        indicators = (
            f"QCheckBox::indicator {{ width: 18px; height: 18px; border: 1px solid {p.muted}; border-radius: 5px; "
            f"background: {p.input}; }}\n"
            f"QCheckBox::indicator:hover {{ border-color: {p.accent}; }}\n"
            f"QCheckBox::indicator:checked {{ background: {p.accent}; border: 1px solid {p.accent}; "
            f"image: url({assets['check']}); }}\n"
            f"QCheckBox::indicator:focus {{ border: 2px solid {p.accent}; }}\n"
            f"QRadioButton::indicator {{ width: 16px; height: 16px; border: 1px solid {p.muted}; "
            f"border-radius: 9px; background: {p.input}; }}\n"
            f"QRadioButton::indicator:checked {{ border: 5px solid {p.accent}; background: {p.input}; }}")
    else:
        indicators = ""
    return _STYLE.substitute(
        text=p.text, window=p.window, card=p.card, muted=p.muted, accent=p.accent, accent_text=p.accent_text,
        border=p.border, input=p.input, hover=p.hover, ok=p.ok, warn=p.warn, danger=p.danger,
        border_strong=mix(p.border, p.muted, 0.45),
        accent_hover=mix(p.accent, p.text, 0.12), accent_pressed=mix(p.accent, p.text, 0.25),
        accent_soft=rgba(p.accent, 0.20),
        danger_hover=mix(p.danger, p.text, 0.12), on_danger=on_color(p.danger),
        ok_soft=rgba(p.ok, 0.16), warn_soft=rgba(p.warn, 0.16), danger_soft=rgba(p.danger, 0.16),
        warn_line=rgba(p.warn, 0.55), danger_line=rgba(p.danger, 0.55),
        radius_card=RADIUS_CARD, radius_control=RADIUS_CONTROL, arrow=arrow, indicators=indicators,
    )


def _palette(p: Colors) -> "QtGui.QPalette":
    qp = QtGui.QPalette()
    roles = {
        QtGui.QPalette.Window: p.window, QtGui.QPalette.WindowText: p.text, QtGui.QPalette.Base: p.input,
        QtGui.QPalette.AlternateBase: p.window, QtGui.QPalette.Text: p.text, QtGui.QPalette.Button: p.card,
        QtGui.QPalette.ButtonText: p.text, QtGui.QPalette.Highlight: p.accent,
        QtGui.QPalette.HighlightedText: p.accent_text, QtGui.QPalette.ToolTipBase: p.card,
        QtGui.QPalette.ToolTipText: p.text, QtGui.QPalette.Link: p.accent, QtGui.QPalette.BrightText: p.text,
    }
    for role, value in roles.items():
        qp.setColor(role, QtGui.QColor(value))
    qp.setColor(QtGui.QPalette.PlaceholderText, QtGui.QColor(p.muted))
    for role in (QtGui.QPalette.WindowText, QtGui.QPalette.Text, QtGui.QPalette.ButtonText):
        qp.setColor(QtGui.QPalette.Disabled, role, QtGui.QColor(p.muted))
    return qp


def _glyph(path: Path, painter_fn: Callable[["QtGui.QPainter"], None], size: int = 36) -> None:
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter_fn(painter)
    painter.end()
    pixmap.save(str(path), "PNG")


def _write_assets(p: Colors) -> Dict[str, str]:
    """Paint the small images the stylesheet refers to into a per-palette directory under the temp directory."""
    key = hashlib.sha1(f"{p.muted}{p.accent_text}".encode()).hexdigest()[:10]
    base = Path(tempfile.gettempdir()) / f"gpu-game-guard-theme-{os.getuid() if hasattr(os, 'getuid') else 0}-{key}"
    try:
        base.mkdir(parents=True, exist_ok=True)

        def arrow(painter: "QtGui.QPainter") -> None:
            pen = QtGui.QPen(QtGui.QColor(p.muted), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(9, 13), QtCore.QPointF(18, 23),
                                                  QtCore.QPointF(27, 13)]))

        def tick(painter: "QtGui.QPainter") -> None:
            pen = QtGui.QPen(QtGui.QColor(p.accent_text), 4.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(8, 19), QtCore.QPointF(15, 26),
                                                  QtCore.QPointF(28, 11)]))

        _glyph(base / "arrow.png", arrow)
        _glyph(base / "check.png", tick)
        return {"arrow": (base / "arrow.png").as_posix(), "check": (base / "check.png").as_posix()}
    except OSError:
        return {}


def apply_theme(app: "QtWidgets.QApplication", dark: Optional[bool] = None) -> Colors:
    """Install the palette and stylesheet on `app`. `dark` forces a scheme; by default the system's is followed.
    Returns the palette in use (also available later through `current_colors()`)."""
    global _current
    use_dark = is_dark(app) if dark is None else dark
    palette = colors(use_dark)
    _current = palette
    app.setStyle("Fusion")
    app.setPalette(_palette(palette))
    app.setStyleSheet(stylesheet(palette, _write_assets(palette)))
    return palette


def _repolish(widget: "QtWidgets.QWidget") -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def button(text: str, role: str = "secondary", parent: Optional["QtWidgets.QWidget"] = None
           ) -> "QtWidgets.QPushButton":
    """A push button styled by role: `secondary` (outlined), `primary` (accent) or `danger` (destructive)."""
    widget = QtWidgets.QPushButton(text, parent)
    widget.setProperty("role", role)
    widget.setCursor(Qt.PointingHandCursor)
    return widget


def styled_label(text: str = "", role: str = "", parent: Optional["QtWidgets.QWidget"] = None,
                 wrap: bool = True) -> "QtWidgets.QLabel":
    """A label with a text role: `headline`, `title`, `muted` or `small`."""
    label = QtWidgets.QLabel(text, parent)
    if role:
        label.setProperty("role", role)
    label.setWordWrap(wrap)
    return label


class ElidedLabel(QtWidgets.QLabel):
    """A single-line label that shortens its text with an ellipsis instead of widening its window, and shows the
    full text as a tooltip while shortened."""

    def __init__(self, text: str = "", role: str = "", parent: Optional["QtWidgets.QWidget"] = None,
                 mode: "QtCore.Qt.TextElideMode" = Qt.ElideRight):
        super().__init__(text, parent)
        self._mode = mode
        if role:
            self.setProperty("role", role)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)

    def sizeHint(self) -> "QtCore.QSize":
        metrics = self.fontMetrics()
        return QtCore.QSize(metrics.horizontalAdvance(self.text()) + 2, metrics.height() + 2)

    def minimumSizeHint(self) -> "QtCore.QSize":
        return QtCore.QSize(24, self.fontMetrics().height() + 2)

    def paintEvent(self, event: "QtGui.QPaintEvent") -> None:
        painter = QtGui.QPainter(self)
        rect = self.contentsRect()
        elided = self.fontMetrics().elidedText(self.text(), self._mode, rect.width())
        self.setToolTip(self.text() if elided != self.text() else "")
        self.style().drawItemText(painter, rect, int(self.alignment()) | int(Qt.AlignVCenter), self.palette(),
                                  self.isEnabled(), elided, QtGui.QPalette.WindowText)


class Badge(QtWidgets.QLabel):
    """A small tinted label for a status or a kind of thing. `kind` is `ok`, `warn`, `danger` or `muted`."""

    def __init__(self, text: str = "", kind: str = "muted", parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(text, parent)
        self.setProperty("badge", "muted")
        self.setSizePolicy(QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.Maximum)
        self.setKind(kind)

    def kind(self) -> str:
        return str(self.property("badge"))

    def paintEvent(self, event: "QtGui.QPaintEvent") -> None:
        # The tinted background is painted here: a stylesheet radius on a label does not round reliably.
        p = current_colors()
        tint = QtGui.QColor(kind_color(p, self.kind()))
        tint.setAlphaF(0.16 if self.kind() != "muted" else 0.0)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QtGui.QColor(p.hover) if self.kind() == "muted" else tint)
        radius = self.height() / 2.0 if self.property("pill") == "true" else 6.0
        painter.drawRoundedRect(QtCore.QRectF(self.rect()), radius, radius)
        painter.end()
        super().paintEvent(event)

    def setKind(self, kind: str) -> None:
        self.setProperty("badge", kind if kind in BADGE_KINDS else "muted")
        _repolish(self)


class Pill(Badge):
    """A `Badge` with fully rounded ends, for a state shown on its own (Running, Paused)."""

    def __init__(self, text: str = "", kind: str = "muted", parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(text, kind, parent)
        self.setProperty("pill", "true")
        _repolish(self)


class Card(QtWidgets.QFrame):
    """A rounded, bordered container with an optional title and subtitle. Content goes into `body` (a
    `QVBoxLayout` with the standard 8 px spacing) through `addWidget` and `addLayout`."""

    def __init__(self, title: str = "", subtitle: str = "", parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(2 * SPACE, 2 * SPACE, 2 * SPACE, 2 * SPACE)
        outer.setSpacing(SPACE)
        self.title_label = styled_label(title, "title", self)
        self.subtitle_label = styled_label(subtitle, "muted", self)
        self.title_label.setVisible(bool(title))
        self.subtitle_label.setVisible(bool(subtitle))
        outer.addWidget(self.title_label)
        outer.addWidget(self.subtitle_label)
        self.body = QtWidgets.QVBoxLayout()
        self.body.setSpacing(SPACE)
        self.body.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self.body)

    def setTitle(self, title: str) -> None:
        self.title_label.setText(title)
        self.title_label.setVisible(bool(title))

    def addWidget(self, widget: "QtWidgets.QWidget", *args: object) -> None:
        self.body.addWidget(widget, *args)

    def addLayout(self, layout: "QtWidgets.QLayout") -> None:
        self.body.addLayout(layout)


class Callout(QtWidgets.QFrame):
    """A tinted note inside a window. `kind` is `""` (neutral), `warn` or `danger`."""

    def __init__(self, text: str = "", kind: str = "", parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(parent)
        self.setObjectName("Callout")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        self.label = styled_label(text, "", self)
        layout.addWidget(self.label)
        self.setKind(kind)

    def setKind(self, kind: str) -> None:
        self.setProperty("kind", kind)
        _repolish(self)


class SectionHeader(QtWidgets.QWidget):
    """A heading with an optional muted line under it, for the sections of a page."""

    def __init__(self, text: str = "", subtitle: str = "", parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, SPACE, 0, 0)
        layout.setSpacing(2)
        self.title_label = styled_label(text, "title", self)
        self.subtitle_label = styled_label(subtitle, "muted", self)
        self.subtitle_label.setVisible(bool(subtitle))
        layout.addWidget(self.title_label)
        layout.addWidget(self.subtitle_label)

    def setText(self, text: str) -> None:
        self.title_label.setText(text)

    def text(self) -> str:
        return self.title_label.text()

    def setSubtitle(self, subtitle: str) -> None:
        self.subtitle_label.setText(subtitle)
        self.subtitle_label.setVisible(bool(subtitle))


class ToggleSwitch(QtWidgets.QAbstractButton):
    """An on/off pill switch. It is a checkable button: `toggled(bool)`, `isChecked()` and `setChecked()` work as
    usual, Space toggles it while it has focus, and the knob slides when the switch is visible.

    `accessible_name` is what assistive technology reads, for example "Pause Ollama while gaming"."""

    WIDTH, HEIGHT, MARGIN = 44, 24, 3

    def __init__(self, checked: bool = False, parent: Optional["QtWidgets.QWidget"] = None,
                 accessible_name: str = ""):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        if accessible_name:
            self.setAccessibleName(accessible_name)
        self._offset = 1.0 if checked else 0.0
        self._animation = QtCore.QPropertyAnimation(self, b"offset", self)
        self._animation.setDuration(140)
        self._animation.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self.toggled.connect(self._slide)

    def sizeHint(self) -> "QtCore.QSize":
        return QtCore.QSize(self.WIDTH, self.HEIGHT)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = QtCore.pyqtProperty(float, _get_offset, _set_offset)

    def _slide(self, checked: bool) -> None:
        target = 1.0 if checked else 0.0
        self._animation.stop()
        if not self.isVisible():
            self._set_offset(target)
            return
        self._animation.setStartValue(self._offset)
        self._animation.setEndValue(target)
        self._animation.start()

    def paintEvent(self, event: "QtGui.QPaintEvent") -> None:
        p = current_colors()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        rect = QtCore.QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        track_off = QtGui.QColor(mix(p.border, p.muted, 0.55))
        track_on = QtGui.QColor(p.accent)
        track = QtGui.QColor(
            *[round(a + (b - a) * self._offset) for a, b in zip(track_off.getRgb()[:3], track_on.getRgb()[:3])])
        painter.setPen(Qt.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        diameter = rect.height() - 2 * self.MARGIN
        travel = rect.width() - diameter - 2 * self.MARGIN
        x = rect.left() + self.MARGIN + travel * self._offset
        painter.setBrush(QtGui.QColor("#ffffff"))
        painter.drawEllipse(QtCore.QRectF(x, rect.top() + self.MARGIN, diameter, diameter))
        if self.hasFocus():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QtGui.QPen(QtGui.QColor(p.accent), 2))
            painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), rect.height() / 2 + 1, rect.height() / 2 + 1)


_STATE_COLORS = {
    "ok": ("#4f8cff", "#2557d6"),
    "paused": ("#fbbf24", "#d97706"),
    "attention": ("#fb7185", "#dc2626"),
    "off": ("#9aa3b2", "#6b7483"),
}


def _shield_path() -> "QtGui.QPainterPath":
    path = QtGui.QPainterPath()
    path.moveTo(32, 2.5)
    path.cubicTo(24, 8.5, 15, 10.5, 7.5, 10.5)
    path.lineTo(7.5, 30)
    path.cubicTo(7.5, 45, 18, 56, 32, 61.5)
    path.cubicTo(46, 56, 56.5, 45, 56.5, 30)
    path.lineTo(56.5, 10.5)
    path.cubicTo(49, 10.5, 40, 8.5, 32, 2.5)
    path.closeSubpath()
    return path


def _controller_path() -> "QtGui.QPainterPath":
    path = QtGui.QPainterPath()
    path.addRoundedRect(QtCore.QRectF(14.5, 23.5, 35, 17), 8.5, 8.5)
    for cx in (21.5, 42.5):
        path.addEllipse(QtCore.QPointF(cx, 39.5), 6, 6)
    return path.simplified()


def _paint_icon(painter: "QtGui.QPainter", size: int, state: str) -> None:
    top, bottom = _STATE_COLORS.get(state, _STATE_COLORS["ok"])
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.scale(size / 64.0, size / 64.0)
    gradient = QtGui.QLinearGradient(0, 0, 0, 64)
    gradient.setColorAt(0, QtGui.QColor(top))
    gradient.setColorAt(1, QtGui.QColor(bottom))
    painter.setPen(Qt.NoPen)
    painter.setBrush(gradient)
    painter.drawPath(_shield_path())
    white = QtGui.QColor("#ffffff")
    painter.setBrush(white)
    if state in ("ok", "off"):
        painter.drawPath(_controller_path())
        painter.setBrush(QtGui.QColor(bottom))
        painter.drawRoundedRect(QtCore.QRectF(19.5, 30.6, 9, 3.2), 1.2, 1.2)  # d-pad
        painter.drawRoundedRect(QtCore.QRectF(22.4, 27.7, 3.2, 9, ), 1.2, 1.2)
        painter.drawEllipse(QtCore.QPointF(40, 30.8), 2.1, 2.1)
        painter.drawEllipse(QtCore.QPointF(44.5, 34.6), 2.1, 2.1)
    elif state == "paused":
        painter.drawRoundedRect(QtCore.QRectF(20.5, 21, 8, 24), 2.5, 2.5)
        painter.drawRoundedRect(QtCore.QRectF(35.5, 21, 8, 24), 2.5, 2.5)
    else:  # attention
        painter.drawRoundedRect(QtCore.QRectF(28.2, 19, 7.6, 21), 3.4, 3.4)
        painter.drawEllipse(QtCore.QPointF(32, 47.5), 4.2, 4.2)


def app_icon(state: str = "ok") -> "QtGui.QIcon":
    """The program icon, a shield with a game controller, painted in code. `state` colours it: `ok` (blue),
    `paused` (amber, pause bars), `attention` (red, exclamation mark) or `off` (grey). Every size in `ICON_SIZES`
    is painted natively, so small tray icons stay crisp."""
    icon = QtGui.QIcon()
    for size in ICON_SIZES:
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QtGui.QPainter(pixmap)
        _paint_icon(painter, size, state)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def render(widget: "QtWidgets.QWidget", path: str) -> bool:
    """Save a picture of `widget` as a PNG without showing it on screen. A widget that was never shown is sized
    to its layout first. Returns True when the file was written."""
    widget.ensurePolished()
    layout = widget.layout()
    if layout is not None:
        layout.activate()
    if widget.size().isEmpty() or not widget.isVisible():
        size = widget.sizeHint().expandedTo(widget.minimumSizeHint())
        if widget.width() > 0:
            size.setWidth(max(size.width(), widget.width()))
        widget.resize(size)
        if layout is not None:
            layout.activate()
            height = layout.hasHeightForWidth() and layout.heightForWidth(widget.width()) or 0
            if height > widget.height():
                widget.resize(widget.width(), height)
    return bool(widget.grab().save(path, "PNG"))


def demo_widget() -> "QtWidgets.QWidget":
    """Every component of the kit on one page, for screenshots and for checking a change to the theme."""
    page = QtWidgets.QWidget()
    page.setObjectName("Window")
    root = QtWidgets.QVBoxLayout(page)
    root.setContentsMargins(3 * SPACE, 3 * SPACE, 3 * SPACE, 3 * SPACE)
    root.setSpacing(2 * SPACE)

    header = QtWidgets.QHBoxLayout()
    logo = QtWidgets.QLabel()
    logo.setPixmap(app_icon("ok").pixmap(48, 48))
    titles = QtWidgets.QVBoxLayout()
    titles.setSpacing(2)
    titles.addWidget(styled_label("GPU Game Guard", "headline"))
    titles.addWidget(styled_label("Design kit: every component in one place.", "muted"))
    header.addWidget(logo)
    header.addSpacing(SPACE)
    header.addLayout(titles, 1)
    header.addWidget(Pill("Watching", "ok"))
    root.addLayout(header)

    icons = QtWidgets.QHBoxLayout()
    for state in ICON_STATES:
        for size in (16, 32):
            label = QtWidgets.QLabel()
            label.setPixmap(app_icon(state).pixmap(size, size))
            icons.addWidget(label)
        icons.addWidget(styled_label(state, "small", wrap=False))
        icons.addSpacing(SPACE)
    icons.addStretch(1)
    root.addLayout(icons)

    card = Card("Card with a title", "A muted line explains what the card is for.")
    row = QtWidgets.QHBoxLayout()
    row.addWidget(Badge("Container", "muted"))
    row.addWidget(Badge("Measured", "ok"))
    row.addWidget(Badge("Heavy", "warn"))
    row.addWidget(Badge("Failed", "danger"))
    row.addWidget(Pill("Paused", "warn"))
    row.addStretch(1)
    card.addLayout(row)
    switches = QtWidgets.QHBoxLayout()
    for checked in (True, False):
        switch = ToggleSwitch(checked, accessible_name="Example switch")
        switches.addWidget(switch)
        switches.addWidget(styled_label("On" if checked else "Off", wrap=False))
        switches.addSpacing(2 * SPACE)
    off_switch = ToggleSwitch(True, accessible_name="Disabled switch")
    off_switch.setEnabled(False)
    switches.addWidget(off_switch)
    switches.addWidget(styled_label("Disabled", "muted", wrap=False))
    switches.addStretch(1)
    card.addLayout(switches)
    card.addWidget(Callout("A neutral callout says one thing once."))
    card.addWidget(Callout("A warning callout is tinted amber.", "warn"))
    root.addWidget(card)

    root.addWidget(SectionHeader("Controls", "Inputs and buttons share one radius and one focus ring."))
    form = QtWidgets.QHBoxLayout()
    field = QtWidgets.QLineEdit("config.json")
    combo = QtWidgets.QComboBox()
    combo.addItems(["Ask me", "Stop automatically", "Do nothing"])
    spin = QtWidgets.QSpinBox()
    spin.setMaximum(100000)
    spin.setValue(4096)
    check = QtWidgets.QCheckBox("Remember my choice")
    check.setChecked(True)
    form.addWidget(field, 2)
    form.addWidget(combo, 2)
    form.addWidget(spin, 1)
    form.addWidget(check)
    root.addLayout(form)
    buttons = QtWidgets.QHBoxLayout()
    buttons.addWidget(button("Secondary"))
    buttons.addWidget(button("Primary", "primary"))
    buttons.addWidget(button("Remove", "danger"))
    disabled = button("Disabled")
    disabled.setEnabled(False)
    buttons.addWidget(disabled)
    buttons.addStretch(1)
    root.addLayout(buttons)

    tabs = QtWidgets.QTabWidget()
    table = QtWidgets.QTableWidget(3, 3)
    table.setHorizontalHeaderLabels(["Name", "Kind", "Memory"])
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    for r, values in enumerate([("ollama", "Container", "6.4 GiB"), ("obs", "Process", "unknown"),
                                ("jellyfin", "Service", "0 MiB")]):
        for c, value in enumerate(values):
            table.setItem(r, c, QtWidgets.QTableWidgetItem(value))
    table.selectRow(1)
    table.setMaximumHeight(130)
    tabs.addTab(table, "Targets")
    tabs.addTab(QtWidgets.QWidget(), "Games")
    tabs.addTab(QtWidgets.QWidget(), "Activity")
    root.addWidget(tabs)
    page.resize(640, page.sizeHint().height())
    return page
