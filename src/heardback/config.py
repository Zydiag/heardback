"""Desktop settings shared by jv and voice; only the standard library is needed."""
import os
import re
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path


@dataclass(frozen=True)
class Config:
    name: str | None = None
    session: str | None = None
    project_roots: tuple[str, ...] = ("~",)
    claude_dirs: tuple[str, ...] = ("~",)
    claude_home: str = "~"
    aliases: dict[str, str] = field(default_factory=dict)
    mic: str = "default"
    modes: dict[str, list[str]] = field(default_factory=lambda: {"default": ["@DEFAULT_SOURCE@"]})


def config_path() -> Path:
    if path := os.environ.get("HEARDBACK_CONFIG"):
        return Path(path).expanduser().absolute()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "heardback" / "config.toml"


def load() -> Config:
    return _load(config_path())


@cache
def _load(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return Config()

    def text(value):
        return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 or ord(c) == 127 for c in value)

    def strings(value):
        return isinstance(value, list) and bool(value) and all(text(s) for s in value)

    sections = {"user": {"name"}, "tmux": {"session", "project_roots", "claude_dirs", "claude_home"},
                "devices": {"aliases"}, "mic": {"mode", "modes"}}
    for section, values in raw.items():
        if section not in sections or not isinstance(values, dict) or values.keys() - sections[section]:
            raise ValueError(f"{path}: unknown section/key or invalid table: {section}")
    vals = {}
    for section, keys in sections.items():
        for key, value in raw.get(section, {}).items():
            if key in {"project_roots", "claude_dirs"}:
                valid = strings(value)
                value = tuple(value) if valid else value
            elif key == "aliases":
                valid = isinstance(value, dict) and all(text(k) and text(v) for k, v in value.items())
                value = {k.casefold(): v for k, v in value.items()} if valid else value
            elif key == "modes":
                valid = isinstance(value, dict) and bool(value) and all(
                    re.fullmatch(r"[\w-]+", k) and strings(v) for k, v in value.items())
            else:
                valid = text(value)
            if not valid:
                raise ValueError(f"{path}: invalid {section}.{key}")
            vals["mic" if key == "mode" else key] = value
    cfg = Config(**vals)
    if cfg.mic not in cfg.modes:
        raise ValueError(f"{path}: mic.mode must name one of mic.modes")
    return cfg
