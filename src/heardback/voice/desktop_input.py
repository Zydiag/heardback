"""Desktop dictation input boundary; no session state or dictated-text logging."""
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Callable

from jv import hypr

from .config import log

IGNORE_LAYERS = ("waybar", "awww-daemon", "swaync-notification-window")
LOCK_UNIT = "heardback-dictation-typinglock"
_CTRL_RELEASE_RESERVE_S = 1.0
_PASTE_REAP_RESERVE_S = 0.25
_timer_lock = threading.Lock()
# Config caps dictation_slice_chars at 48; wtype assigns distinct symbols codes 1..N.
# Ctrl+V uses one printable code; -m ctrl remains a separate modifier cleanup.
_RELEASE_SYMBOLS = tuple("abcdefghijklmnopqrstuvwxyz0123456789") + (
    "space", "exclam", "quotedbl", "numbersign", "dollar", "percent",
    "ampersand", "apostrophe", "parenleft", "parenright", "asterisk", "plus")
_KEY_RELEASE_TIMEOUT_S = 1.0


@dataclass(frozen=True)
class Pin:
    address: str
    cls: str
    pid: int


class InputRefused(Exception):
    """Safe reason code; never carries text or a subprocess exception."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _active(empty_code: str) -> tuple[Pin, dict]:
    try:
        window = hypr.hyprctl("activewindow", json_out=True)
        if window == {}:
            raise InputRefused(empty_code)
        if (not isinstance(window, dict)
                or not isinstance(window.get("address"), str)
                or not re.fullmatch(hypr.ADDR_RE, window["address"])
                or not isinstance(window.get("class"), str) or not window["class"]
                or type(window.get("pid")) is not int or window["pid"] <= 0
                or type(window.get("xwayland")) is not bool):
            raise InputRefused("focus_changed")
        return Pin(window["address"], window["class"], window["pid"]), window
    except InputRefused:
        raise
    except Exception:
        raise InputRefused("focus_changed") from None


def read_pin(denied: tuple[str, ...], unsupported: tuple[str, ...],
             ignore_layers: tuple[str, ...]) -> Pin:
    pin, window = _active("no_active_window")
    if pin.cls.casefold() in {cls.casefold() for cls in denied}:
        raise InputRefused("denied_class")
    if window["xwayland"] or pin.cls.casefold() in {cls.casefold() for cls in unsupported}:
        raise InputRefused("unsupported_class")
    if not shutil.which("wtype"):
        raise InputRefused("wtype_missing")
    check(pin, ignore_layers)
    return pin


def check(pin: Pin, ignore_layers: tuple[str, ...] = IGNORE_LAYERS) -> None:
    try:
        locked = subprocess.run(["pgrep", "-x", "hyprlock|swaylock"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        if locked.returncode != 1:  # Only pgrep's explicit "no matches" permits input.
            raise InputRefused("locked")
    except Exception:
        raise InputRefused("locked") from None
    current, window = _active("destination_closed")
    if current != pin:
        raise InputRefused("focus_changed")
    if window["xwayland"]:
        raise InputRefused("unsupported_class")
    try:
        monitors = hypr.hyprctl("layers", json_out=True)
        if not isinstance(monitors, dict):
            raise ValueError
        for monitor in monitors.values():
            levels = monitor["levels"]
            if not isinstance(levels, dict):
                raise ValueError
            for entries in levels.values():
                if not isinstance(entries, list):
                    raise ValueError
                for layer in entries:
                    namespace = layer["namespace"]
                    if not isinstance(namespace, str) or not namespace or namespace not in ignore_layers:
                        raise ValueError
    except Exception:
        # Hyprland exposes namespaces, not reliable keyboard focus: refuse unknown layers.
        raise InputRefused("layer_open") from None


def sanitize(text: str) -> str:
    if any(unicodedata.category(c) in ("Cc", "Cs") or 0x202A <= ord(c) <= 0x202E
           or 0x2066 <= ord(c) <= 0x2069 for c in text):
        raise InputRefused("unsafe_text")
    return unicodedata.normalize("NFC", text)


def join(text: str, first: bool) -> str:
    return text if first or not text or text[0] in ",.!?:;)]}”’»" else " " + text


def _clusters(text: str) -> list[str]:
    clusters: list[str] = []
    for c in text:
        n = ord(c)
        previous = clusters[-1] if clusters else ""
        if previous and (unicodedata.category(c).startswith("M") or c == "\u200d"
                         or previous.endswith("\u200d") or 0x1F3FB <= n <= 0x1F3FF
                         or 0xE0020 <= n <= 0xE007F
                         or (0xDC00 <= n <= 0xDFFF and 0xD800 <= ord(previous[-1]) <= 0xDBFF)
                         or (0x1F1E6 <= n <= 0x1F1FF and len(previous) == 1
                             and 0x1F1E6 <= ord(previous) <= 0x1F1FF)):
            clusters[-1] += c
        else:
            clusters.append(c)
    return clusters


def slices(text: str, limit: int = 48) -> list[str]:
    if limit < 1:
        raise ValueError("invalid_slice_limit")
    clusters = _clusters(text)
    parts, start = [], 0
    while start < len(clusters):
        end, size, word_end = start, 0, start
        while end < len(clusters) and size + len(clusters[end]) <= limit:
            size += len(clusters[end])
            end += 1
            if clusters[end - 1].isspace():
                word_end = end
        if end == start:
            raise InputRefused("unsafe_text")  # A cluster cannot be split to satisfy the bound.
        if end < len(clusters) and word_end > start:
            end = word_end
        parts.append("".join(clusters[start:end]))
        start = end
    return parts


def send(text: str, delay_ms: int, timeout_s: float = 5.0) -> bool:
    process = None
    try:
        if type(delay_ms) is not int or delay_ms < 0 or not math.isfinite(timeout_s) or timeout_s <= 0:
            return False
        payload = sanitize(text).encode("utf-8")
        process = subprocess.Popen(["wtype", "-d", str(delay_ms), "-"], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.communicate(input=payload, timeout=timeout_s)
    except Exception:
        if process is None:
            return False
        if not _kill_and_reap(process, monotonic() + _PASTE_REAP_RESERVE_S):
            _release_failed()
    else:
        if process.returncode == 0:
            return True
    release_keys()
    return False


def _kill_and_reap(process, deadline: float) -> bool:
    try:
        process.kill()
    except OSError:
        pass  # It may already have exited; still confirm termination.
    try:
        process.wait(timeout=max(0.0, deadline - monotonic()))
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _release_failed() -> None:
    log("dictation_key_release_failed", warning="Keys may be held; check the keyboard and destination")
    raise InputRefused("key_release_failed") from None


def release_keys() -> None:
    """Bounded release-only recovery; safe without knowing the focused window."""
    process = None
    try:
        argv = ["wtype", *(arg for symbol in _RELEASE_SYMBOLS for arg in ("-p", symbol))]
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.communicate(timeout=_KEY_RELEASE_TIMEOUT_S)
        if process.returncode == 0:
            return
    except Exception:
        if process is not None:
            _kill_and_reap(process, monotonic() + _PASTE_REAP_RESERVE_S)
    _release_failed()


def paste(text: str, delay_ms: int, timeout_s: float = 5.0) -> bool:
    """One uncertain attempt; ctrl_release_failed means Ctrl may still be held."""
    process = None
    chord_started = False
    chord_reaped = True
    recover = False
    try:
        if (type(delay_ms) is not int or delay_ms < 0 or not math.isfinite(timeout_s)
                or timeout_s <= _CTRL_RELEASE_RESERVE_S + 2 * _PASTE_REAP_RESERVE_S):
            return False
        deadline = monotonic() + timeout_s
        work_deadline = deadline - _CTRL_RELEASE_RESERVE_S - _PASTE_REAP_RESERVE_S
        communicate_deadline = work_deadline - _PASTE_REAP_RESERVE_S
        payload = sanitize(text).encode("utf-8")
        wl_copy = shutil.which("wl-copy")
        if not wl_copy:
            return False
        process = subprocess.Popen([wl_copy, "--sensitive"], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.communicate(input=payload, timeout=max(0.0, communicate_deadline - monotonic()))
        if process.returncode != 0 or monotonic() >= communicate_deadline:
            return False
        process = None
        chord_started = True
        process = subprocess.Popen(["wtype", "-d", str(delay_ms), "-M", "ctrl", "v", "-m", "ctrl"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        process.communicate(timeout=max(0.0, communicate_deadline - monotonic()))
        recover = process.returncode != 0
        return not recover
    except Exception:
        if process is not None:
            reaped = _kill_and_reap(process, work_deadline)
            if chord_started:
                chord_reaped = reaped
                recover = True
        return False
    finally:
        if chord_started:
            release = None
            released = False
            try:
                release = subprocess.Popen(["wtype", "-m", "ctrl"], stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                release.communicate(timeout=max(0.0, min(
                    _CTRL_RELEASE_RESERVE_S, deadline - monotonic() - _PASTE_REAP_RESERVE_S)))
                released = release.returncode == 0
            except Exception:
                if release is not None:
                    release_reaped = _kill_and_reap(release, deadline)
                    recover = True
                    chord_reaped = chord_reaped and release_reaped
            if recover and chord_reaped:
                release_keys()
            if not released or not chord_reaped:
                raise InputRefused("ctrl_release_failed") from None


class FocusWatch:
    """One-shot event watch. Only check() grants permission to type."""

    def __init__(self, pin: Pin, on_lost: Callable[[str], None], ignore_layers: tuple[str, ...]):
        self.pin, self.on_lost, self.ignore_layers = pin, on_lost, ignore_layers
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._mutex = threading.Lock()
        self._fired = False
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="dictation-focus", daemon=True)
        self._thread.start()
        self._ready.wait()  # Do not permit capture before the socket is connected (or loss reported).

    def stop(self) -> None:
        with self._mutex:
            self._stop.set()
            sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()

    def _lost(self, reason: str) -> None:
        with self._mutex:
            if self._fired or self._stop.is_set():
                return
            self._fired = True
        self.on_lost(reason)

    def _event(self, line: str) -> str | None:
        event, separator, data = line.partition(">>")
        if not separator:
            return None
        if event in ("activewindowv2", "closewindow"):
            address = data if data.startswith("0x") else "0x" + data
            if event == "activewindowv2" and address != self.pin.address:
                return "focus_changed"
            if event == "closewindow" and address == self.pin.address:
                return "destination_closed"
        elif event == "activewindow" and data.split(",", 1)[0] != self.pin.cls:
            return "focus_changed"
        elif event == "openlayer" and data not in self.ignore_layers:
            return "layer_opened"
        return None

    def _run(self) -> None:
        sock = None
        try:
            signature = hypr.instance()  # Never use HYPRLAND_INSTANCE_SIGNATURE here.
            runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with self._mutex:
                self._socket = sock
            sock.settimeout(0.5)
            sock.connect(str(runtime / "hypr" / signature / ".socket2.sock"))
            self._ready.set()
            pending = b""
            while not self._stop.is_set():
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    self._lost("focus_watch_lost")
                    return
                pending += data
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    reason = self._event(line.decode("utf-8"))
                    if reason:
                        self._lost(reason)
                        return
                if len(pending) > 65536:
                    raise ValueError("invalid_event_stream")
        except Exception:
            self._lost("focus_watch_lost")
        finally:
            if sock is not None:
                with self._mutex:
                    self._socket = None
                sock.close()
            self._ready.set()


def _submap(name: str, expected: str) -> bool:
    try:
        return (hypr.hyprctl("dispatch", f'hl.dsp.submap("{name}")').strip() == "ok"
                and hypr.hyprctl("submap", json_out=True) == expected)
    except Exception:
        return False


def lock_enter() -> bool:
    return _submap("typinglock", "typinglock")


def lock_held() -> bool:
    try:
        return hypr.hyprctl("submap", json_out=True) == "typinglock"
    except Exception:
        return False


def lock_leave() -> bool:
    return _submap("", "default")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=5)


def _disarm_lock_timer() -> bool:
    for suffix in ("timer", "service"):
        unit = f"{LOCK_UNIT}.{suffix}"
        state = _run(["systemctl", "--user", "show", "--property=LoadState", "--value", unit])
        if state.returncode or state.stdout.strip() not in ("loaded", "not-found"):
            return False
        if state.stdout.strip() == "loaded":
            if _run(["systemctl", "--user", "stop", unit]).returncode:
                return False
    return True


def arm_lock_timer(seconds: float) -> bool:
    """Replace one transient user timer, outside heardback.service's cgroup."""
    try:
        if not math.isfinite(seconds) or seconds <= 0:
            return False
        with _timer_lock:
            executable = shutil.which("hyprctl")
            signature = hypr.instance()
            if not executable or not _disarm_lock_timer():
                return False
            return _run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={LOCK_UNIT}",
                         f"--on-active={seconds:g}s", "--timer-property=AccuracySec=1us",
                         "--expand-environment=no",
                         "--", "/bin/sh", "-c",
                         'current=$("$1" -i "$2" -j submap) && '
                         'if [ "$current" = \'"typinglock"\' ]; then '
                         '"$1" -i "$2" dispatch \'hl.dsp.submap("")\'; fi',
                         LOCK_UNIT, executable, signature]).returncode == 0
    except Exception:
        return False


def disarm_lock_timer() -> bool:
    try:
        with _timer_lock:
            return _disarm_lock_timer()
    except Exception:
        return False


def reset_lock() -> bool:
    """Startup/ExecStopPost backstop, resolving the compositor through jv.hypr.instance()."""
    if os.environ.get("JV_DRY") == "1":
        return True
    released = True
    try:
        release_keys()
    except InputRefused:
        released = False
    try:
        current = hypr.hyprctl("submap", json_out=True)
    except Exception:
        return False
    # Keep the outside timer armed if we cannot confirm the submap was left.
    return (current != "typinglock" or lock_leave()) and disarm_lock_timer() and released
