"""PipeWire devices via `pactl`: pick the mic, watch hot-plug events, spot other apps recording."""
import json
import re
import subprocess
import threading
import time
from fnmatch import fnmatchcase
from typing import Callable

from heardback.config import load as desktop_config
from .audio_in import backoff
from .config import log

DEBOUNCE_S = 0.5
_EVENT = re.compile(r"Event '(?:new|remove|change)' on (source-output|source|sink|card|server) #\d+")
PEAK_METERS = {"org.PulseAudio.pavucontrol"}


def pactl_json(*args: str) -> list[dict]:
    try:
        out = subprocess.run(["pactl", "-f", "json", *args], capture_output=True, text=True, timeout=5).stdout
        data = json.loads(out or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _is_monitor(s: dict) -> bool:
    return s.get("name", "").endswith(".monitor")


def pick_mic(sources: list[dict], mode: str, mic_node: str, *, fallback: bool = False) -> tuple[str, bool]:
    names = [s.get("name", "") for s in sources]
    if not fallback and mic_node != "auto" and mic_node in names:
        return mic_node, False
    selectors = desktop_config().modes.get(mode)
    if selectors is None:
        raise ValueError(f"unknown mic mode: {mode}")
    available = sorted((s for s in sources if not _is_monitor(s)),
                       key=lambda s: (s.get("properties") or {}).get("device.bus") != "pci")
    for i, selector in enumerate(selectors):
        if fallback and i == 0:  # A stalled source can remain listed until PipeWire removes it.
            continue
        if selector == "@DEFAULT_SOURCE@":
            try:
                selector = subprocess.run(["pactl", "get-default-source"], capture_output=True,
                                          text=True, timeout=5, check=True).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                continue
        for source in available:
            if fnmatchcase(source.get("name", ""), selector):
                return source["name"], i > 0
    raise ValueError(f"no mic for mode {mode}")


def parse_event(line: str) -> str | None:
    m = _EVENT.search(line)
    return m.group(1) if m else None


def other_recorders(outputs: list[dict], sources: list[dict]) -> list[str]:
    monitors = {s.get("index") for s in sources if _is_monitor(s)}
    apps = []
    for o in outputs:
        p = o.get("properties") or {}
        if p.get("node.name") == "heardback-mic" or o.get("source") in monitors:
            continue
        if p.get("stream.monitor") == "true" or p.get("application.id") in PEAK_METERS:
            continue
        apps.append(p.get("application.name") or p.get("node.name") or "?")
    return apps


class Watcher:
    """Thread running `pactl subscribe`; calls on_change(facility) once per 0.5 s burst of events."""

    def __init__(self, on_change: Callable[[str], None]):
        self.on_change = on_change
        self._stopping = False
        self._proc: subprocess.Popen | None = None
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, name="heardback-pactl", daemon=True).start()

    def stop(self) -> None:
        self._stopping = True
        if self._proc and self._proc.poll() is None:
            self._proc.kill()

    def _event(self, facility: str) -> None:
        with self._lock:
            self._pending.add(facility)
            if self._timer is None:
                self._timer = threading.Timer(DEBOUNCE_S, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            pending, self._pending, self._timer = self._pending, set(), None
        for f in sorted(pending):
            self.on_change(f)

    def _run(self) -> None:
        n = 0
        while not self._stopping:
            try:
                p = self._proc = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                                  stderr=subprocess.DEVNULL, text=True)
                for line in p.stdout:
                    n = 0
                    if (f := parse_event(line)):
                        self._event(f)
                p.wait()
            except OSError:
                pass
            if self._stopping:
                return
            log("pactl_exit", retry_s=backoff(n))
            time.sleep(backoff(n))
            n += 1


def describe(mode: str | None = None, mic_node: str = "auto") -> str:
    mode = mode or desktop_config().mic
    sources = pactl_json("list", "sources")
    try:
        node, fell_back = pick_mic(sources, mode, mic_node)
        mic = node + (f" ({mode} missing, fell back)" if fell_back else "")
    except ValueError as e:
        mic = f"none: {e}"
    try:
        sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sink = "?"
    lines = [f"mic: {mic}", f"sink: {sink}"]
    for c in pactl_json("list", "cards"):
        if c.get("name", "").startswith("bluez_card."):
            codec = (c.get("properties") or {}).get("api.bluez5.codec", "?")
            lines.append(f"{c['name']}: profile {c.get('active_profile', '?')}, codec {codec}")
    others = other_recorders(pactl_json("list", "source-outputs"), sources)
    lines.append("recording: " + (", ".join(others) or "nobody else"))
    return "\n".join(lines)
