"""The state behind the settings window: a working copy of the configuration with dirty tracking, validation and
saving, plus the live views the window needs (status, events, discovered candidates, pause state, service restart).
Everything outside the change signal works without a QApplication, and the file, status directory, discovery and
command runner are injectable so tests never touch the real system."""
from __future__ import annotations

import copy
import dataclasses
import glob
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from .. import config as configmod
from .. import discovery, guard, gpu, runtimes, status as statusmod
from ..config import Config, ConfigError, TargetSpec
from ..discovery import Candidate
from ..model import Target
from ..status import Status
from ..util import log
from .qt import QtCore

SERVICE_UNIT = "gpu-game-guard.service"
RESTART_COMMAND = ["systemctl", "--user", "restart", SERVICE_UNIT]
START_COMMAND = "systemctl --user start gpu-game-guard"
# Settings that the running service reads once at start; a saved change to one of them needs a restart.
_RESTART_FIELDS = ("runtimes", "triggers", "dialog")
SCAN_POLL_MS = 30  # how often the GUI thread looks for the result of a scan
MAX_RAW_BYTES = 2 * 1024 * 1024  # the largest unusable file offered for repair in the Raw editor

Runner = Callable[[List[str]], Tuple[int, str]]
Discover = Callable[[Config], List[Candidate]]


def _default_discover(config: Config) -> List[Candidate]:
    return discovery.discover(config, runtimes.build(config.runtimes, get_config=lambda: config))


def _default_runner(argv: List[str]) -> Tuple[int, str]:
    result = gpu.run_process(argv, timeout=30)
    if result is None:
        return 127, "the command could not be run"
    return result.returncode, (result.stdout + result.stderr).strip()


def _restart_key(config: Config) -> Tuple[Any, ...]:
    return tuple(tuple(getattr(config, name)) if isinstance(getattr(config, name), list) else getattr(config, name)
                 for name in _RESTART_FIELDS)


def _load_report(path: Path, error: ConfigError) -> Tuple[List[str], str]:
    """(the problems in the file, its text) for a file that could not be loaded. The text is empty when the file
    cannot be read or is unreasonably large."""
    try:
        if path.stat().st_size > MAX_RAW_BYTES:
            return [f"The file is larger than {MAX_RAW_BYTES // 1024 // 1024} MiB."], ""
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [error.reason], ""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return [error.reason], text
    found = configmod._problems(data, strict=False)
    return (found or [error.reason]), text


class SettingsModel(QtCore.QObject):
    changed = QtCore.pyqtSignal()  # working config, saved state, status or candidates changed

    def __init__(self, path: Optional[Path] = None, status_dir: Optional[Path] = None,
                 discover: Optional[Discover] = None, runner: Optional[Runner] = None,
                 parent: Optional[QtCore.QObject] = None, threaded: bool = True):
        super().__init__(parent)
        self.path = Path(path) if path is not None else configmod.config_path()
        self.status_dir = Path(status_dir) if status_dir is not None else None
        self._discover = discover or _default_discover
        self._runner = runner or _default_runner
        self._threaded = threaded
        self.load_error = ""  # why the file could not be loaded; empty when it was
        self.load_problems: List[str] = []  # every problem found in a file that could not be loaded
        self.raw_file_text = ""  # the text of a file that could not be loaded, for repair in the Raw editor
        self.notices: List[str] = []  # what the last apply did besides saving
        self.raw_problems: List[str] = []  # problems with text typed into the Raw editor that is not yet usable
        self._base, self._stamp = self._load()
        self._working = copy.deepcopy(self._base)
        self.problems: List[str] = []
        self._refresh_problems()
        self._restart_baseline = _restart_key(self._base)
        self._candidates: Optional[List[Candidate]] = None
        self._defaults_accepted = False  # a broken file was given up on; the next save replaces it
        self.scanning = False
        self.scan_error = ""  # why the last scan found nothing; empty when it worked
        self._scan_results: "queue.Queue[Tuple[int, List[Candidate], str]]" = queue.Queue()
        self._scan_running = False
        self._scan_serial = 0
        self._scan_again = False
        self._closed = False
        self._scan_poll = QtCore.QTimer(self)
        self._scan_poll.setInterval(SCAN_POLL_MS)
        self._scan_poll.timeout.connect(self._poll_scan)

    def _load(self) -> Tuple[Config, Optional[Tuple[int, int, int]]]:
        """(the config in the file, the file's stamp from before it was read). A file that cannot be used leaves
        the problem in `load_error`, `load_problems` and `raw_file_text`, and the defaults as the config."""
        stamp = configmod.file_stamp(self.path)
        self.load_error, self.load_problems, self.raw_file_text = "", [], ""
        try:
            return configmod.load(self.path), stamp
        except ConfigError as exc:
            self.load_error = str(exc)
            self.load_problems, self.raw_file_text = _load_report(self.path, exc)
        except Exception as exc:  # an unexpected failure reading the file is treated like an unreadable file
            self.load_error = f"{self.path}: {exc}"
            self.load_problems = [str(exc) or type(exc).__name__]
        return Config(), stamp

    # state

    @property
    def config(self) -> Config:
        return self._working

    @property
    def saved(self) -> Config:
        """The configuration as it was when the file was last read or written."""
        return self._base

    @property
    def dirty(self) -> bool:
        return self._working != self._base or self._defaults_accepted or bool(self.raw_problems)

    @property
    def defaults_pending(self) -> bool:
        """True when the next apply replaces a file that could not be used."""
        return self._defaults_accepted

    def _refresh_problems(self) -> None:
        # Keys this version does not know are kept unchanged, so only the problems that make the file unusable count.
        self.problems = configmod._problems(self._working.to_json(), strict=False)

    def _replace(self, cfg: Config) -> None:
        self._working = cfg
        self._refresh_problems()
        self.changed.emit()

    def accept_defaults(self) -> None:
        """Give up on a file that could not be read: the working copy stays at the defaults, and the next apply
        replaces the file with it, keeping the unusable one as a `.broken-<time>` copy."""
        if self.load_error:
            self.load_error = ""
            self._defaults_accepted = True
            self.changed.emit()

    def set_raw_problems(self, problems: List[str]) -> None:
        """Record that the Raw editor holds text that cannot be used yet (`problems`), or that it does not (empty)."""
        problems = list(problems)
        if problems != self.raw_problems:
            was = bool(self.raw_problems)
            self.raw_problems = problems
            if was != bool(problems):
                self.changed.emit()

    def set_config(self, cfg: Config) -> None:
        if self.load_error:  # the unusable file is being replaced on purpose
            self.load_error = ""
            self._defaults_accepted = True
        self.raw_problems = []
        self._replace(copy.deepcopy(cfg))

    def update(self, **fields: Any) -> None:
        self._replace(dataclasses.replace(copy.deepcopy(self._working), **fields))

    def set_target(self, spec: TargetSpec) -> None:
        self._replace(self._working.with_target(spec))

    def remove_target(self, target_id: str) -> None:
        self._replace(self._working.without_target(target_id))

    def set_game(self, game_id: str, name: str, action: str) -> None:
        self._replace(self._working.with_game(game_id, name, action))

    def remove_game(self, game_id: str) -> None:
        updated = copy.deepcopy(self._working)
        updated.games.pop(game_id, None)
        self._replace(updated)

    def _broken_copies(self) -> List[Path]:
        return sorted(self.path.parent.glob(glob.escape(self.path.name) + ".broken-*"))

    def _config_error_problem(self, error: ConfigError) -> str:
        """The message for a save that was refused because of `error`. A file that became unusable after it was
        read says how to repair it."""
        try:
            configmod.load(self.path)
        except ConfigError as disk:
            return (f"The settings could not be saved because the file on disk has become unusable: {self.path} "
                    f"{disk.reason}. It was left as it is. Press Revert to load the file again, which discards "
                    "these edits and opens its text in the Raw config tab for repair; or open the Raw config tab, "
                    "whose text is that file, and repair it there.")
        return f"The settings could not be saved: {error.reason}. {self.path} was left as it is."

    def _replace_unusable_file(self, ours: Config) -> Config:
        """Save `ours` over a file that could not be used, and note whether the old file was kept as a copy."""
        started = time.time()
        before = set(self._broken_copies())
        existed = os.path.lexists(self.path)
        why = self._unreadable_reason()
        configmod.save(ours, self.path)

        def newer(copy_path: Path) -> bool:
            try:
                return copy_path not in before or copy_path.stat().st_mtime >= started - 2
            except OSError:
                return False

        fresh = [p for p in self._broken_copies() if newer(p)]
        if fresh:
            self.notices.append(f"The unusable settings file was kept as {fresh[-1]}.")
        elif existed:
            self.notices.append("The unusable settings file was replaced, but no backup copy of it could be made: "
                                + (f"it cannot be read ({why})." if why else "the copy could not be written next to it."))
        return ours

    def _unreadable_reason(self) -> str:
        """Why the file cannot be read, or an empty string when it can."""
        try:
            with open(self.path, "rb") as handle:
                handle.read(1)
        except OSError as exc:
            return exc.strerror or type(exc).__name__
        return ""

    def apply(self) -> List[str]:
        """Validate and save the working copy. Returns the problems; an empty list means it was saved. When the
        file changed on disk since it was read, the edits are merged into the new content first."""
        self.notices = []
        self._refresh_problems()
        problems = [f"Raw config: {p}" for p in self.raw_problems] + list(self.problems)
        if self.load_error:
            problems.insert(0, "The settings file could not be read, so nothing was saved. Fix it in the Raw config "
                               "tab, or start from the defaults.")
        if problems:
            return problems
        ours = copy.deepcopy(self._working)
        replace_file = self._defaults_accepted
        if replace_file and configmod.file_stamp(self.path) != self._stamp:
            try:  # the unusable file was repaired elsewhere in the meantime: merge into it instead of replacing it
                configmod.load(self.path)
                replace_file = False
            except ConfigError:
                pass
        try:
            if replace_file:
                merged = self._replace_unusable_file(ours)
            else:
                outcome: List[str] = []

                def mutate(disk: Config) -> Config:
                    if self._stamp is not None and not os.path.lexists(self.path):
                        outcome.append("removed")  # nothing on disk to merge with: the working copy is the file
                        return ours
                    if configmod.file_stamp(self.path) != self._stamp:
                        outcome.append("merged")
                        return configmod.merge3(self._base, ours, disk)
                    return ours

                merged = configmod.update_file(mutate, self.path)
                if outcome == ["removed"]:
                    self.notices.append("The configuration file was removed; it has been written again with "
                                        "your settings.")
                elif outcome == ["merged"]:
                    self.notices.append("The configuration changed on disk; your edits were merged.")
        except ConfigError as exc:
            return [self._config_error_problem(exc)]
        except (OSError, ValueError) as exc:
            reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
            return [f"The settings could not be saved: {reason}. {self.path} could not be written, so the changes "
                    "are still unsaved."]
        self._base = copy.deepcopy(merged)
        self._working = copy.deepcopy(merged)
        self._stamp = configmod.file_stamp(self.path)
        self._defaults_accepted = False
        self.raw_problems = []
        self._refresh_problems()
        self.changed.emit()
        return []

    def revert(self) -> None:
        """Discard every unsaved edit and read the file again."""
        self._defaults_accepted = False
        self.raw_problems = []
        self._base, self._stamp = self._load()
        self._replace(copy.deepcopy(self._base))

    def sync_with_disk(self) -> bool:
        """Read the file again when it changed on disk and there are no unsaved edits. Returns True when it did."""
        if self.dirty or configmod.file_stamp(self.path) == self._stamp:
            return False
        self._base, self._stamp = self._load()
        self._replace(copy.deepcopy(self._base))
        return True

    # discovery

    @property
    def candidates_loaded(self) -> bool:
        return self._candidates is not None

    def cached_candidates(self) -> List[Candidate]:
        """The candidates found so far; empty before the first scan finishes."""
        return list(self._candidates or [])

    def candidates(self, refresh: bool = False) -> List[Candidate]:
        """What could be paused, cached after the first lookup; `refresh` looks again. Looks in the calling thread;
        the window uses `scan`."""
        if self._candidates is None or refresh:
            self._finish_scan(*self._discover_now(copy.deepcopy(self._working)))
        return list(self._candidates or [])

    def _discover_now(self, config: Config) -> Tuple[List[Candidate], str]:
        try:
            return list(self._discover(config)), ""
        except Exception as exc:
            return [], str(exc) or type(exc).__name__

    def _finish_scan(self, found: List[Candidate], error: str) -> None:
        if error:
            log(f"settings: discovery failed ({error})")
        self._candidates = found
        self.scan_error = error
        self.scanning = False
        self.changed.emit()

    def scan(self, refresh: bool = True) -> bool:
        """Look for candidates without blocking the window: the work runs in a daemon thread whose result is
        handed back through a queue that a timer on the GUI thread reads, and `changed` fires when the scan starts
        and again when the result is in. Returns False when nothing was started (a scan is under way, `refresh`
        is false and there is a result already, or the model was shut down)."""
        if self._closed:
            return False
        if self._scan_running:
            self._scan_again = self._scan_again or refresh
            return False
        if not refresh and self._candidates is not None:
            return False
        if not self._threaded:
            self._finish_scan(*self._discover_now(copy.deepcopy(self._working)))
            return True
        self._scan_serial += 1
        serial, config, discover, results = self._scan_serial, copy.deepcopy(self._working), self._discover, self._scan_results

        def work() -> None:
            try:
                outcome: Tuple[int, List[Candidate], str] = (serial, list(discover(config)), "")
            except BaseException as exc:
                outcome = (serial, [], str(exc) or type(exc).__name__)
            results.put(outcome)

        self._scan_running = True
        self.scanning = True
        self._scan_poll.start()
        threading.Thread(target=work, name="settings-scan", daemon=True).start()
        self.changed.emit()
        return True

    def _poll_scan(self) -> None:
        """Take the result of the scan on the GUI thread once the worker has delivered it."""
        try:
            serial, found, error = self._scan_results.get_nowait()
        except queue.Empty:
            return
        self._scan_poll.stop()
        if self._closed or serial != self._scan_serial:
            return
        self._scan_running = False
        again, self._scan_again = self._scan_again, False
        self._finish_scan(found, error)
        if again:
            self.scan(True)

    def wait_scan(self, timeout: float = 10.0) -> bool:
        """Block until no scan is running and its result has been applied; True when that happened in time."""
        deadline = time.monotonic() + timeout
        while self._scan_running:
            if time.monotonic() >= deadline:
                return False
            self._poll_scan()
            if self._scan_running:
                time.sleep(0.01)
            QtCore.QCoreApplication.processEvents()
        return True

    def shutdown(self) -> None:
        """Stop following a running scan and return at once. The scan's thread is a daemon: it finishes or ends
        with the process, and its result is dropped."""
        self._closed = True
        self._scan_again = False
        self._scan_running = False
        self.scanning = False
        self._scan_poll.stop()

    # the service

    def status(self) -> Optional[Status]:
        return statusmod.read_status(self.status_dir)

    def events(self, limit: int = 100) -> List[dict]:
        return statusmod.read_events(limit, self.status_dir)

    def recent_games(self) -> List[Tuple[str, str]]:
        """(game id, title) of the games the guard intercepted, newest first, each game once."""
        found: List[Tuple[str, str]] = []
        seen = set()
        for event in reversed(self.events(300)):
            game_id = event.get("game")
            if event.get("kind") != "intercept" or not isinstance(game_id, str) or not game_id or game_id in seen:
                continue
            seen.add(game_id)
            found.append((game_id, self._title_for(game_id, event)))
        return found

    def _title_for(self, game_id: str, event: dict) -> str:
        title = event.get("title")
        if isinstance(title, str) and title:
            return title
        message = event.get("message")
        if isinstance(message, str) and " started while " in message:
            head = message.split(" started while ", 1)[0].strip()
            if head:
                return head
        rule = self._working.games.get(game_id)
        return rule.name if rule is not None and rule.name else game_id

    def pause(self, seconds: Optional[float] = None) -> None:
        statusmod.pause(seconds, self.status_dir)
        self.changed.emit()

    def resume(self) -> None:
        statusmod.resume(self.status_dir)
        self.changed.emit()

    def paused(self) -> Tuple[bool, Optional[float]]:
        return statusmod.paused(self.status_dir)

    def restart_needed(self) -> bool:
        """True when runtimes, triggers or the dialog were saved with a value the running service has not seen."""
        return _restart_key(self._base) != self._restart_baseline

    def restart_service(self) -> Tuple[bool, str]:
        try:
            code, output = self._runner(list(RESTART_COMMAND))
        except Exception as exc:
            code, output = 1, str(exc)
        ok = code == 0
        if ok:
            self._restart_baseline = _restart_key(self._base)
            self.changed.emit()
        message = output.strip() or ("The service was restarted." if ok else f"systemctl exited with status {code}.")
        return ok, message

    def plan_for(self, game_id: str, targets: Sequence[Target], can_ask: bool = True) -> guard.Plan:
        return guard.plan(self._working, game_id, targets, can_ask, paused=self.paused()[0])

    def effective_action_for_game(self, game_id: str) -> str:
        return self._working.action_for(game_id)


# What the service is doing, shared by the header pill and the Basic status card.
STATE_WATCHING, STATE_PAUSED, STATE_OFF, STATE_RESTART, STATE_DISABLED = (
    "watching", "paused", "not-running", "restart-pending", "disabled")
PILL_TEXT = {STATE_WATCHING: "Watching", STATE_PAUSED: "Paused", STATE_OFF: "Not running",
             STATE_RESTART: "Restart pending", STATE_DISABLED: "Switched off"}
PILL_KIND = {STATE_WATCHING: "ok", STATE_PAUSED: "warn", STATE_OFF: "danger", STATE_RESTART: "warn",
             STATE_DISABLED: "muted"}


def service_state(model: SettingsModel, status: Optional[Status] = None,
                  now: Optional[float] = None) -> Tuple[str, Optional[float]]:
    """(state, pause end) for the service as the model sees it. The pause file is read directly, so a pause
    shows at once instead of after the service's next pass. `pause end` is None for a pause with no end. A pause
    outranks a pending restart, so every view that shows the state agrees on one answer."""
    status = model.status() if status is None else status
    if status is None:
        return STATE_OFF, None
    is_paused, until = statusmod.paused(model.status_dir, now=now)
    if is_paused or status.paused:
        return STATE_PAUSED, until if is_paused else (None if status.paused_until in (None, float("inf"))
                                                      else status.paused_until)
    if model.restart_needed() or status.state == "restart-pending":
        return STATE_RESTART, None
    if not model.saved.enabled or status.state == "disabled":
        return STATE_DISABLED, None
    return STATE_WATCHING, None

