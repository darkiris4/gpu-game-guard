"""The Qt prompt dialogs and the prompter that shows them.

`QtPrompter` runs each dialog in a short-lived child process (`<self> prompt launch|restart`): the payload goes in
as JSON on standard input and the answer comes back as JSON on standard output, so a crash in Qt or the display
server can never take the watcher down. Every failure ends in the least destructive answer: "anyway" for a launch
question, None (ask later) for the restart question.

Payload (standard input):
    {"kind": "launch" | "restart", "title": str,
     "targets": [{"kind": str, "name": str, "image": str, "vram_mib": int | null, "label": str}],
     "totals": [used_mib, total_mib] | null, "remember": bool, "heavy_vram_mib": int}
`label` is the unit of a service or another short qualifier; it is shown where an image would be.
Answer (standard output):
    {"choice": "stop" | "anyway" | "cancel", "remember": "stop" | "ignore" | null}   (launch)
    {"choice": "restart" | "later" | "leave"}                                         (restart)
"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple

from .. import dialogs
from ..model import Launch, Ref, Target
from ..util import log
from . import qt

TIMEOUT_SECONDS = 600  # a dialog nobody answers is given up on after this long
LAUNCH_CHOICES = ("stop", "anyway", "cancel")
RESTART_CHOICES = ("restart", "later", "leave")
KIND_LABELS = {"container": "Container", "service": "Service", "command": "Command", "process": "Process"}
HEADLINE_TITLE_LIMIT = 120
MAX_WIDTH = 640  # widest the dialog gets at the base font size, in pixels
MIN_WIDTH = 520
HEIGHT_FRACTION = 0.8  # the most of the screen's height the dialog may take
MIN_LIST_HEIGHT = 76  # the target list always shows at least one card
ENTER_GUARD_MS = 700  # Enter and Return are ignored for this long after the window appears
HEADLESS_PLATFORMS = ("offscreen", "minimal", "minimalegl", "vnc", "linuxfb")
LAUNCH_LABELS = {"stop": "&Stop and launch", "anyway": "Launch &anyway", "cancel": "&Cancel"}
RESTART_LABELS = {"restart": "&Restart", "later": "Ask me &later", "leave": "Leave &stopped"}
REMEMBER_LABEL = "&Remember my choice for this game"
test_platform: Optional[str] = None  # test hook: applied to QT_QPA_PLATFORM after the platform choice, e.g. "offscreen"


def self_command(main: Any = None, argv: Optional[Sequence[str]] = None, executable: Optional[str] = None) -> List[str]:
    """The command line that starts this program again, without its subcommand. Running as `python -m
    gpu_game_guard` gives `[python, "-m", "gpu_game_guard"]`; running from a zipapp (no module spec, or the
    spec of a bare `__main__`) gives `[python, <path of the zipapp>]`. Without a script path (`python -c`, standard
    input or an empty `sys.argv`) the module form is used."""
    main = sys.modules.get("__main__") if main is None else main
    argv = list(sys.argv if argv is None else argv)
    executable = executable or sys.executable
    spec = getattr(main, "__spec__", None)
    name = getattr(spec, "name", None)
    if name in (None, "__main__") and argv and argv[0] and argv[0] not in ("-m", "-c", "-"):
        return [executable, os.path.abspath(argv[0])]
    return [executable, "-m", "gpu_game_guard"]


def display_available(environ: Mapping[str, str]) -> bool:
    return bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


def platform_changes(environ: Mapping[str, str]) -> Dict[str, Optional[str]]:
    """The change to `QT_QPA_PLATFORM` the dialog process makes before Qt starts: `{"QT_QPA_PLATFORM": value}` to set
    it, a value of None to remove it, an empty dict to leave it. An inherited headless platform (offscreen, minimal
    and the like) would show nothing, so it is removed; with no platform chosen, a Wayland session gets
    `wayland;xcb`, which falls back to X11 when the Wayland connection fails."""
    changes: Dict[str, Optional[str]] = {}
    current = environ.get("QT_QPA_PLATFORM") or ""
    if current and re.split(r"[;:]", current)[0].strip().lower() in HEADLESS_PLATFORMS:
        changes["QT_QPA_PLATFORM"] = None
        current = ""
    if not current and environ.get("WAYLAND_DISPLAY"):
        changes["QT_QPA_PLATFORM"] = "wayland;xcb"
    return changes


def effective_environment(environ: Mapping[str, str]) -> Dict[str, str]:
    """`environ` with `platform_changes` (and the `test_platform` hook) applied."""
    result = dict(environ)
    for key, value in platform_changes(environ).items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    if test_platform:
        result["QT_QPA_PLATFORM"] = test_platform
    return result


def target_payload(target: Target) -> Dict[str, Any]:
    unit = (target.extra or {}).get("unit")
    return {"kind": target.kind, "name": target.name, "image": target.image, "vram_mib": target.vram_mib,
            "label": str(unit) if unit else ""}


def launch_payload(launch: Launch, targets: Sequence[Target], totals: Optional[Tuple[int, int]], remember: bool,
                   heavy_vram_mib: int) -> Dict[str, Any]:
    return {"kind": "launch", "title": launch.title, "targets": [target_payload(t) for t in targets],
            "totals": list(totals) if totals else None, "remember": bool(remember),
            "heavy_vram_mib": int(heavy_vram_mib)}


def restart_payload(refs: Sequence[Ref]) -> Dict[str, Any]:
    targets = [{"kind": str(r.get("kind") or "container"), "name": str(r.get("name") or ""), "image": "",
                "vram_mib": None, "label": str(r.get("unit") or "")} for r in refs]
    return {"kind": "restart", "title": "", "targets": targets, "totals": None, "remember": False,
            "heavy_vram_mib": dialogs.HEAVY_VRAM_MIB}


def parse_answer(kind: str, text: str) -> Optional[Dict[str, Any]]:
    """The validated answer object, or None when the text is not a valid answer to a `kind` question."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    choice = data.get("choice")
    if choice not in (LAUNCH_CHOICES if kind == "launch" else RESTART_CHOICES):
        return None
    if kind == "launch":
        remember = data.get("remember")
        return {"choice": choice, "remember": remember if remember in ("stop", "ignore") else None}
    return {"choice": choice}


def _remembered(answer: Dict[str, Any]) -> Optional[str]:
    """The action to remember: "stop" only together with the stop choice, "ignore" only with launching anyway."""
    pair = (answer["choice"], answer.get("remember"))
    return pair[1] if pair in (("stop", "stop"), ("anyway", "ignore")) else None


class QtPrompter:
    """A Prompter that shows a Qt dialog from a child process."""

    name = "qt"

    def __init__(self, environ: Mapping[str, str] = os.environ,
                 which: Callable[[str], Optional[str]] = shutil.which,
                 run: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
                 command: Optional[Callable[[], List[str]]] = None, timeout: float = TIMEOUT_SECONDS):
        self._environ, self._which, self._run, self._timeout = environ, which, run, timeout
        self._command = command or self_command
        self._remember: Optional[str] = None

    def available(self) -> bool:
        return qt.available() and display_available(self._environ)

    @property
    def can_ask(self) -> bool:
        return self.available()

    def _ask(self, kind: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Run the dialog process; None (with one log line) on any failure."""
        try:
            result = self._run([*self._command(), "prompt", kind], input=json.dumps(payload), stdout=subprocess.PIPE,
                               universal_newlines=True, timeout=self._timeout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log(f"cannot show the {kind} dialog ({exc})")
            return None
        if result.returncode != 0:
            log(f"the {kind} dialog failed (exit status {result.returncode})")
            return None
        answer = parse_answer(kind, result.stdout or "")
        if answer is None:
            log(f"the {kind} dialog gave an unusable answer")
        return answer

    def launch(self, launch: Launch, targets: List[Target], totals: Optional[Tuple[int, int]]) -> str:
        self._remember = None
        payload = launch_payload(launch, targets, totals, dialogs.REMEMBER_CHOICES, dialogs.HEAVY_VRAM_MIB)
        answer = self._ask("launch", payload)
        if answer is None:
            return "anyway"
        self._remember = _remembered(answer)
        return answer["choice"]

    def consume_remember(self) -> Optional[str]:
        remembered, self._remember = self._remember, None
        return remembered

    def restart(self, refs: List[Ref]) -> Optional[bool]:
        if not dialogs._names(refs):
            return None
        answer = self._ask("restart", restart_payload(refs))
        if answer is None:
            return None
        return {"restart": True, "leave": False}.get(answer["choice"])

    def notify(self, title: str, body: str) -> None:
        dialogs._send_notification(self._which, title, body)


@functools.lru_cache(maxsize=None)
def dialog_class() -> type:
    """The dialog widget class, built on first use so importing this module does not load Qt."""
    from . import theme

    QtCore, QtGui, QtWidgets = qt.QtCore, qt.QtGui, qt.QtWidgets
    Qt = QtCore.Qt

    class PromptDialog(QtWidgets.QDialog):
        """The launch or restart question. `choice` holds the answer; it starts as the answer for a dismissed window
        (cancel for a launch, later for a restart) and only a button changes it. Enter and Return are ignored for
        `ENTER_GUARD_MS` after the window appears, so a key press meant for the game cannot answer the question."""

        def __init__(self, payload: Mapping[str, Any], clock: Callable[[], float] = time.monotonic):
            super().__init__()
            self.payload = payload
            self._clock = clock
            self._shown_at: Optional[float] = None
            self.kind = "restart" if payload.get("kind") == "restart" else "launch"
            self.choice = "cancel" if self.kind == "launch" else "later"
            self.buttons: Dict[str, Any] = {}
            self.remember_box: Optional[Any] = None
            self.list_area: Optional[Any] = None
            self.targets = [self._target(t) for t in payload.get("targets") or [] if isinstance(t, dict)]
            scale = theme.font_scale()
            self.max_width = int(MAX_WIDTH * scale)
            self.setWindowTitle(dialogs.TITLE)
            self.setWindowIcon(theme.app_icon("ok"))
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.setMinimumWidth(int(MIN_WIDTH * scale))
            self.setMaximumWidth(self.max_width)
            self._build()
            self.fit_height(None)
            for widget in [self, *self.findChildren(QtWidgets.QWidget)]:
                widget.installEventFilter(self)
            self.buttons[self.default_choice].setFocus(Qt.OtherFocusReason)

        @property
        def default_choice(self) -> str:
            return "stop" if self.kind == "launch" else "restart"

        @staticmethod
        def _target(item: Mapping[str, Any]) -> Tuple[Target, str]:
            vram = item.get("vram_mib")
            vram = vram if isinstance(vram, int) and not isinstance(vram, bool) and vram >= 0 else None
            target = Target(theme.safe_text(item.get("kind") or "container").strip() or "container", "",
                            theme.safe_text(item.get("name") or "").strip(),
                            theme.safe_text(item.get("image") or "").strip(), vram)
            return target, theme.safe_text(item.get("label") or "").strip()

        def _build(self) -> None:
            space = theme.SPACE
            root = QtWidgets.QVBoxLayout(self)
            root.setContentsMargins(3 * space, 3 * space, 3 * space, 3 * space)
            root.setSpacing(2 * space)
            root.addLayout(self._header())
            if self.targets:
                area = QtWidgets.QScrollArea()
                area.setObjectName("TargetList")
                area.setWidgetResizable(True)
                area.setFrameShape(QtWidgets.QFrame.NoFrame)
                area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                inner = QtWidgets.QWidget()
                inner.setObjectName("TargetListContents")
                holder = QtWidgets.QVBoxLayout(inner)
                holder.setSpacing(space)
                holder.setContentsMargins(0, 0, space, 0)
                for target, label in self.targets:
                    holder.addWidget(self._target_card(target, label))
                area.setWidget(inner)
                self.list_area = area
                root.addWidget(area, 1)
            if self.kind == "launch":
                self._launch_details(root)
            root.addLayout(self._buttons())

        def _title_text(self, headline: Any) -> str:
            """The game title cut to fit the headline: very long titles are shortened, and a word wider than the
            line is elided so an unbreakable title cannot widen the dialog."""
            title = theme.safe_text(self.payload.get("title") or "").strip() or "this game"
            if len(title) > HEADLINE_TITLE_LIMIT:
                title = title[:HEADLINE_TITLE_LIMIT - 1] + "…"
            space = theme.SPACE
            line = self.max_width - 6 * space - 48 - 2 * space
            headline.ensurePolished()
            metrics = headline.fontMetrics()
            words = [metrics.elidedText(w, Qt.ElideMiddle, line) if metrics.horizontalAdvance(w) > line else w
                     for w in title.split(" ")]
            return " ".join(words)

        def _header(self) -> Any:
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(2 * theme.SPACE)
            icon = QtWidgets.QLabel()
            icon.setTextFormat(Qt.PlainText)
            icon.setPixmap(theme.app_icon("ok").pixmap(48, 48))
            icon.setAlignment(Qt.AlignTop)
            texts = QtWidgets.QVBoxLayout()
            texts.setSpacing(4)
            headline = theme.styled_label("", "headline")
            headline.setObjectName("Headline")
            headline.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            if self.kind == "launch":
                full = theme.safe_text(self.payload.get("title") or "").strip() or "this game"
                title = self._title_text(headline)
                count = len(self.targets)
                head = f"Launching {title}"
                sub = (f"GPU Game Guard paused this launch because {count} "
                       f"{'program is' if count == 1 else 'programs are'} using the GPU.")
                if title != full:
                    theme.plain_tooltip(headline, f"Launching {full}")
            else:
                head = "Restart what was stopped?"
                sub = "These were stopped for a game and are still stopped."
            headline.setText(head)
            texts.addWidget(headline)
            texts.addWidget(theme.styled_label(sub, "muted"))
            row.addWidget(icon)
            row.addLayout(texts, 1)
            return row

        def _target_card(self, target: Target, label: str) -> Any:
            card = theme.Card()
            card.setObjectName("Card")
            layout = card.layout()
            layout.setContentsMargins(16, 12, 16, 12)
            layout.setSpacing(4)
            top = QtWidgets.QHBoxLayout()
            top.setSpacing(theme.SPACE)
            top.addWidget(theme.Badge(KIND_LABELS.get(target.kind, target.kind.capitalize() or "Target"), "muted"))
            name = theme.ElidedLabel(target.name, "title")
            name.setObjectName("TargetName")
            top.addWidget(name, 1)
            card.addLayout(top)
            if self.kind == "launch":
                qualifier = target.image or label
                memory = dialogs._memory_text(target)
                detail = f"{qualifier} · {memory}" if qualifier else memory
                line = theme.ElidedLabel(detail, "muted")
                line.setObjectName("TargetDetail")
                card.addWidget(line)
            elif label:
                card.addWidget(theme.ElidedLabel(label, "muted"))
            return card

        def _launch_details(self, root: Any) -> None:
            targets = [t for t, _ in self.targets]
            threshold = _int(self.payload.get("heavy_vram_mib"), dialogs.HEAVY_VRAM_MIB)
            configured, dialogs.HEAVY_VRAM_MIB = dialogs.HEAVY_VRAM_MIB, threshold
            try:
                sentence = dialogs.impact_sentence(targets, "plain")
            finally:
                dialogs.HEAVY_VRAM_MIB = configured
            if sentence:
                held = sum(t.vram_mib or 0 for t in targets)
                heavy = held > 0 and held >= threshold
                note = theme.Callout(sentence, "warn" if heavy else "")
                note.setObjectName("Callout")
                root.addWidget(note)
            totals = self.payload.get("totals")
            if isinstance(totals, list) and len(totals) == 2 and all(isinstance(v, int) for v in totals):
                text = dialogs._totals_text((totals[0], totals[1]))
                root.addWidget(theme.styled_label(text, "small"))
            if self.payload.get("remember") is True:
                self.remember_box = QtWidgets.QCheckBox(REMEMBER_LABEL)
                root.addWidget(self.remember_box)

        def _buttons(self) -> Any:
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(theme.SPACE)
            if self.kind == "launch":
                spec = [("cancel", "secondary"), ("anyway", "secondary"), ("stop", "primary")]
                labels = LAUNCH_LABELS
            else:
                spec = [("leave", "secondary"), ("later", "secondary"), ("restart", "primary")]
                labels = RESTART_LABELS
            for index, (choice, role) in enumerate(spec):
                widget = theme.button(labels[choice], role)
                widget.setObjectName(f"button_{choice}")
                widget.clicked.connect(functools.partial(self.answer, choice))
                self.buttons[choice] = widget
                row.addWidget(widget)
                if index == 0:
                    row.addStretch(1)
            primary = spec[-1][0]
            self.buttons[primary].setDefault(True)
            self.buttons[primary].setAutoDefault(True)
            return row

        def answer(self, choice: str) -> None:
            self.choice = choice
            self.accept()

        def result_payload(self) -> Dict[str, Any]:
            if self.kind == "restart":
                return {"choice": self.choice}
            remember = None
            if self.remember_box is not None and self.remember_box.isChecked():
                remember = {"stop": "stop", "anyway": "ignore"}.get(self.choice)
            return {"choice": self.choice, "remember": remember}

        def enter_allowed(self) -> bool:
            """False until `ENTER_GUARD_MS` have passed since the window was shown."""
            return self._shown_at is not None and (self._clock() - self._shown_at) * 1000.0 >= ENTER_GUARD_MS

        def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802 (Qt naming)
            if (event.type() in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease)
                    and event.key() in (Qt.Key_Return, Qt.Key_Enter) and not self.enter_allowed()):
                return True
            return super().eventFilter(obj, event)

        def target_screen(self) -> Any:
            """The screen under the pointer, or the primary screen."""
            screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos())
            return screen if screen is not None else QtWidgets.QApplication.primaryScreen()

        def _height_for(self, width: int) -> int:
            height = self.layout().totalHeightForWidth(width)
            return height if height >= 0 else self.sizeHint().height()

        def fit_height(self, available: Optional[int]) -> None:
            """Size the target list to its contents, or, when `available` (the screen's usable height) is given, so
            that the dialog takes at most `HEIGHT_FRACTION` of it; the list scrolls when it does not fit."""
            area = self.list_area
            if area is None:
                return
            self.ensurePolished()
            inner = area.widget()
            content = inner.sizeHint().height() if inner is not None else MIN_LIST_HEIGHT
            area.setFixedHeight(content)
            self.layout().invalidate()
            self.layout().activate()
            if available is None:
                return
            floor = min(content, int(MIN_LIST_HEIGHT * theme.font_scale()))
            limit = int(available * HEIGHT_FRACTION)
            least = self.layout().minimumSize().height() - content + floor  # with the list at its smallest
            if limit < least:  # a very small screen: use all of it before clipping
                limit = min(available, least)
            width = min(max(self.sizeHint().width(), self.minimumWidth()), self.maximumWidth())
            total = self._height_for(width)
            if total > limit:
                area.setFixedHeight(max(floor, content - (total - limit)))
                self.layout().invalidate()
                self.layout().activate()
            self.setMaximumHeight(max(limit, 1))

        def place_in(self, available: Any) -> None:
            """Size the dialog for the screen area `available` (a QRect) and centre it there, kept fully inside."""
            width_cap = int(available.width() * 0.95)
            self.setMaximumWidth(min(self.max_width, width_cap))
            self.setMinimumWidth(min(self.minimumWidth(), width_cap))
            self.fit_height(available.height())
            width = min(max(self.sizeHint().width(), self.minimumWidth()), self.maximumWidth())
            height = min(self._height_for(width), self.maximumHeight())
            self.resize(width, height)
            frame = self.frameGeometry()
            full_w, full_h = max(frame.width(), width), max(frame.height(), height)
            x = available.left() + (available.width() - full_w) // 2
            y = available.top() + (available.height() - full_h) // 2
            x = max(available.left(), min(x, available.left() + available.width() - full_w))
            y = max(available.top(), min(y, available.top() + available.height() - full_h))
            self.move(x, y)

        def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt naming)
            super().showEvent(event)
            self._shown_at = self._clock()
            screen = self.target_screen()
            if screen is not None:
                self.place_in(screen.availableGeometry())
            self.buttons[self.default_choice].setFocus(Qt.OtherFocusReason)
            QtCore.QTimer.singleShot(0, self.activateWindow)

    return PromptDialog


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def build_dialog(payload: Mapping[str, Any], clock: Callable[[], float] = time.monotonic) -> Any:
    """A themed, not yet shown dialog for `payload` (needs a QApplication)."""
    return dialog_class()(payload, clock)


def _default_execute(dialog: Any) -> None:
    dialog.exec_()


def run_prompt(argv: Sequence[str], stdin: TextIO, stdout: TextIO,
               execute: Callable[[Any], None] = _default_execute, environ: Mapping[str, str] = os.environ) -> int:
    """The child process: read the payload, show the dialog, print the answer. `argv` is `["launch"]` or
    `["restart"]`. Returns 0 after printing an answer and non-zero when nothing could be shown, which the
    parent treats as a failure. `execute(dialog)` runs the dialog's event loop; tests replace it.

    The Qt platform is chosen first (see `platform_changes`) and written to `os.environ` when `environ` is it."""
    kind = argv[0] if argv else ""
    if kind not in ("launch", "restart"):
        log("usage: prompt launch|restart")
        return 2
    if not qt.available():
        log("PyQt5 is not installed")
        return 1
    effective = effective_environment(environ)
    if not (display_available(effective) or effective.get("QT_QPA_PLATFORM")):
        log("no display is available for the dialog")
        return 1
    if environ is os.environ:
        for key, value in platform_changes(environ).items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if test_platform:
            os.environ["QT_QPA_PLATFORM"] = test_platform
    try:
        payload = json.loads(stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("the payload is not an object")
    except ValueError as exc:
        log(f"unreadable dialog payload ({exc})")
        return 2
    payload["kind"] = kind
    try:
        from . import theme

        application = qt.app()
        theme.apply_theme(application)
        dialog = build_dialog(payload)
        execute(dialog)
        answer = dialog.result_payload()
    except Exception as exc:  # the parent fails safe on a non-zero status
        log(f"cannot show the dialog ({exc})")
        return 1
    stdout.write(json.dumps(answer) + "\n")
    stdout.flush()
    return 0
