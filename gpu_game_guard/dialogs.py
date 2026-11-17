"""Dialog backends: ask the user what to do about a launch and whether to restart what was stopped.

Four Prompter implementations share one set of message builders. Each builder takes a markup name:
"html" (kdialog rich text), "pango" (zenity) or "plain" (logs and notifications). Every user-supplied
string is escaped for the markup in use.
"""
from __future__ import annotations

import os
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
LABEL_LEAVE = "Leave stopped"

NOTIFY_TIMEOUT_SECONDS = 600  # an unanswered actionable notification stops waiting after this long
NOTIFY_EXPIRE_MS = NOTIFY_TIMEOUT_SECONDS * 1000
INFO_TIMEOUT_SECONDS = 30  # commands that only display something and return


def format_mib(mib: int) -> str:
    return f"{mib / 1024:.1f} GB" if mib >= 1024 else f"{mib} MB"


def _esc(text: object, markup: str) -> str:
    """Escape a dynamic string for the markup; plain text is left as it is."""
    return text if markup == "plain" else escape(str(text), quote=False)


def _bold(text: str, markup: str) -> str:
    return text if markup == "plain" else f"<b>{text}</b>"


def _small(text: str, markup: str) -> str:
    return text if markup == "plain" else f"<small>{text}</small>"


def impact_sentence(targets: Sequence[Target], markup: str = "plain") -> str:
    held = sum(t.vram_mib or 0 for t in targets)
    if held >= HEAVY_VRAM_MIB:
        return (f"Together they hold {_bold(format_mib(held), markup)} of GPU memory and compete for GPU time, so the "
                "game could stutter, load slowly or run out of memory.")
    if held > 0:
        return (f"Together they hold {format_mib(held)} of GPU memory. The impact on the game is probably modest, but "
                "they can still take GPU time.")
    return ("They are idle right now, so the impact on the game should be small. They can take the GPU again as soon "
            "as one of them gets work.")


def _memory_text(target: Target) -> str:
    return f"{format_mib(target.vram_mib)} of GPU memory" if target.vram_mib else "no GPU memory in use right now"


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
        return f"<p>{intro}</p><ul>{rows}</ul>{small}<p>{impact}</p><p>{question}</p>"
    lines = [intro, ""]
    for t in targets:
        lines.append(f"• {_bold(_esc(t.name, markup), markup)}")
        lines.append(f"    {_small(_detail(t, markup), markup)}")
    if summary:
        lines += ["", _small(summary, markup)]
    lines += ["", impact, "", question]
    return "\n".join(lines)


def restart_message(names: Sequence[str], markup: str = "plain") -> str:
    intro = f"{_bold(TITLE, markup)}: the game has closed. Restart the containers it stopped before the game started?"
    if markup == "html":
        return f"<p>{intro}</p><ul>{''.join(f'<li>{_esc(n, markup)}</li>' for n in names)}</ul>"
    return "\n".join([intro, ""] + [f"• {_esc(n, markup)}" for n in names])


def _names(refs: Sequence[Ref]) -> List[str]:
    return [r.get("name", "") for r in refs]


def _run(command: List[str], timeout: Optional[float]) -> "subprocess.CompletedProcess":
    return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                          timeout=timeout)


class _Base:
    name = ""
    command = ""

    def __init__(self, which: Callable[[str], Optional[str]] = shutil.which):
        self._which = which

    def available(self) -> bool:
        return self._which(self.command) is not None

    def notify(self, title: str, body: str) -> None:
        _send_notification(self._which, title, body)


def _send_notification(which: Callable[[str], Optional[str]], title: str, body: str) -> None:
    """Show a passive notification through notify-send; without it the message goes to the log."""
    log(f"{title}: {body}")
    if not which("notify-send"):
        return
    try:
        _run(["notify-send", "--app-name", TITLE, title, body], INFO_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"cannot show a notification ({exc})")


class ZenityPrompter(_Base):
    name = "zenity"
    command = "zenity"

    def launch(self, launch: Launch, targets: List[Target], totals: Optional[Tuple[int, int]]) -> str:
        text = launch_message(launch, targets, totals, "pango")
        try:
            result = _run(["zenity", "--question", "--title", TITLE, "--text", text, "--ok-label", LABEL_STOP,
                           "--cancel-label", LABEL_CANCEL, "--extra-button", LABEL_ANYWAY], None)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"cannot show a dialog ({exc}); launching anyway")
            return "anyway"
        if result.returncode == 0:
            return "stop"
        if result.returncode == 1 and (result.stdout or "").strip() == LABEL_ANYWAY:
            return "anyway"
        return "cancel"

    def restart(self, refs: List[Ref]) -> Optional[bool]:
        text = restart_message(_names(refs), "pango")
        try:
            result = _run(["zenity", "--question", "--title", TITLE, "--text", text, "--ok-label", LABEL_RESTART,
                           "--cancel-label", LABEL_LEAVE], None)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"cannot show a dialog ({exc}); keeping the containers marked as stopped")
            return None
        return {0: True, 1: False}.get(result.returncode)


class KdialogPrompter(_Base):
    name = "kdialog"
    command = "kdialog"

    def launch(self, launch: Launch, targets: List[Target], totals: Optional[Tuple[int, int]]) -> str:
        text = launch_message(launch, targets, totals, "html")
        try:
            result = _run(["kdialog", "--title", TITLE, "--yes-label", LABEL_STOP, "--no-label", LABEL_ANYWAY,
                           "--cancel-label", LABEL_CANCEL, "--yesnocancel", text], None)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"cannot show a dialog ({exc}); launching anyway")
            return "anyway"
        return {0: "stop", 1: "anyway"}.get(result.returncode, "cancel")

    def restart(self, refs: List[Ref]) -> Optional[bool]:
        text = restart_message(_names(refs), "html")
        try:
            result = _run(["kdialog", "--title", TITLE, "--yes-label", LABEL_RESTART, "--no-label", LABEL_LEAVE,
                           "--yesno", text], None)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"cannot show a dialog ({exc}); keeping the containers marked as stopped")
            return None
        return {0: True, 1: False}.get(result.returncode)

    def notify(self, title: str, body: str) -> None:
        log(f"{title}: {body}")
        try:
            _run(["kdialog", "--title", title, "--passivepopup", body, "10"], INFO_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"cannot show a notification ({exc})")


class NotifyPrompter(_Base):
    """Desktop notifications. Questions are only asked when notify-send supports actions."""

    name = "notify"
    command = "notify-send"

    def __init__(self, which: Callable[[str], Optional[str]] = shutil.which):
        super().__init__(which)
        self._actions: Optional[bool] = None

    def supports_actions(self) -> bool:
        if self._actions is None:
            try:
                result = _run(["notify-send", "--help"], INFO_TIMEOUT_SECONDS)
                self._actions = "--action" in (result.stdout or "")
            except (OSError, subprocess.SubprocessError):
                self._actions = False
        return self._actions

    def _ask(self, summary: str, body: str, actions: Sequence[Tuple[str, str]]) -> Optional[str]:
        """The name of the chosen action; None when the notification was dismissed or expired."""
        command = ["notify-send", "--app-name", TITLE, "--wait", f"--expire-time={NOTIFY_EXPIRE_MS}"]
        command += [f"--action={name}={label}" for name, label in actions]
        command += [summary, body]
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
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"cannot show a notification ({exc}); launching anyway")
            return "anyway"
        return chosen or "anyway"  # an unanswered question must not strand the player

    def restart(self, refs: List[Ref]) -> Optional[bool]:
        if not self.supports_actions():
            return None
        body = restart_message(_names(refs), "plain")
        try:
            chosen = self._ask(TITLE, body, [("restart", LABEL_RESTART), ("leave", LABEL_LEAVE)])
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"cannot show a notification ({exc}); keeping the containers marked as stopped")
            return None
        return {"restart": True, "leave": False}.get(chosen) if chosen else None


class NullPrompter:
    """No dialog is possible: launches proceed and the restart question is deferred."""

    name = "none"

    def available(self) -> bool:
        return True

    def launch(self, launch: Launch, targets: List[Target], totals: Optional[Tuple[int, int]]) -> str:
        return "anyway"

    def restart(self, refs: List[Ref]) -> Optional[bool]:
        return None

    def notify(self, title: str, body: str) -> None:
        log(f"{title}: {body}")


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
