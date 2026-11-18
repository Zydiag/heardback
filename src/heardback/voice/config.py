"""voice.toml: the Config dataclass, load/update, XDG paths, and the journal `log`."""
import json
import math
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from heardback.config import load as desktop_config
from .extra import default_device

ENUMS = {"speaker_check": {"on", "wake", "off"}, "stt_device": {"cuda", "cpu"}}


@dataclass(frozen=True)
class Config:
    mic: str = field(default_factory=lambda: desktop_config().mic)
    mic_node: str = "auto"
    wake_threshold: float = 0.5
    speaker_check: str = "off"
    speaker_wake: float = 0.0
    speaker_utt: float = 0.0
    end_silence: float = 0.7
    short_silence: float = 0.0   # end after this much silence when under 2 s was spoken; 0 = always end_silence
    max_utterance_s: float = 60.0
    stt_device: str = field(default_factory=default_device)
    tts_voice: str = "bm_george"
    tts_volume: float = 2.5      # gain on Kokoro's output, 1.0 = raw; (0, 5]
    notify: bool = True
    duck_media: bool = True      # pause players while listening, resume at turn end (not after a play verb)
    automute_when_mic_in_use: bool = True
    log_text: bool = False
    follow_up: bool = True       # listen once more after Claude asks a question
    dash_port: int = 8787        # heardbackd's dashboard on 127.0.0.1; 0 = off
    stt_hotwords: tuple = ()     # extra words Whisper should expect (names, sites); first in the hint string
    stt_fixes: dict = field(default_factory=dict)  # whole-phrase STT corrections for command speech
    wake_miss_floor: float = 0.1
    dictation_silence_s: float = 0.6
    dictation_soft_cap_s: float = 8.0
    dictation_soft_silence_s: float = 0.25
    dictation_hard_cap_s: float = 20.0
    dictation_min_burst_s: float = 0.15
    dictation_min_voiced_s: float = 0.3
    dictation_slice_chars: int = 48
    dictation_type_delay_ms: int = 20
    dictation_post_shortcut_ms: int = 491
    dictation_max_session_s: float = 1800.0
    dictation_denied_classes: tuple = ("1Password", "Bitwarden", "KeePassXC", "pinentry",
        "pinentry-qt", "pinentry-gtk-2", "polkit-gnome-authentication-agent-1",
        "hyprpolkitagent", "org.kde.polkit-kde-authentication-agent-1", "gcr-prompter")
    dictation_unsupported_classes: tuple = ("electron", "electron-wayland", "apple-music",
        "claude-desktop", "feishin", "md.obsidian.Obsidian")
    dictation_ignore_layers: tuple = ("waybar", "awww-daemon", "swaync-notification-window")
    dictation_paste_classes: tuple = ()

    def __post_init__(self):
        for name in ("silence_s", "soft_cap_s", "soft_silence_s", "hard_cap_s", "min_burst_s",
                     "min_voiced_s", "max_session_s"):
            value = getattr(self, "dictation_" + name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or \
                    not math.isfinite(value) or value <= 0:
                raise ValueError(f"voice.toml: dictation_{name} must be finite and positive")
        if not self.dictation_soft_silence_s <= self.dictation_silence_s or not (
                self.dictation_min_burst_s <= self.dictation_min_voiced_s <=
                self.dictation_soft_cap_s < self.dictation_hard_cap_s <= 120):
            raise ValueError("voice.toml: inconsistent dictation chunk durations")
        for name, minimum, maximum in (("slice_chars", 1, 48), ("type_delay_ms", 0, 4000),
                                       ("post_shortcut_ms", 300, None)):
            value = getattr(self, "dictation_" + name)
            if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
                raise ValueError(f"voice.toml: dictation_{name} outside safe bounds")
        # Leave a second inside the five-second send timeout and ten-second lock lease.
        if self.dictation_slice_chars * self.dictation_type_delay_ms > 4000:
            raise ValueError("voice.toml: dictation typing delay exceeds the send/lock budget")
        for name in ("denied_classes", "unsupported_classes", "ignore_layers", "paste_classes"):
            value = getattr(self, "dictation_" + name)
            if not isinstance(value, tuple) or any(not isinstance(s, str) or not s.strip() or
                    any(ord(c) < 32 or ord(c) == 127 for c in s) for s in value):
                raise ValueError(f"voice.toml: dictation_{name} must contain nonempty class/layer names")


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or os.path.expanduser(default))


def config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", "~/.config") / "heardback" / "voice.toml"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", "~/.local/share") / "heardback"


def log(event: str, **fields) -> None:
    print(json.dumps({"event": event, "t": time.time(), **fields}), flush=True)


def _check(name: str, value, default):
    if isinstance(default, tuple):
        ok = isinstance(value, list) and all(isinstance(w, str) for w in value)
        value = tuple(value) if ok else value
        if not ok:
            raise ValueError(f"voice.toml: {name} must be a list of strings, got {value!r}")
        return value
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(default, float):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        value = float(value) if ok else value
    else:
        ok = isinstance(value, str)
    if not ok:
        raise ValueError(f"voice.toml: {name} must be {type(default).__name__}, got {value!r}")
    if name in ENUMS and value not in ENUMS[name]:
        raise ValueError(f"voice.toml: {name} must be one of {sorted(ENUMS[name])}, got {value!r}")
    return value


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raw = {}
    base = Config()
    defaults = {f.name: getattr(base, f.name) for f in fields(Config)}
    vals = {}
    fixes = raw.pop("stt_fixes", {})
    if not isinstance(fixes, dict) or any(not isinstance(k, str) or not k.strip() or
            not isinstance(v, str) or not v.strip() or any(ord(c) < 32 or ord(c) == 127 for c in k + v)
            for k, v in fixes.items()):
        raise ValueError("voice.toml: stt_fixes must be a table of nonempty phrase strings")
    if fixes:
        vals["stt_fixes"] = fixes
    for k, v in raw.items():
        if k not in defaults:
            log("unknown_key", key=k, path=str(path))
            continue
        vals[k] = _check(k, v, defaults[k])
    cfg = replace(base, **vals)
    if cfg.mic not in desktop_config().modes:
        raise ValueError(f"voice.toml: mic must name a mode in config.toml: {sorted(desktop_config().modes)}")
    if not 0.0 < cfg.tts_volume <= 5.0:
        raise ValueError(f"voice.toml: tts_volume must be > 0 and <= 5, got {cfg.tts_volume}")
    if not math.isfinite(cfg.wake_miss_floor) or not 0.0 <= cfg.wake_miss_floor < cfg.wake_threshold:
        raise ValueError("voice.toml: wake_miss_floor must be finite, nonnegative, and below wake_threshold")
    if not math.isfinite(cfg.max_utterance_s) or not 5.0 <= cfg.max_utterance_s <= 120.0:
        raise ValueError(f"voice.toml: max_utterance_s must be finite and between 5 and 120, got "
                         f"{cfg.max_utterance_s}")
    if not 0 <= cfg.dash_port <= 65535:
        raise ValueError(f"voice.toml: dash_port must be 0-65535, got {cfg.dash_port}")
    if cfg.speaker_check == "on" and (cfg.speaker_wake <= 0.0 or cfg.speaker_utt <= 0.0):
        raise ValueError("voice.toml: speaker_check = \"on\" needs speaker_wake and speaker_utt; "
                         "run `heardback-voice tune` first")
    if cfg.speaker_check == "wake" and cfg.speaker_wake <= 0.0:
        raise ValueError("voice.toml: speaker_check = \"wake\" needs speaker_wake; run `heardback-voice tune` first")
    return cfg


def _toml(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else json.dumps(v)


def update(path: Path, **values) -> None:
    """Rewrite `key = value` lines in place (trailing comments kept), append missing keys."""
    lines = path.read_text().splitlines() if path.exists() else []
    for k, v in values.items():
        pat = re.compile(rf"^\s*{re.escape(k)}\s*=[^#]*?(\s*#.*)?$")
        for i, line in enumerate(lines):
            m = pat.match(line)
            if m:
                lines[i] = f"{k} = {_toml(v)}{m.group(1) or ''}"
                break
        else:
            lines.append(f"{k} = {_toml(v)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
