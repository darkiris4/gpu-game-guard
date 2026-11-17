"""Docker and Podman as Runtime implementations."""
from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import gpu, util
from .model import Ref, Runtime, Target

CHANGE_TIMEOUT = 120.0  # stopping a container waits for its own grace period first
KNOWN = ("docker", "podman")


def _has_gpu_name(value: Any) -> bool:
    return isinstance(value, str) and "gpu" in value.lower()


def _device_is_gpu(device: Any) -> bool:
    path = device.get("PathOnHost") if isinstance(device, dict) else device
    if not isinstance(path, str):
        return False
    return (path.startswith("/dev/nvidia") or path == "/dev/kfd" or path.rstrip("/") == "/dev/dri"
            or path.startswith("/dev/dri/") or _has_gpu_name(path))


def _items(value: Any) -> list:
    """The value when it is a list, otherwise an empty list, so malformed records iterate safely."""
    return value if isinstance(value, list) else []


def _request_is_gpu(request: Any) -> bool:
    if not isinstance(request, dict):
        return False
    for capability in _items(request.get("Capabilities")):
        if isinstance(capability, (list, tuple)) and "gpu" in capability:
            return True
    if str(request.get("Driver") or "").lower() == "cdi":
        return any(_has_gpu_name(device) for device in _items(request.get("DeviceIDs")))
    return False


def _annotations_are_gpu(annotations: Any) -> bool:
    if not isinstance(annotations, dict):
        return False
    return any(isinstance(key, str) and key.startswith("cdi.k8s.io/") and _has_gpu_name(value)
               for key, value in annotations.items())


def _hides_nvidia_devices(config: dict) -> bool:
    """True when the environment sets NVIDIA_VISIBLE_DEVICES to void or none."""
    for entry in _items(config.get("Env")):
        if isinstance(entry, str):
            name, _, value = entry.partition("=")
            if name == "NVIDIA_VISIBLE_DEVICES" and value.strip().lower() in ("void", "none"):
                return True
    return False


def is_gpu_container(info: dict) -> bool:
    """True when a single `inspect` record shows the container was given access to a GPU.

    Privileged containers see every device without naming any, so they are not detected. A container that
    only maps /dev/dri, for example for video decoding, is detected too: render nodes cannot be told apart
    from compute access, and such containers are excluded through `ignore_containers`. The nvidia runtime
    does not count when the environment hides every device, which matters when it is the daemon default.
    Malformed records are never detected.
    """
    if not isinstance(info, dict):
        return False
    host = info.get("HostConfig") or {}
    config = info.get("Config") or {}
    if not isinstance(host, dict) or not isinstance(config, dict):
        return False
    if any(_request_is_gpu(request) for request in _items(host.get("DeviceRequests"))):
        return True
    if host.get("Runtime") == "nvidia" and not _hides_nvidia_devices(config):
        return True
    if any(_device_is_gpu(device) for device in _items(host.get("Devices"))):
        return True
    return _annotations_are_gpu(config.get("Annotations")) or _annotations_are_gpu(host.get("Annotations"))


class DockerLikeRuntime:
    """A container runtime whose command line matches Docker's, parametrised by the command name."""

    def __init__(self, command: str, vram: Callable[[], Optional[Dict[str, int]]] = gpu.vram_by_container,
                 run: Optional[gpu.Runner] = None):
        self.name = command
        self.command = command
        self._vram = vram
        self._run = run or gpu.run_command

    def installed(self) -> bool:
        """True when the command exists on PATH, whether or not its daemon is running."""
        return shutil.which(self.command) is not None

    def available(self) -> bool:
        """True when the command exists and its daemon answers."""
        return self._run([self.command, "version"]) is not None

    def _own(self, refs: Sequence[Ref]) -> List[str]:
        names = []
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("kind") != "container" or ref.get("runtime") != self.name:
                continue
            name = ref.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name.startswith("-"):  # a leading dash would be read as an option
                util.log(f"{self.command}: skipping container name '{name}'")
                continue
            names.append(name)
        return names

    def _change(self, verb: str, name: str) -> None:
        """Run `stop` or `start` for one container; a failure is logged and never raised."""
        try:
            self._run([self.command, verb, "--", name], timeout=CHANGE_TIMEOUT)
        except Exception as exc:
            util.log(f"{self.command} {verb}: {exc}")

    def running_gpu_targets(self) -> List[Target]:
        out = self._run([self.command, "ps", "-q"])
        ids = out.split() if out else []
        if not ids:
            return []
        # An id that vanished since `ps` makes inspect exit non-zero while still printing the other records.
        out = self._run([self.command, "inspect", *ids], keep_output=True)
        if not out:
            return []
        try:
            records = json.loads(out)
        except ValueError as exc:
            util.log(f"{self.command} inspect: unreadable output ({exc})")
            return []
        found = [info for info in records if is_gpu_container(info)] if isinstance(records, list) else []
        if not found:
            return []
        try:
            vram = self._vram()
        except Exception as exc:  # a measuring failure must not hide the containers
            util.log(f"GPU memory lookup failed ({exc})")
            vram = None
        targets = []
        for info in found:
            name = str(info.get("Name") or "").lstrip("/")
            if not name:
                continue
            ref_id = str(info.get("Id") or "")
            config = info.get("Config")
            image = config.get("Image", "") if isinstance(config, dict) else ""
            if vram is None:
                measured = None
            else:  # a source that could not see every process cannot say that a missing container holds none
                measured = vram.get(ref_id, 0 if getattr(vram, "authoritative", True) else None)
            targets.append(Target("container", self.name, name, str(image), measured, ref_id))
        return sorted(targets, key=lambda target: target.name)

    def stop(self, refs: Sequence[Ref]) -> None:
        names = self._own(refs)
        if not names:
            return
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            list(pool.map(lambda name: self._change("stop", name), names))

    def start(self, refs: Sequence[Ref]) -> None:
        for name in self._own(refs):
            self._change("start", name)


def build(names: Sequence[str]) -> List[Runtime]:
    runtimes: List[Runtime] = []
    for name in names:
        if name in KNOWN:
            runtimes.append(DockerLikeRuntime(name))
        else:
            util.log(f"unknown runtime '{name}' skipped")
    return runtimes
