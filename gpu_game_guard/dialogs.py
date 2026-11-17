"""Dialog backends: ask the user what to do about a launch and whether to restart what was stopped.

Four Prompter implementations share one set of message builders. Each builder takes a markup name:
"html" (kdialog rich text), "pango" (zenity) or "plain" (logs and notifications). Every user-supplied
string is escaped for the markup in use.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from html import escape
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from .model import Launch, Ref, Target
from .util import log

TITLE = "GPU Game Guard"
HEAVY_VRAM_MIB = 4096  # container GPU memory at which the warning stops being tentative; the guard may overwrite it
MARKUPS = ("html", "pango", "plain")

LABEL_STOP = "Stop containers and launch"
LABEL_ANYWAY = "Launch anyway"
LABEL_CANCEL = "Cancel"
LABEL_RESTART = "Restart"
LABEL_LATER = "Ask me later"
LABEL_LEAVE = "Leave stopped"

NOTIFY_TIMEOUT_SECONDS = 600  # an unanswered actionable notification stops waiting after this long
NOTIFY_EXPIRE_MS = NOTIFY_TIMEOUT_SECONDS * 1000
INFO_TIMEOUT_SECONDS = 30  # commands that only display something and return

# Characters that cannot appear in XML text or in a command line argument: C0 and C1 controls other than tab and
# newline, lone surrogates and the two non-characters at the end of the basic plane.
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]")
_DIALOG_ERRORS = (OSError, subprocess.SubprocessError, ValueError)


def format_mib(mib: int) -> str:
    return f"{mib / 1024:.1f} GiB" if mib >= 1024 else f"{mib} MiB"


def _clean(text: object) -> str:
    """The text with every character that would break markup or an argument vector replaced by a space."""
    return _UNSAFE.sub(" ", str(text))


def _esc(text: object, markup: str) -> str:
    """Clean a dynamic string and escape it for the markup; plain text is only cleaned."""
    cleaned = _clean(text)
    return cleaned if markup == "plain" else escape(cleaned, quote=False)


def _notification_text(text: object) -> str:
    """Notification servers treat title and body as markup, so the text is cleaned and escaped."""
    return escape(_clean(text), quote=False)


def _bold(text: str, markup: str) -> str:
    return text if markup == "plain" else f"<b>{text}</b>"


def _small(text: str, markup: str) -> str:
    return text if markup == "plain" else f"<small>{text}</small>"


def impact_sentence(targets: Sequence[Target], markup: str = "plain") -> str:
    """What the containers could do to the game, never claiming more than the measurements show."""
    if not targets:
        return ""
    measured = [t for t in targets if t.vram_mib is not None]
    held = sum(t.vram_mib or 0 for t in measured)
    amount = _bold(format_mib(held), markup)
    single = len(targets) == 1
    heavy = held >= HEAVY_VRAM_MIB
    consequence = "so the game could stutter, load slowly or run out of memory."
    if not measured:
        return "GPU memory use could not be measured, so the impact on the game is unknown."
    if len(measured) < len(targets):
        if held == 0:
            return ("The ones that could be measured hold no GPU memory right now; the rest could not be measured, "
                    "so the impact on the game is uncertain.")
        text = (f"The impact on the game is uncertain: at least {amount} held by the ones that could be measured; "
                "the rest could not be measured.")
        if heavy:
            text += f" That much competes for GPU time, {consequence}"
        return text
    if heavy:
        if single:
            return f"It holds {amount} of GPU memory and competes for GPU time, {consequence}"
        return f"Together they hold {amount} of GPU memory and compete for GPU time, {consequence}"
    if held > 0:
        if single:
            return (f"It holds {amount} of GPU memory. The impact on the game is probably modest, but it can still "
                    "take GPU time.")
        return (f"Together they hold {amount} of GPU memory. The impact on the game is probably modest, but they can "
                "still take GPU time.")
    if single:
        return ("It is not holding any GPU memory right now, so the impact on the game should be small, though it can "
                "claim the GPU as soon as it gets work.")
    return ("They are not holding any GPU memory right now, so the impact on the game should be small, though they "
            "can claim the GPU as soon as they get work.")


def _memory_text(target: Target) -> str:
    if target.vram_mib is None:
        return "GPU memory use unknown"
    if target.vram_mib == 0:
        return "no GPU memory in use right now"
    return f"{format_mib(target.vram_mib)} of GPU memory"


def _detail(target: Target, markup: str) -> str:
    memory = _memory_text(target)
    return f"{_esc(target.image, markup)} · {memory}" if target.image else memory


def _totals_text(totals: Optional[Tuple[int, int]]) -> str:
    if not totals:
        return ""
    return f"GPU memory in use by all programs: {format_mib(totals[0])} of {format_mib(totals[1])}."


def launch_message(launch: Launch, targets: Sequence[Target], totals: Optional[Tuple[int, int]],
                   markup: str = "plain") -> str:
    title = _esc(launch.title, markup)
    intro = (f"{_bold(TITLE, markup)} ended the launch of {_bold(title, markup)} because these containers have "
             "access to the GPU:")
    summary = _totals_text(totals)
    impact = impact_sentence(targets, markup)
    question = "Stop them and start the game?"
    if markup == "html":
        rows = "".join(f"<li><b>{_esc(t.name, markup)}</b><br><small>{_detail(t, markup)}</small></li>" for t in targets)
        small = f"<p><small>{summary}</small></p>" if summary else ""
        verdict = f"<p>{impact}</p>" if impact else ""
        return f"<p>{intro}</p><ul>{rows}</ul>{small}{verdict}<p>{question}</p>"
    lines = [intro, ""]
    for t in targets:
        lines.append(f"• {_bold(_esc(t.name, markup), markup)}")
        lines.append(f"    {_small(_detail(t, markup), markup)}")
    if summary:
        lines += ["", _small(summary, markup)]
    if impact:
        lines += ["", impact]
    lines += ["", question]
    return "\n".join(lines)


def restart_message(names: Sequence[str], markup: str = "plain") -> str:
    intro = f"{_bold(TITLE, markup)} stopped these containers earlier. Restart them?"
    if markup == "html":
        return f"<p>{intro}</p><ul>{''.join(f'<li>{_esc(n, markup)}</li>' for n in names)}</ul>"
    return "\n".join([intro, ""] + [f"• {_esc(n, markup)}" for n in names])


def _names(refs: Sequence[Ref]) -> List[str]:
    """The names of the refs that have one; a ref without a name has nothing to show."""
    return [n for n in (str(r.get("name") or "") for r in refs) if _clean(n).strip()]


def _run(command: List[str], timeout: Optional[float]) -> "subprocess.CompletedProcess":
    return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                          timeout=timeout)


class _Base:
    name = ""
    command = ""
    can_ask = True

    def __init__(self, which: Callable[[str], Optional[str]] = shutil.which):
        self._which = which

    def available(self) -> bool:
        return self._which(self.command) is not None

    def notify(self, title: str, body: str) -> None:
        _send_notification(self._which, title, body)


def _send_notification(which: Callable[[str], Optional[str]], title: str, body: str) -> None:
    """Show a passive notification through notify-send; without it the message goes to the log."""
    log(f"{_clean(title)}: {_clean(body)}")
    if not which("notify-send"):
        return
    try:
        _run(["notify-send", "--app-name", TITLE, _notification_text(title), _notification_text(body)],
             INFO_TIMEOUT_SECONDS)
    except _DIALOG_ERRORS as exc:
        log(f"cannot show a notification ({exc})")


class ZenityPrompter(_Base):
    """Questions through zenity. The OK button is the primary choice; a crash, a missing display or a closed window
    ends with exit status 1 and no output, which is the least destructive answer, never a deliberate refusal."""

    name = "zenity"
    command = "zenity"

    def launch(self, launch: Launch, targets: List[Target], totals: Optional[Tuple[int, int]]) -> str:
        text = launch_message(launch, targets, totals, "pango")
        try:
            result = _run(["zenity", "--question", "--title", TITLE, "--text", text, "--ok-label", LABEL_STOP,
                           "--cancel-label", LABEL_ANYWAY, "--extra-button", LABEL_CANCEL], None)
        except _DIALOG_ERRORS as exc:
            log(f"cannot show a dialog ({exc}); launching anyway")
            return "anyway"
        if result.returncode == 0:
            return "stop"
        if result.returncode == 1 and (result.stdout or "").strip() == LABEL_CANCEL:
            return "cancel"
        return "anyway"

    def restart(self, refs: List[Ref]) -> Optional[bool]:
        names = _names(refs)
        if not names:
            return None
        text = restart_message(names, "pango")
        try:
            result = _run(["zenity", "--question", "--title", TITLE, "--text", text, "--ok-label", LABEL_RESTART,
                           "--cancel-label", LABEL_LATER, "--extra-button", LABEL_LEAVE], None)
        except _DIALOG_ERRORS as exc:
            log(f"cannot show a dialog ({exc}); keeping the containers marked as stopped")
            return None
        if result.returncode == 0:
            return True
        if result.returncode == 1 and (result.stdout or "").strip() == LABEL_LEAVE:
            return False
        return None


class KdialogPrompter(_Base):
    """Questions through kdialog --yesnocancel: yes is the primary choice and cancel the deliberate refusal; any
    other exit status is treated like an unanswered question."""

    name = "kdialog"
    command = "kdialog"

    def launch(self, launch: Launch, targets: List[Target], totals: Optional[Tuple[int, int]]) -> str:
        text = launch_message(launch, targets, totals, "html")
        try:
            result = _run(["kdialog", "--title", TITLE, "--yes-label", LABEL_STOP, "--no-label", LABEL_ANYWAY,
                           "--cancel-label", LABEL_CANCEL, "--yesnocancel", text], None)
        except _DIALOG_ERRORS as exc:
            log(f"cannot show a dialog ({exc}); launching anyway")
            return "anyway"
        return {0: "stop", 2: "cancel"}.get(result.returncode, "anyway")

    def restart(self, refs: List[Ref]) -> Optional[bool]:
        names = _names(refs)
        if not names:
            return None
        text = restart_message(names, "html")
        try:
            result = _run(["kdialog", "--title", TITLE, "--yes-label", LABEL_RESTART, "--no-label", LABEL_LATER,
                           "--cancel-label", LABEL_LEAVE, "--yesnocancel", text], None)
        except _DIALOG_ERRORS as exc:
            log(f"cannot show a dialog ({exc}); keeping the containers marked as stopped")
            return None
        return {0: True, 2: False}.get(result.returncode)

    def notify(self, title: str, body: str) -> None:
        log(f"{_clean(title)}: {_clean(body)}")
        try:
            _run(["kdialog", "--title", _clean(title), "--passivepopup", _notification_text(body), "10"],
                 INFO_TIMEOUT_SECONDS)
        except _DIALOG_ERRORS as exc:
            log(f"cannot show a notification ({exc})")


class NotifyPrompter(_Base):
    """Desktop notifications. Questions are only asked when notify-send supports actions."""

    name = "notify"
    command = "notify-send"

    def __init__(self, which: Callable[[str], Optional[str]] = shutil.which):
        super().__init__(which)
        self._actions: Optional[bool] = None

    @property
    def can_ask(self) -> bool:  # type: ignore[override]
        return self.supports_actions()

    def supports_actions(self) -> bool:
        if self._actions is None:
            try:
                result = _run(["notify-send", "--help"], INFO_TIMEOUT_SECONDS)
                self._actions = "--action" in (result.stdout or "")
            except _DIALOG_ERRORS:
                self._actions = False
        return self._actions

    def _ask(self, summary: str, body: str, actions: Sequence[Tuple[str, str]]) -> Optional[str]:
        """The name of the chosen action; None when the notification was dismissed or expired."""
        command = ["notify-send", "--app-name", TITLE, "--wait", f"--expire-time={NOTIFY_EXPIRE_MS}"]
        command += [f"--action={name}={label}" for name, label in actions]
        command += [_notification_text(summary), _notification_text(body)]
        result = _run(command, NOTIFY_TIMEOUT_SECONDS + 30)
        chosen = (result.stdout or "").strip()
        return chosen if chosen in dict(actions) else None

    def launch(self, launch: Launch, targets: List[Target], totals: Optional[Tuple[int, int]]) -> str:
        if not self.supports_actions():
            self.notify(TITLE, f"Launching {launch.title} with GPU containers running; this notification cannot ask.")
            return "anyway"
        body = launch_message(launch, targets, totals, "plain")
        try:
            chosen = self._ask(TITLE, body, [("stop", LABEL_STOP), ("anyway", LABEL_ANYWAY), ("cancel", LABEL_CANCEL)])
        except _DIALOG_ERRORS as exc:
            log(f"cannot show a notification ({exc}); launching anyway")
            return "anyway"
        return chosen or "anyway"  # an unanswered question must not strand the player

    def restart(self, refs: List[Ref]) -> Optional[bool]:
        names = _names(refs)
        if not names or not self.supports_actions():
            return None
        body = restart_message(names, "plain")
        try:
            chosen = self._ask(TITLE, body, [("restart", LABEL_RESTART), ("leave", LABEL_LEAVE)])
        except _DIALOG_ERRORS as exc:
            log(f"cannot show a notification ({exc}); keeping the containers marked as stopped")
            return None
        return {"restart": True, "leave": False}.get(chosen) if chosen else None


class NullPrompter:
    """No dialog is possible: launches proceed and the restart question is deferred."""

    name = "none"
    can_ask = False

    def available(self) -> bool:
        return True

    def launch(self, launch: Launch, targets: List[Target], totals: Optional[Tuple[int, int]]) -> str:
        return "anyway"

    def restart(self, refs: List[Ref]) -> Optional[bool]:
        return None

    def notify(self, title: str, body: str) -> None:
        log(f"{_clean(title)}: {_clean(body)}")


_BY_NAME = {"zenity": ZenityPrompter, "kdialog": KdialogPrompter, "notify": NotifyPrompter}


def _is_kde(environ: Mapping[str, str]) -> bool:
    return "KDE" in (environ.get("XDG_CURRENT_DESKTOP") or "").upper() or bool(environ.get("KDE_FULL_SESSION"))


def choose(preference: str = "auto", environ: Mapping[str, str] = os.environ,
           which: Callable[[str], Optional[str]] = shutil.which):
    """Pick a Prompter. An explicit preference that is unavailable falls back to the automatic order."""
    if preference in _BY_NAME:
        prompter = _BY_NAME[preference](which)
        if prompter.available():
            return prompter
        log(f"dialog backend {preference} is not available; choosing automatically")
    elif preference == "none":
        return NullPrompter()
    elif preference != "auto":
        log(f"unknown dialog backend {preference!r}; choosing automatically")
    order = ["kdialog", "zenity"] if _is_kde(environ) else ["zenity", "kdialog"]
    for name in order + ["notify"]:
        prompter = _BY_NAME[name](which)
        if prompter.available():
            return prompter
    return NullPrompter()
