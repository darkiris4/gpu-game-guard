"""A polkit rule that lets a user manage chosen systemd units without a password prompt."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import pwd
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence

RULES_DIR = Path("/etc/polkit-1/rules.d")
RULE_NAME = "60-gpu-game-guard.rules"
LOCK_NAME = ".60-gpu-game-guard.lock"  # not a *.rules file, so polkit never reads it
VERBS = ("start", "stop", "restart")
ACTION = "org.freedesktop.systemd1.manage-units"
_UNIT = re.compile(r"(?!-)[A-Za-z0-9:_.@-]+\.service")
_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\$?")
_GRANTS = re.compile(r"^\s*var grants = (\{.*\});$", re.MULTILINE)


class PolkitError(Exception):
    """A problem to show the user as one line."""


def _check_units(units: Sequence[str]) -> List[str]:
    clean = []
    for unit in units:
        if not isinstance(unit, str) or not _UNIT.fullmatch(unit):
            raise PolkitError(f"{unit!r} is not a valid unit name (letters, digits and : _ . @ - ending in .service)")
        if unit not in clean:
            clean.append(unit)
    return clean


def _check_user(user: str) -> str:
    if not isinstance(user, str) or not _USER.fullmatch(user):
        raise PolkitError(f"{user!r} is not a valid user name")
    return user


def _render(grants: Mapping[str, Sequence[str]]) -> str:
    table = json.dumps({user: sorted(units) for user, units in sorted(grants.items())}, sort_keys=True)
    return (
        "// Managed by gpu-game-guard: 'gpu-game-guard polkit allow|revoke|status UNIT...'.\n"
        "// Lets the listed users start, stop and restart the listed systemd units without a password,\n"
        "// when they are logged in locally with an active session.\n"
        "(function() {\n"
        f"    var grants = {table};\n"
        f"    var verbs = {json.dumps(list(VERBS))};\n"
        "    polkit.addRule(function(action, subject) {\n"
        f'        if (action.id !== "{ACTION}") {{\n'
        "            return polkit.Result.NOT_HANDLED;\n"
        "        }\n"
        "        if (!subject.local || !subject.active) {\n"
        "            return polkit.Result.NOT_HANDLED;\n"
        "        }\n"
        "        if (!Object.prototype.hasOwnProperty.call(grants, subject.user)) {\n"
        "            return polkit.Result.NOT_HANDLED;\n"
        "        }\n"
        '        if (verbs.indexOf(action.lookup("verb")) === -1) {\n'
        "            return polkit.Result.NOT_HANDLED;\n"
        "        }\n"
        '        if (grants[subject.user].indexOf(action.lookup("unit")) !== -1) {\n'
        "            return polkit.Result.YES;\n"
        "        }\n"
        "        return polkit.Result.NOT_HANDLED;\n"
        "    });\n"
        "})();\n"
    )


def render_rule(units: Sequence[str], user: str) -> str:
    """The rule text allowing `user` to manage exactly `units`."""
    return _render({_check_user(user): _check_units(units)})


def _rule_path(rules_dir: Path) -> Path:
    return Path(rules_dir) / RULE_NAME


def _read_grants(path: Path) -> Dict[str, List[str]]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise PolkitError(f"cannot read {path}: {exc}")
    match = _GRANTS.search(text)
    try:
        data = json.loads(match.group(1)) if match else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise PolkitError(f"{path} was not written by gpu-game-guard; not changing it")
    return {str(user): [unit for unit in units if isinstance(unit, str)]
            for user, units in data.items() if isinstance(units, list)}


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _locked(rules_dir: Path) -> Iterator[None]:
    """Hold an exclusive lock on a lock file in the rules directory, so concurrent changes do not lose updates."""
    lock_path = Path(rules_dir) / LOCK_NAME
    try:
        handle = open(lock_path, "a")
    except OSError as exc:
        raise PolkitError(f"cannot open {lock_path}: {exc}")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError as exc:
            raise PolkitError(f"cannot lock {lock_path}: {exc}")
        yield
    finally:
        handle.close()


def _require_root(euid: Optional[int]) -> None:
    if (os.geteuid() if euid is None else euid) != 0:
        raise PolkitError("changing polkit rules needs root: run it with sudo, for example "
                          "'sudo gpu-game-guard polkit allow UNIT --user NAME'")


def _default_user(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    user = env.get("SUDO_USER") or env.get("USER") or env.get("LOGNAME") or ""
    if not user or user == "root":
        raise PolkitError("cannot tell which user to allow; pass --user NAME")
    return user


def allow(units: Sequence[str], user: Optional[str] = None, rules_dir: Path = RULES_DIR,
          euid: Optional[int] = None, environ: Optional[Mapping[str, str]] = None) -> Path:
    """Add `units` to what `user` may manage and rewrite the rule file. Needs root."""
    wanted = _check_units(units)
    if not wanted:
        raise PolkitError("name at least one unit")
    _require_root(euid)
    user = _check_user(user or _default_user(environ))
    if Path(rules_dir) == RULES_DIR:  # a real run: a rule for a name that does not exist would be a trap
        try:
            pwd.getpwnam(user)
        except KeyError:
            raise PolkitError(f"{user!r} is not a user on this system")
    if not Path(rules_dir).is_dir():
        raise PolkitError(f"{rules_dir} does not exist; is polkit installed?")
    path = _rule_path(rules_dir)
    with _locked(rules_dir):
        grants = _read_grants(path)
        grants[user] = sorted(set(grants.get(user, [])) | set(wanted))
        try:
            _write_atomic(path, _render(grants))
        except OSError as exc:
            raise PolkitError(f"cannot write {path}: {exc}")
    return path


def revoke(units: Sequence[str], user: Optional[str] = None, rules_dir: Path = RULES_DIR,
           euid: Optional[int] = None, environ: Optional[Mapping[str, str]] = None) -> Path:
    """Remove `units` from what `user` may manage; the file is deleted when nothing remains. Needs root."""
    wanted = _check_units(units)
    if not wanted:
        raise PolkitError("name at least one unit")
    _require_root(euid)
    user = _check_user(user or _default_user(environ))
    path = _rule_path(rules_dir)
    if not Path(rules_dir).is_dir():
        return path  # nothing was ever granted here
    with _locked(rules_dir):
        grants = _read_grants(path)
        if user in grants:
            grants[user] = [unit for unit in grants[user] if unit not in wanted]
        grants = {name: listed for name, listed in grants.items() if listed}
        try:
            if grants:
                _write_atomic(path, _render(grants))
            elif path.exists():
                path.unlink()
        except OSError as exc:
            raise PolkitError(f"cannot change {path}: {exc}")
    return path


def status(units: Sequence[str], user: Optional[str] = None, rules_dir: Path = RULES_DIR,
           environ: Optional[Mapping[str, str]] = None) -> Dict[str, bool]:
    """Whether each unit is currently allowed for `user` by the rule file. Needs no privileges."""
    wanted = _check_units(units)
    user = _check_user(user or _default_user(environ))
    allowed = _read_grants(_rule_path(rules_dir)).get(user, [])
    return {unit: unit in allowed for unit in wanted}


def main(argv: Optional[Sequence[str]] = None, rules_dir: Path = RULES_DIR, euid: Optional[int] = None,
         environ: Optional[Mapping[str, str]] = None) -> int:
    """`polkit allow|revoke|status UNIT... [--user NAME]`. Returns 0 on success, 1 on a refused or failed change,
    2 on bad usage."""
    parser = argparse.ArgumentParser(prog="gpu-game-guard polkit", description=__doc__)
    parser.add_argument("action", choices=("allow", "revoke", "status"))
    parser.add_argument("units", nargs="+", metavar="UNIT")
    parser.add_argument("--user", help="the user the rule applies to (default: the user who ran sudo)")
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        if args.action == "status":
            for unit, allowed in status(args.units, args.user, rules_dir, environ).items():
                print(f"{unit}: {'allowed' if allowed else 'not allowed'}")
            return 0
        if args.action == "allow":
            path = allow(args.units, args.user, rules_dir, euid, environ)
            print(f"Updated {path}")
            return 0
        user = _check_user(args.user or _default_user(environ))
        granted = [unit for unit, allowed in status(args.units, user, rules_dir, environ).items() if allowed]
        path = revoke(args.units, user, rules_dir, euid, environ)
    except PolkitError as exc:
        print(f"gpu-game-guard polkit: {exc}", file=sys.stderr)
        return 1
    if granted:
        print(f"Removed {', '.join(granted)} for {user} from {path}")
    else:
        print(f"Nothing was granted to {user} for {', '.join(args.units)}; {path} is unchanged")
    return 0
