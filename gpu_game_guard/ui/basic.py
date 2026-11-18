"""The Basic page of the settings window: plain-language choices for what happens when a game starts, which
containers and services are paused, and what happens afterwards. Every control writes straight to the shared
`SettingsModel`; saving is the window's job."""
from __future__ import annotations

import dataclasses
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..config import AFTER_GAME, ON_LAUNCH, Config, TargetSpec
from ..discovery import Candidate
from ..status import Status
from . import theme
from .qt import QtCore, QtGui, QtWidgets
from .settings_model import SettingsModel

Qt = QtCore.Qt
SPACE = theme.SPACE
CONTENT_WIDTH = 760

ON_LAUNCH_CHOICES = (
    ("ask", "Ask me", "A question appears before the game starts."),
    ("stop", "Stop them automatically", "Frees up your graphics card without asking."),
    ("ignore", "Do nothing", "Games start as usual and nothing is paused."),
)
AFTER_GAME_CHOICES = (
    ("ask", "Ask me", "A question appears when the game closes."),
    ("restart", "Start them again automatically", "They come back as soon as the game closes."),
    ("leave", "Leave them stopped", "You start them again yourself when you want them."),
)

_SEPARATORS = re.compile(r"[-_.]+")


def friendly_name(raw: str) -> str:
    """A readable name for a container name or unit: separators become spaces and each word starts in capitals
    unless it already has capitals or digits."""
    stem = raw[:-len(".service")] if raw.endswith(".service") else raw
    words = [word for word in _SEPARATORS.split(stem) if word]
    return " ".join(word if (word != word.lower() or any(c.isdigit() for c in word)) else word.capitalize()
                    for word in words) or raw


def format_memory(mib: Optional[int]) -> str:
    """GPU memory for a badge: "2.1 GiB", "512 MiB", "none measured" or "unknown"."""
    if mib is None:
        return "unknown"
    if mib <= 0:
        return "none measured"
    return f"{mib / 1024:.1f} GiB" if mib >= 1024 else f"{mib} MiB"


def clock_text(until: float, now: Optional[float] = None) -> str:
    """A local wall-clock time such as "3:40 PM", with the weekday when it is not today."""
    moment = time.localtime(until)
    today = time.localtime(time.time() if now is None else now)
    text = time.strftime("%I:%M %p", moment).lstrip("0")
    if (moment.tm_year, moment.tm_yday) == (today.tm_year, today.tm_yday):
        return text
    return f"{time.strftime('%a', moment)} {text}"


def things(count: int) -> str:
    return f"{count} thing" if count == 1 else f"{count} things"


def slug_id(unit: str, taken: List[str]) -> str:
    """A target id for a unit: lower case, `[a-z0-9_-]`, at most 40 characters, unique among `taken`."""
    stem = unit[:-len(".service")] if unit.endswith(".service") else unit
    base = re.sub(r"[^a-z0-9_-]+", "-", stem.lower()).strip("-_")[:40].strip("-_") or "service"
    candidate, number = base, 1
    while candidate in taken:
        number += 1
        suffix = f"-{number}"
        candidate = base[:40 - len(suffix)] + suffix
    return candidate


class ChoiceCard(QtWidgets.QAbstractButton):
    """A large selectable card with a title and a one-line explanation. Cards sharing a parent are mutually
    exclusive, and the arrow keys move between them."""

    def __init__(self, value: str, title: str, text: str, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.value = value
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        self.setMinimumHeight(8 * SPACE + 40)
        self.setAccessibleName(title)
        self.setAccessibleDescription(text)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2 * SPACE, 14, 4 * SPACE + 4, 14)
        layout.setSpacing(4)
        self.title_label = theme.styled_label(title, "title", self)
        self.text_label = theme.styled_label(text, "muted", self)
        for label in (self.title_label, self.text_label):
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self.title_label)
        layout.addWidget(self.text_label)
        layout.addStretch(1)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return max(self.minimumHeight(), self.layout().heightForWidth(width))

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(220, self.heightForWidth(220))

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        colors = theme.current_colors()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = theme.RADIUS_CARD
        checked = self.isChecked()
        fill = QtGui.QColor(colors.card)
        if checked:
            fill = QtGui.QColor(theme.mix(colors.card, colors.accent, 0.10))
        elif self.underMouse():
            fill = QtGui.QColor(colors.hover)
        painter.setBrush(fill)
        painter.setPen(QtGui.QPen(QtGui.QColor(colors.accent if checked else colors.border), 2 if checked else 1))
        painter.drawRoundedRect(rect, radius, radius)
        if self.hasFocus():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QtGui.QPen(QtGui.QColor(colors.accent), 2, Qt.DotLine))
            painter.drawRoundedRect(rect.adjusted(-0.5, -0.5, 0.5, 0.5), radius, radius)
        centre = QtCore.QPointF(self.width() - 2 * SPACE - 6, 2 * SPACE + 8)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QtGui.QPen(QtGui.QColor(colors.accent if checked else theme.mix(colors.border, colors.muted, 0.6)),
                                  1.6))
        painter.drawEllipse(centre, 8, 8)
        if checked:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QtGui.QColor(colors.accent))
            painter.drawEllipse(centre, 4.5, 4.5)

    def enterEvent(self, event: QtCore.QEvent) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self.update()
        super().leaveEvent(event)


class ChoiceGroup(QtWidgets.QWidget):
    """A row of `ChoiceCard`s bound to one setting. `valueChanged(str)` fires only for choices made by the user."""

    valueChanged = QtCore.pyqtSignal(str)

    def __init__(self, choices: Tuple[Tuple[str, str, str], ...], parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE + 4)
        self.cards: Dict[str, ChoiceCard] = {}
        for value, title, text in choices:
            card = ChoiceCard(value, title, text, self)
            card.toggled.connect(lambda checked, v=value: checked and self.valueChanged.emit(v))
            layout.addWidget(card, 1)
            self.cards[value] = card

    def setValue(self, value: str) -> None:
        for key, card in self.cards.items():
            card.blockSignals(True)
            card.setChecked(key == value)
            card.blockSignals(False)
            card.update()

    def value(self) -> str:
        return next((key for key, card in self.cards.items() if card.isChecked()), "")


@dataclasses.dataclass
class Item:
    """One row of "What gets paused"."""

    kind: str  # "container" or "service"
    key: str
    name: str  # the container's name or the unit
    title: str
    subtitle: str
    memory: str
    on: bool
    scope: str = "system"
    locked: str = ""  # why the switch cannot be changed, when it cannot


class ItemRow(QtWidgets.QWidget):
    def __init__(self, item: Item, on_toggle: Callable[[Item, bool], None], parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.item = item
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 6, 0, 6)
        row.setSpacing(SPACE + 4)
        texts = QtWidgets.QVBoxLayout()
        texts.setSpacing(1)
        self.title_label = theme.ElidedLabel(item.title, "title")
        self.subtitle_label = theme.ElidedLabel(item.subtitle, "small")
        texts.addWidget(self.title_label)
        texts.addWidget(self.subtitle_label)
        row.addLayout(texts, 1)
        self.badge = theme.Badge(item.memory, "muted")
        self.badge.setToolTip("GPU memory in use")
        row.addWidget(self.badge, 0, Qt.AlignVCenter)
        self.switch = theme.ToggleSwitch(item.on, accessible_name=f"Pause {item.title} while gaming")
        self.switch.toggled.connect(lambda checked: on_toggle(self.item, checked))
        row.addWidget(self.switch, 0, Qt.AlignVCenter)
        self.update_item(item)

    def update_item(self, item: Item) -> None:
        self.item = item
        self.title_label.setText(item.title)
        self.subtitle_label.setText(item.subtitle)
        self.badge.setText(item.memory)
        self.switch.blockSignals(True)
        self.switch.setChecked(item.on)
        self.switch.blockSignals(False)
        self.switch.update()
        self.switch.setEnabled(not item.locked)
        self.switch.setToolTip(item.locked)


def describe(model: SettingsModel, status: Optional[Status], active: int, now: Optional[float] = None
             ) -> Tuple[str, str, str, str]:
    """(state key, headline, detail, icon state) for the status card. State keys: "watching", "paused", "off",
    "disabled"."""
    paused, until = model.paused()
    if status is None:
        return ("off", "GPU Game Guard is not running", "Nothing will be paused until it is started.", "off")
    if paused or status.paused:
        if until is None and status.paused and status.paused_until not in (None, float("inf")):
            until = status.paused_until
        when = f"until {clock_text(until, now)}" if until is not None else "until you resume it"
        return ("paused", f"GPU Game Guard is paused {when}",
                "Games start as usual and nothing is stopped.", "paused")
    if not model.saved.enabled or status.state == "disabled":
        return ("disabled", "GPU Game Guard is switched off", "Games start as usual until it is switched on.", "off")
    if status.stopped:
        names = ", ".join(friendly_name(str(ref.get("name", ""))) for ref in status.stopped[:3])
        more = f" and {len(status.stopped) - 3} more" if len(status.stopped) > 3 else ""
        return ("watching", "GPU Game Guard is watching", f"Paused for the current game: {names}{more}.", "ok")
    if active == 0:
        return ("watching", "GPU Game Guard is watching", "Nothing is set to be paused right now.", "ok")
    return ("watching", "GPU Game Guard is watching", f"Ready to pause {things(active)} when a game starts.", "ok")


class BasicView(QtWidgets.QWidget):
    """The Basic page. `advancedRequested` fires when the footnote link is used."""

    advancedRequested = QtCore.pyqtSignal()

    def __init__(self, model: SettingsModel, autoload: bool = True, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.model = model
        self._loaded = False
        self.state = "off"
        self._rows: List[ItemRow] = []
        self._widgets: List[QtWidgets.QWidget] = []  # rows and the dividers between them
        self._row_keys: List[Tuple[str, str]] = []
        self._build()
        model.changed.connect(self.sync)
        self.sync()
        if autoload:
            QtCore.QTimer.singleShot(0, self.load)

    # layout

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        page = QtWidgets.QWidget()
        page.setObjectName("Window")
        scroll.setWidget(page)
        centre = QtWidgets.QHBoxLayout(page)
        centre.setContentsMargins(3 * SPACE, 2 * SPACE, 3 * SPACE, 3 * SPACE)
        column_host = QtWidgets.QWidget()
        column_host.setMaximumWidth(CONTENT_WIDTH)
        centre.addStretch(1)
        centre.addWidget(column_host, 100)
        centre.addStretch(1)
        column = QtWidgets.QVBoxLayout(column_host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2 * SPACE)

        column.addWidget(self._build_status())

        column.addWidget(theme.SectionHeader("When I start a game",
                                             "What should happen to the apps below when a game launches."))
        self.on_launch = ChoiceGroup(ON_LAUNCH_CHOICES)
        self.on_launch.valueChanged.connect(lambda value: self.model.update(on_launch=value))
        column.addWidget(self.on_launch)

        column.addWidget(self._build_list())

        column.addWidget(theme.SectionHeader("After the game", "What should happen once the game has closed."))
        self.after_game = ChoiceGroup(AFTER_GAME_CHOICES)
        self.after_game.valueChanged.connect(lambda value: self.model.update(after_game=value))
        column.addWidget(self.after_game)

        self.footnote = theme.styled_label(
            'Need rules for a single game, or something that is not listed? '
            '<a href="advanced">Switch to Advanced</a>.', "muted")
        self.footnote.setTextFormat(Qt.RichText)
        self.footnote.setTextInteractionFlags(Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard)
        self.footnote.linkActivated.connect(lambda _link: self.advancedRequested.emit())
        column.addWidget(self.footnote)
        column.addStretch(1)

    def _build_status(self) -> theme.Card:
        card = theme.Card()
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(2 * SPACE)
        self.status_icon = QtWidgets.QLabel()
        self.status_icon.setFixedSize(52, 52)
        top.addWidget(self.status_icon, 0, Qt.AlignTop)
        texts = QtWidgets.QVBoxLayout()
        texts.setSpacing(2)
        self.headline = theme.styled_label("", "headline")
        self.detail = theme.styled_label("", "muted")
        texts.addWidget(self.headline)
        texts.addWidget(self.detail)
        top.addLayout(texts, 1)
        card.addLayout(top)
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(SPACE)
        self.pause_hour = theme.button("Pause 1 hour")
        self.pause_until_resumed = theme.button("Pause until resumed")
        self.resume_button = theme.button("Resume", "primary")
        self.switch_on = theme.button("Switch on", "primary")
        self.pause_hour.clicked.connect(lambda: self.model.pause(3600))
        self.pause_until_resumed.clicked.connect(lambda: self.model.pause(None))
        self.resume_button.clicked.connect(self.model.resume)
        self.switch_on.clicked.connect(lambda: self.model.update(enabled=True))
        for widget in (self.pause_hour, self.pause_until_resumed, self.resume_button, self.switch_on):
            buttons.addWidget(widget)
        buttons.addStretch(1)
        self.button_row = QtWidgets.QWidget()
        self.button_row.setLayout(buttons)
        buttons.setContentsMargins(0, SPACE, 0, 0)
        card.addWidget(self.button_row)
        return card

    def _build_list(self) -> theme.Card:
        card = theme.Card()
        head = QtWidgets.QHBoxLayout()
        titles = QtWidgets.QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(theme.styled_label("What gets paused", "title"))
        titles.addWidget(theme.styled_label(
            "Apps using your graphics card right now. Switch off anything that should keep running.", "muted"))
        head.addLayout(titles, 1)
        self.refresh_button = theme.button("Refresh")
        self.refresh_button.clicked.connect(lambda: self.load(True))
        head.addWidget(self.refresh_button, 0, Qt.AlignTop)
        card.addLayout(head)
        divider = QtWidgets.QFrame()
        divider.setObjectName("Divider")
        card.addWidget(divider)
        self.rows_layout = QtWidgets.QVBoxLayout()
        self.rows_layout.setSpacing(0)
        card.addLayout(self.rows_layout)
        self.empty_label = theme.styled_label("", "muted")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setMinimumHeight(6 * SPACE)
        card.addWidget(self.empty_label)
        return card

    # data

    def load(self, refresh: bool = False) -> None:
        """Look for things that could be paused (the first call, or `refresh`) and show them."""
        self.refresh_button.setEnabled(False)
        try:
            self.model.candidates(refresh)
        finally:
            self.refresh_button.setEnabled(True)
        self._loaded = True
        self.sync()

    def items(self) -> List[Item]:
        """The rows to show now: discovered containers and services, then configured services that are idle."""
        config = self.model.config
        found: List[Item] = []
        seen = set()
        for candidate in self.model.candidates(False) if self._loaded else []:
            item = self._item_for(candidate, config)
            if item is not None:
                found.append(item)
                seen.add((item.kind, item.name, item.scope))
        for spec in config.targets:
            if spec.kind == "service" and ("service", spec.unit, spec.scope) not in seen:
                found.append(Item("service", f"service:{spec.scope}:{spec.unit}", spec.unit,
                                  spec.label if spec.label and spec.label != spec.id else friendly_name(spec.unit),
                                  spec.unit + (" (user service)" if spec.scope == "user" else ""), "idle",
                                  spec.enabled, spec.scope))
        return found

    @staticmethod
    def _item_for(candidate: Candidate, config: Config) -> Optional[Item]:
        if candidate.kind == "container":
            image = candidate.detail.split("image ", 1)[1] if "image " in candidate.detail else ""
            subtitle = image or candidate.detail
            locked = ""
            ignored = config.is_ignored(candidate.name)
            if ignored and candidate.name not in config.ignore_containers:
                locked = "Ignored by a pattern in Advanced"
            return Item("container", candidate.key, candidate.name, friendly_name(candidate.name), subtitle,
                        format_memory(candidate.vram_mib), config.auto_containers and not ignored, locked=locked)
        if candidate.kind == "service":
            scope = (candidate.spec or {}).get("scope", "system")
            spec = BasicView._find_service(config, candidate.name, scope)
            return Item("service", candidate.key, candidate.name, friendly_name(candidate.name),
                        candidate.name + (" (user service)" if scope == "user" else ""),
                        format_memory(candidate.vram_mib), bool(spec and spec.enabled), scope)
        return None

    @staticmethod
    def _find_service(config: Config, unit: str, scope: str) -> Optional[TargetSpec]:
        return next((t for t in config.targets if t.kind == "service" and t.unit == unit and t.scope == scope), None)

    # updates

    def sync(self) -> None:
        """Bring every control in line with the model."""
        config = self.model.config
        self.on_launch.setValue(config.on_launch if config.on_launch in ON_LAUNCH else "ask")
        self.after_game.setValue(config.after_game if config.after_game in AFTER_GAME else "ask")
        items = self.items()
        keys = [(item.kind, item.key) for item in items]
        if keys != self._row_keys:
            self._rebuild(items, keys)
        else:
            for row, item in zip(self._rows, items):
                row.update_item(item)
        if self._loaded:
            self.empty_label.setText("Nothing is using the GPU right now. Start your apps and press Refresh.")
        else:
            self.empty_label.setText("Looking for apps that use your graphics card...")
        self.empty_label.setVisible(not items)
        self.refresh_status()

    def _rebuild(self, items: List[Item], keys: List[Tuple[str, str]]) -> None:
        for widget in self._widgets:
            self.rows_layout.removeWidget(widget)
            widget.deleteLater()
        self._rows, self._widgets = [], []
        for index, item in enumerate(items):
            if index:
                divider = QtWidgets.QFrame()
                divider.setObjectName("Divider")
                self.rows_layout.addWidget(divider)
                self._widgets.append(divider)
            row = ItemRow(item, self._toggled)
            self.rows_layout.addWidget(row)
            self._widgets.append(row)
            self._rows.append(row)
        self._row_keys = keys

    def refresh_status(self, now: Optional[float] = None) -> None:
        """Update the status card from the service's status and the pause state."""
        status = self.model.status()
        active = sum(1 for item in self.items() if item.on)
        state, headline, detail, icon = describe(self.model, status, active, now)
        self.state = state
        self.headline.setText(headline)
        self.detail.setText(detail)
        self.status_icon.setPixmap(theme.app_icon(icon).pixmap(48, 48))
        enabled = self.model.config.enabled
        paused = state == "paused"
        running = state != "off"
        self.pause_hour.setVisible(running and not paused and enabled)
        self.pause_until_resumed.setVisible(running and not paused and enabled)
        self.resume_button.setVisible(paused)
        self.switch_on.setVisible(not enabled)
        self.button_row.setVisible(not (state == "off" and enabled))

    def _toggled(self, item: Item, on: bool) -> None:
        if item.kind == "container":
            ignored = [name for name in self.model.config.ignore_containers if name != item.name]
            if on:
                if not self.model.config.auto_containers:
                    self.model.update(auto_containers=True, ignore_containers=ignored)
                else:
                    self.model.update(ignore_containers=ignored)
            else:
                self.model.update(ignore_containers=ignored + [item.name])
            return
        existing = self._find_service(self.model.config, item.name, item.scope)
        if not on:
            if existing is not None:
                self.model.remove_target(existing.id)
            return
        if existing is not None:
            self.model.set_target(dataclasses.replace(existing, enabled=True))
            return
        taken = [t.id for t in self.model.config.targets]
        self.model.set_target(TargetSpec(id=slug_id(item.name, taken), kind="service", label=item.title,
                                         unit=item.name, scope=item.scope))
