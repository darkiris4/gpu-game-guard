"""Command line: run the watcher, diagnose the setup, and manage the config file."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, config as configmod, dialogs, gpu, runtimes, triggers
from .guard import Guard
from .state import State
from .util import log

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
EXIT_CONFIG = 78  # EX_CONFIG: `run` refuses to start on an invalid config
EXIT_INTERRUPTED = 130


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
    commands.add_parser("check", parents=[common], help="show what the guard would see and use on this system")
    config_parser = commands.add_parser("config", parents=[common], help="inspect or create the config file")
    config_parser.add_argument("action", choices=["path", "show", "validate", "init"])
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

    def current(self):
        cfg = self.watcher.current()
        dialogs.HEAVY_VRAM_MIB = cfg.heavy_vram_mib
        return cfg


def command_run(args) -> int:
    cfg, problem = read_config(args.config)
    if cfg is None:
        log(f"cannot start: {args.config}: {problem}")
        return EXIT_CONFIG
    watcher = LiveConfig(configmod.ConfigWatcher(args.config))
    active_triggers = triggers.build(cfg.triggers)
    active_runtimes = []
    for runtime in runtimes.build(cfg.runtimes):
        if runtime.installed():
            active_runtimes.append(runtime)
        else:
            log(f"{runtime.name} is not installed; skipped")
    prompter = dialogs.choose(cfg.dialog)
    log(f"gpu-game-guard {__version__}: triggers {[t.name for t in active_triggers]}, "
        f"runtimes {[r.name for r in active_runtimes]}, dialogs {prompter.name}")
    Guard(watcher, active_triggers, active_runtimes, prompter, State(), gpu_totals=gpu.totals).run()
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
    found = runtimes.build(cfg.runtimes)
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
    problems = configmod.validate(data)
    for problem in problems:
        print(f"{path}: {problem}", file=sys.stderr)
    print("config is valid" if not problems else f"{len(problems)} problem(s) found")
    return EXIT_FAILED if problems else EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    if not hasattr(args, "config"):
        args.config = configmod.config_path()
    command = args.command or "run"
    handlers = {"run": command_run, "check": command_check, "config": command_config}
    try:
        return handlers[command](args)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except OSError as exc:
        print(f"gpu-game-guard: {command}: {exc}", file=sys.stderr)
        return EXIT_FAILED


def entry() -> None:
    sys.exit(main())
