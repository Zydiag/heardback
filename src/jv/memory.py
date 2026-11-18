"""Typed-only commands for the user's manually edited, durable heardback memory."""
import argparse
import errno
import os
import stat
import sys

from heardback.brain import memory as memory_file

INIT_TEXT = "# heardback memory\n\n## About me\n\n## Preferences\n\n## Projects\n\n## Current focus\n"
ERROR_TEXT = {
    "changed_during_read": "changed while it was being read",
    "invalid_utf8": "is not valid UTF-8",
    "not_regular": "is not a regular file",
    "oversize": f"exceeds the {memory_file.MAX_BYTES}-byte limit",
    "unsafe_path": "is a symbolic link or unsafe path",
    "unreadable": "cannot be read",
}


def _parents_private(path) -> None:
    missing = []
    current = path
    while True:
        try:
            mode = current.stat().st_mode
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise
            current = parent
            continue
        if not stat.S_ISDIR(mode):
            raise OSError(errno.ENOTDIR, "memory parent is not a directory")
        break

    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise OSError(errno.ENOTDIR, "memory parent is not a directory")
        else:
            os.chmod(directory, 0o700)


def _init_file() -> None:
    path = memory_file.memory_path()
    _parents_private(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        data = INIT_TEXT.encode("utf-8")
        while data:
            written = os.write(fd, data)
            data = data[written:]
    finally:
        os.close(fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jv memory", description="Inspect or initialize the typed-only memory file.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("path", help="print the memory file path")
    commands.add_parser("show", help="print the bounded memory file")
    commands.add_parser("init", help="create a blank memory file without overwriting one")
    return parser


def main(argv: list[str]) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0

    path = memory_file.memory_path()
    if args.command == "path":
        print(path)
        return 0
    if args.command == "init":
        try:
            _init_file()
        except FileExistsError:
            print(f"Memory file already exists; left unchanged: {path}", file=sys.stderr)
            return 1
        except OSError:
            print("Could not create the memory file safely.", file=sys.stderr)
            return 1
        print(f"Created {path}")
        return 0

    snapshot = memory_file.read_memory(path)
    if snapshot.status == "missing":
        print(f"No memory file exists at {path}. Run `jv memory init` to create one.")
        return 0
    if snapshot.status == "error":
        print(f"Memory file {ERROR_TEXT.get(snapshot.reason, 'is unavailable')}.", file=sys.stderr)
        return 1
    if snapshot.status == "empty":
        print("Memory file is empty.")
        return 0
    print(snapshot.text, end="" if snapshot.text.endswith("\n") else "\n")
    return 0
