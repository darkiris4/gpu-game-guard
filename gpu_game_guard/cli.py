"""Command line: run the watcher, diagnose the setup, and manage the config file."""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__, config as configmod, dialogs, gpu, runtimes, status as statusmod, triggers
from .guard import Guard
from .state import State
from .util import log

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
EXIT_CONFIG = 78  # EX_CONFIG: `run` refuses to start on an invalid config
EXIT_INTERRUPTED = 130
QT_MISSING = "Install python3-pyqt5 (sudo apt install python3-pyqt5)"
_DURATION = re.compile(r"([0-9]+(?:\.[0-9]+)?)([smhd])\Z")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MAX_PAUSE_DAYS = 365
MAX_PAUSE_SECONDS = MAX_PAUSE_DAYS * 86400


def parse_duration(text: str) -> float:
    """Seconds in a duration such as 90s, 30m, 2h or 1d."""
    match = _DURATION.match(text.strip().lower())
    if not match or (len(match.group(1)) <= 40 and float(match.group(1)) <= 0):
        raise argparse.ArgumentTypeError(f"{text[:40]!r} is not a duration; use a number and s, m, h or d (for example 30m)")
    digits = match.group(1)
    seconds = float(digits) * _UNIT_SECONDS[match.group(2)] if len(digits) <= 40 else math.inf
    if not math.isfinite(seconds) or seconds > MAX_PAUSE_SECONDS:
        shown = text.strip() if len(text.strip()) <= 20 else text.strip()[:17] + "..."
        raise argparse.ArgumentTypeError(f"{shown!r} is too long; the longest pause is {MAX_PAUSE_DAYS} days")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    # `--config` is accepted before and after the subcommand. Both parsers default to SUPPRESS so a value
    # given in either place survives; `main` applies the real default when neither set it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, metavar="PATH", default=argparse.SUPPRESS,
                        help=f"config file (default: {configmod.config_path()})")
    parser = argparse.ArgumentParser(
        prog="gpu-game-guard", parents=[common],
        description="Asks before a game starts while GPU workloads are running, and offers to start them again afterwards.")
    parser.add_argument("--version", action="version", version=f"gpu-game-guard {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", parents=[common], help="watch for game launches (the default)")
    commands.add_parser("check", parents=[common],
                        help="show what the guard would see and use on this system, including configured targets "
                             "and whether the service is running")
    config_parser = commands.add_parser("config", parents=[common], help="inspect or create the config file")
    config_parser.add_argument("action", choices=["path", "show", "validate", "init"],
                               help="path: print the location; show: print the effective config as JSON; "
                                    "validate: report problems; init: write the defaults")
    pause_parser = commands.add_parser("pause", parents=[common],
                                       help="stop intercepting launches for a while (launches start untouched)")
    pause_parser.add_argument("--for", dest="duration", type=parse_duration, metavar="DURATION", default=None,
                              help="how long, such as 90s, 30m, 2h or 1d, at most 365 days "
                                   "(default: until 'resume')")
    commands.add_parser("resume", parents=[common], help="end a pause")
    status_parser = commands.add_parser("status", parents=[common],
                                        help="show what the running service is doing (exit 1 when it is not running)")
    status_parser.add_argument("--json", action="store_true", help="print the status as JSON")
    state_parser = commands.add_parser("state", parents=[common],
                                       help="inspect or drop the list of targets the guard stopped and has not started again")
    state_parser.add_argument("action", choices=["clear"],
                              help="clear: print the stopped targets and forget them without starting them")
    targets_parser = commands.add_parser("targets", parents=[common], help="list or change the configured host targets")
    targets_parser.add_argument("action", choices=["list", "enable", "disable", "remove"],
                                help="list the targets, or enable, disable or remove the target ID")
    targets_parser.add_argument("id", nargs="?", metavar="ID", help="the target's id (not used by list)")
    presets_parser = commands.add_parser("presets", parents=[common], help="list or add ready-made targets")
    presets_parser.add_argument("action", choices=["list", "add"], help="list the presets, or add the preset ID")
    presets_parser.add_argument("id", nargs="?", metavar="ID", help="the preset's id (only for add)")
    commands.add_parser("discover", parents=[common],
                        help="list what uses the GPU now and could be managed, safest first")
    polkit_parser = commands.add_parser("polkit", parents=[common],
                                        help="let a user manage systemd units without a password (allow and "
                                             "revoke need root)")
    polkit_parser.add_argument("action", choices=["allow", "revoke", "status"])
    polkit_parser.add_argument("units", nargs="+", metavar="UNIT", help="unit names ending in .service")
    polkit_parser.add_argument("--user", default=None, help="the user the rule applies to (default: the sudo user)")
    commands.add_parser("settings", parents=[common], help="open the settings window (needs python3-pyqt5)")
    commands.add_parser("tray", parents=[common], help="run the tray icon (needs python3-pyqt5)")
    prompt_parser = commands.add_parser("prompt", parents=[common],
                                        help="internal: show a dialog described by JSON on standard input")
    prompt_parser.add_argument("kind", choices=["launch", "restart"])
    return parser


def read_config(path: Path):
    """Return (config, problem). `problem` is the reason without the path, and None when the file is
    absent or valid."""
    try:
        return configmod.load(path), None
    except configmod.ConfigError as exc:
        reason = getattr(exc, "reason", None)
        if reason is None:
            reason = str(exc)
            prefix = f"{path}: "
            reason = reason[len(prefix):] if reason.startswith(prefix) else reason
        return None, reason


class LiveConfig:
    """Wraps a config watcher so settings that live outside the guard follow the file as it changes."""

    def __init__(self, watcher):
        self.watcher = watcher

    @property
    def path(self):
        return getattr(self.watcher, "path", None)

    @property
    def error(self):
        return getattr(self.watcher, "error", "")

    def current(self):
        cfg = self.watcher.current()
        dialogs.HEAVY_VRAM_MIB = cfg.heavy_vram_mib
        dialogs.REMEMBER_CHOICES = cfg.remember_choices
        return cfg


def command_run(args) -> int:
    cfg, problem = read_config(args.config)
    if cfg is None:
        log(f"cannot start: {args.config}: {problem}")
        return EXIT_CONFIG
    watcher = LiveConfig(configmod.ConfigWatcher(args.config))
    active_triggers = triggers.build(cfg.triggers)
    active_runtimes = []
    for runtime in runtimes.build(cfg.runtimes, get_config=watcher.current):
        if runtime.installed():
            active_runtimes.append(runtime)
        else:
            log(f"{runtime.name} is not installed; skipped")
    prompter = dialogs.choose(cfg.dialog)
    log(f"gpu-game-guard {__version__}: triggers {[t.name for t in active_triggers]}, "
        f"runtimes {[r.name for r in active_runtimes]}, dialogs {prompter.name}")
    guard = Guard(watcher, active_triggers, active_runtimes, prompter, State(), gpu_totals=gpu.totals,
                  status_dir=None, dialog_name=prompter.name)

    def terminate(signum, frame):
        raise SystemExit(EXIT_OK)  # unwinds through `Guard.run`, which removes the status file

    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        try:
            number = getattr(signal, name)
            previous[number] = signal.signal(number, terminate)
        except (AttributeError, ValueError, OSError):
            pass  # not available on this platform, or not the main thread
    try:
        guard.run()
    finally:
        for number, handler in previous.items():
            try:
                signal.signal(number, handler)
            except (ValueError, OSError, TypeError):
                pass
    return EXIT_OK


def command_check(args) -> int:
    cfg, problem = read_config(args.config)
    print(f"gpu-game-guard {__version__}")
    if cfg is None:
        print(f"config:    {args.config}  INVALID: {problem}")
        cfg = configmod.Config()
        ok = False
    else:
        state = "present" if args.config.exists() else "not created, using defaults"
        print(f"config:    {args.config}  ({state})")
        ok = True
    prompter = dialogs.choose(cfg.dialog)
    can_ask = bool(getattr(prompter, "can_ask", True))
    note = "" if can_ask else "  (this dialog backend cannot ask: games start without being stopped or restarted)"
    print(f"dialogs:   {prompter.name}  (preference: {cfg.dialog}){note}")
    found = runtimes.build(cfg.runtimes, get_config=lambda: cfg)
    usable = [runtime for runtime in found if runtime.installed() and runtime.available()]

    def describe(runtime) -> str:
        if not runtime.installed():
            return f"{runtime.name} (not installed)"
        return f"{runtime.name} ({'available' if runtime in usable else 'installed, but not answering'})"

    print("runtimes:  " + (", ".join(describe(runtime) for runtime in found) or "none"))
    active = triggers.build(cfg.triggers)
    print("triggers:  " + (", ".join(t.name for t in active) or "none"))
    launches = [launch for trigger in active for launch in trigger.scan()]
    print("launches running now:" + (" none" if not launches else ""))
    for launch in launches:
        print(f"  {launch.game_id}  {launch.title}")
    targets = [t for runtime in usable for t in runtime.running_gpu_targets()]
    print("GPU containers running now:" + (" none" if not targets else ""))
    for target in targets:
        memory = "unknown" if target.vram_mib is None else f"{target.vram_mib} MiB"
        ignored = "  (ignored by config)" if cfg.is_ignored(target.name) else ""
        print(f"  {target.name}  [{target.runtime}]  {target.image}  GPU memory: {memory}{ignored}")
    totals = gpu.totals()
    print("GPU memory overall: " + ("unknown" if totals is None else f"{totals[0]} of {totals[1]} MiB"))
    running_keys = set()
    for runtime in usable:
        try:
            running_keys.update((t.runtime, t.extra.get("id") if t.extra else None) for t in runtime.running_gpu_targets()
                                if t.kind != "container")
        except Exception as exc:
            log(f"runtime {runtime.name} failed: {exc!r}")
    print("configured targets:" + (" none" if not cfg.targets else ""))
    for spec in cfg.targets:
        if not spec.enabled:
            state = "disabled"
        elif (spec.kind, spec.id) in running_keys:
            state = "running"
        else:
            state = "not running"
        print(f"  {spec.id}  [{spec.kind}]  {spec.label or spec.id}  {state}")
    view = statusmod.read_status()
    if view is None:
        print("service:   not running (no recent status)" + ("; a pause is set" if statusmod.paused()[0] else ""))
    else:
        print(f"service:   running (pid {view.pid}), state {view.state}" + (", paused" if view.paused else ""))
    defaults = configmod.Config()
    changed = [key for key in ("runtimes", "triggers", "dialog") if getattr(cfg, key) != getattr(defaults, key)]
    if changed:
        print(f"note: {', '.join(changed)} differ from the defaults; these are read once when the service "
              "starts, so a change needs: systemctl --user restart gpu-game-guard")
    return EXIT_OK if ok else EXIT_FAILED


def command_config(args) -> int:
    path: Path = args.config
    if args.action == "path":
        print(path)
        return EXIT_OK
    if args.action == "init":
        if path.exists():
            print(f"{path} already exists; not overwriting", file=sys.stderr)
            return EXIT_FAILED
        try:
            configmod.save(configmod.Config(), path)
        except OSError as exc:
            print(f"cannot write {path}: {exc.strerror or exc}", file=sys.stderr)
            return EXIT_FAILED
        print(f"wrote defaults to {path}")
        return EXIT_OK
    if args.action == "show":
        cfg, problem = read_config(path)
        if cfg is None:
            print(f"{path}: {problem}", file=sys.stderr)
            return EXIT_FAILED
        print(json.dumps(cfg.to_json(), indent=2, sort_keys=True))
        return EXIT_OK
    if not path.exists():
        print(f"{path} does not exist; the defaults are in use")
        return EXIT_OK
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        print(f"{path}: cannot be read as JSON: {detail}", file=sys.stderr)
        return EXIT_FAILED
    try:
        problems = configmod.validate(data)
    except configmod.ConfigError as exc:
        problems = [exc.reason]
    for problem in problems:
        print(f"{path}: {problem}", file=sys.stderr)
    if any("unknown key" in problem for problem in problems):
        print("unknown keys are reported as likely typos; this version keeps them in the file unchanged "
              "when it saves", file=sys.stderr)
    print("config is valid" if not problems else f"{len(problems)} problem(s) found")
    return EXIT_FAILED if problems else EXIT_OK


def _clock(wall: float) -> str:
    return time.strftime("%H:%M on %Y-%m-%d", time.localtime(wall))


def command_pause(args) -> int:
    duration = args.duration
    if duration is not None and (not math.isfinite(duration) or not 0 < duration <= MAX_PAUSE_SECONDS):
        print(f"gpu-game-guard: the pause must be longer than zero and at most {MAX_PAUSE_DAYS} days", file=sys.stderr)
        return EXIT_USAGE
    statusmod.pause(args.duration)
    if args.duration is None:
        print("paused until resumed (gpu-game-guard resume)")
    else:
        print(f"paused until {_clock(time.time() + args.duration)}")
    return EXIT_OK


def command_resume(args) -> int:
    statusmod.resume()
    print("resumed")
    return EXIT_OK


def command_status(args) -> int:
    view = statusmod.read_status()
    paused, until = statusmod.paused()
    if args.json:
        if view is None:
            data: dict = {"running": False, "paused": paused, "paused_until": until}
        else:
            data = dataclasses.asdict(view)
            if data["paused_until"] is not None and data["paused_until"] == float("inf"):
                data["paused_until"] = None
            data["running"] = True
        print(json.dumps(data, indent=2, sort_keys=True))
        return EXIT_OK if view is not None else EXIT_FAILED
    if view is None:
        print("gpu-game-guard is not running (no recent status)")
        if paused:
            print("a pause is set: " + ("until resumed" if until is None else f"until {_clock(until)}"))
        return EXIT_FAILED
    print(f"gpu-game-guard {view.version} is running (pid {view.pid})")
    print(f"state:    {view.state}")
    if view.paused:
        end = view.paused_until
        print("paused:   " + ("until resumed" if end is None or end == float("inf") else f"until {_clock(end)}"))
    if view.game:
        print(f"game:     {view.game.get('title') or view.game.get('id')} ({view.game.get('id')})")
    if view.stopped:
        print("stopped:  " + ", ".join(str(ref.get("name")) for ref in view.stopped))
    print(f"dialogs:  {view.dialog or 'unknown'}" + ("" if view.can_ask else " (cannot ask)"))
    print(f"targets:  {view.targets} configured")
    print("config:   " + ("ok" if view.config_ok else f"INVALID: {view.config_error}; the previous settings are in use"))
    return EXIT_OK


def command_state(args) -> int:
    """`state clear`: forget the stopped targets without starting them. Providers hear of it through `release`."""
    state = State()
    if not state.stopped:
        print("nothing is recorded as stopped")
        return EXIT_OK
    refs = [dict(ref) for ref in state.stopped]
    print("stopped targets dropped from the list (they were not started): " + ", ".join(str(ref["name"]) for ref in refs))
    if statusmod.read_status() is not None:
        print("note: the service is running; restart it (systemctl --user restart gpu-game-guard) so it forgets them too",
              file=sys.stderr)
    cfg, _problem = read_config(args.config)
    cfg = cfg or configmod.Config()
    wanted = {ref["runtime"] for ref in refs}
    try:
        found = [runtime for runtime in runtimes.build(cfg.runtimes, get_config=lambda: cfg) if runtime.name in wanted]
    except Exception as exc:
        log(f"cannot reach the providers to release the targets: {exc!r}")
        found = []
    for runtime in found:
        hook = getattr(runtime, "release", None)
        if not callable(hook):
            continue
        try:
            hook([ref for ref in refs if ref["runtime"] == runtime.name])
        except Exception as exc:
            log(f"release through {runtime.name} failed: {exc!r}")
    state.clear()
    return EXIT_OK


def _target_summary(spec) -> str:
    if spec.kind == "service":
        return f"{spec.unit} ({spec.scope})"
    if spec.kind == "command":
        return " ".join(spec.stop or [])[:60]
    return f"match {spec.match}" + ("" if spec.restart else ", no restart")


def _load_for_edit(args):
    cfg, problem = read_config(args.config)
    if cfg is None:
        print(f"{args.config}: {problem}", file=sys.stderr)
    return cfg


class _Refused(Exception):
    """An edit that does not apply to the file as it is now; the message says why."""


def _edit(args, mutate) -> bool:
    """Apply `mutate` to the config file under its lock; prints the problem and returns False on failure."""
    try:
        configmod.update_file(mutate, args.config)
    except _Refused as exc:
        print(str(exc), file=sys.stderr)
    except configmod.ConfigError as exc:
        print(f"{args.config}: {exc.reason}", file=sys.stderr)
    except OSError as exc:
        print(f"cannot write {args.config}: {exc.strerror or exc}", file=sys.stderr)
    else:
        return True
    return False


def command_targets(args) -> int:
    cfg = _load_for_edit(args)
    if cfg is None:
        return EXIT_FAILED
    if args.action == "list":
        if not cfg.targets:
            print("no targets configured")
        for spec in cfg.targets:
            print(f"{spec.id}  [{spec.kind}]  {'enabled' if spec.enabled else 'disabled'}  "
                  f"{spec.label or spec.id}  {_target_summary(spec)}")
        return EXIT_OK
    if not args.id:
        print(f"targets {args.action} needs a target id", file=sys.stderr)
        return EXIT_USAGE
    if cfg.target(args.id) is None:
        print(f"no target with id {args.id!r}", file=sys.stderr)
        return EXIT_FAILED

    def mutate(current):
        spec = current.target(args.id)
        if spec is None:
            raise _Refused(f"no target with id {args.id!r}")
        if args.action == "remove":
            return current.without_target(args.id)
        return current.with_target(dataclasses.replace(spec, enabled=args.action == "enable"))

    if not _edit(args, mutate):
        return EXIT_FAILED
    print({"remove": "removed", "enable": "enabled", "disable": "disabled"}[args.action] + f" {args.id}")
    return EXIT_OK


def command_presets(args) -> int:
    from . import presets
    cfg = _load_for_edit(args)
    if cfg is None:
        return EXIT_FAILED
    available = presets.load_presets()
    if args.action == "list":
        for preset in available:
            marks = []
            if cfg.target(preset.spec.get("id", preset.id)) is not None:
                marks.append("added")
            marks.append("applies here" if presets.applicable(preset) else "not found here")
            print(f"{preset.id}  {preset.label}  ({', '.join(marks)})")
            print(f"    {preset.description}")
        return EXIT_OK
    if not args.id:
        print("presets add needs a preset id", file=sys.stderr)
        return EXIT_USAGE
    preset = next((p for p in available if p.id == args.id), None)
    if preset is None:
        print(f"no preset with id {args.id!r}; see 'gpu-game-guard presets list'", file=sys.stderr)
        return EXIT_FAILED
    spec_data = dict(preset.spec)
    problems = configmod.validate({"targets": [spec_data]})
    if problems:
        print(f"preset {preset.id} is invalid: {'; '.join(problems)}", file=sys.stderr)
        return EXIT_FAILED
    spec = configmod.TargetSpec.from_json(spec_data)
    if cfg.target(spec.id) is not None:
        print(f"target {spec.id!r} is already configured", file=sys.stderr)
        return EXIT_FAILED
    if not presets.applicable(preset):
        print(f"note: what {preset.label!r} manages was not found on this system", file=sys.stderr)

    def mutate(current):
        if current.target(spec.id) is not None:
            raise _Refused(f"target {spec.id!r} is already configured")
        return current.with_target(spec)

    if not _edit(args, mutate):
        return EXIT_FAILED
    print(f"added {spec.id}")
    if spec.kind not in cfg.runtimes:
        fixed = json.dumps(list(cfg.runtimes) + [spec.kind])
        print(f"warning: the {spec.kind!r} provider is not in \"runtimes\", so {spec.id!r} is never used; in "
              f"{args.config} set \"runtimes\": {fixed} and restart the service "
              "(systemctl --user restart gpu-game-guard)", file=sys.stderr)
    if spec.kind == "service" and spec.scope == "system":
        print(f"stopping a system service needs authorisation: sudo gpu-game-guard polkit allow {spec.unit}")
    return EXIT_OK


def command_discover(args) -> int:
    from . import discovery
    cfg, problem = read_config(args.config)
    if cfg is None:
        print(f"{args.config}: {problem}", file=sys.stderr)
        return EXIT_FAILED
    usable = [r for r in runtimes.build(cfg.runtimes, get_config=lambda: cfg) if r.installed() and r.available()]
    found = discovery.discover(cfg, usable)
    if not found:
        print("nothing using the GPU was found")
        return EXIT_OK
    for item in found:
        memory = "" if item.vram_mib is None else f"  {item.vram_mib} MiB"
        print(f"{item.key}  [{item.kind}, risk {item.risk}]  {item.label}{memory}"
              + ("  (already managed)" if item.managed else ""))
        if item.detail:
            print(f"    {item.detail}")
        if item.note:
            print(f"    {item.note}")
        if item.spec and not item.managed:
            print(f"    target: {json.dumps(item.spec, sort_keys=True)}")
    return EXIT_OK


def command_polkit(args) -> int:
    from . import polkit
    error = getattr(polkit, "PolkitError", ())
    try:
        if args.action == "status":
            for unit, allowed in polkit.status(args.units, args.user).items():
                print(f"{unit}: {'allowed' if allowed else 'not allowed'}")
            return EXIT_OK
        change = polkit.allow if args.action == "allow" else polkit.revoke
        path = change(args.units, args.user)
    except error as exc:
        print(f"gpu-game-guard polkit: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"{'updated' if args.action == 'allow' else 'revoked in'} {path}")
    return EXIT_OK


def _qt_ready() -> bool:
    """True when PyQt5 can be used; otherwise the install hint is printed."""
    try:
        from .ui.qt import available
        if available():
            return True
    except ImportError:
        pass
    print(QT_MISSING, file=sys.stderr)
    return False


def command_settings(args) -> int:
    if not _qt_ready():
        return EXIT_FAILED
    try:
        from .ui.settings import run
    except ImportError:
        print(QT_MISSING, file=sys.stderr)
        return EXIT_FAILED
    if getattr(args, "config_given", False):
        return int(run(config_path=args.config))
    return int(run())


def command_tray(args) -> int:
    if not _qt_ready():
        return EXIT_FAILED
    try:
        from .ui.tray import run
    except ImportError:
        print(QT_MISSING, file=sys.stderr)
        return EXIT_FAILED
    return int(run())


def command_prompt(args) -> int:
    if not _qt_ready():
        return EXIT_FAILED
    try:
        from .ui.prompt import run_prompt
    except ImportError:
        print(QT_MISSING, file=sys.stderr)
        return EXIT_FAILED
    return int(run_prompt([args.kind], sys.stdin, sys.stdout))


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    args.config_given = hasattr(args, "config")
    if not args.config_given:
        args.config = configmod.config_path()
    command = args.command or "run"
    handlers = {
        "run": command_run, "check": command_check, "config": command_config, "pause": command_pause,
        "resume": command_resume, "status": command_status, "state": command_state, "targets": command_targets,
        "presets": command_presets, "discover": command_discover, "polkit": command_polkit,
        "settings": command_settings, "tray": command_tray, "prompt": command_prompt,
    }
    try:
        return handlers[command](args)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except OSError as exc:
        print(f"gpu-game-guard: {command}: {exc}", file=sys.stderr)
        return EXIT_FAILED


def entry() -> None:
    sys.exit(main())
