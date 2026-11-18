"""Read named attachments locally without returning their text to the brain."""
import json
import os
import re
import shutil
import stat
import time
import unicodedata
from pathlib import Path

from heardback.status.store import state_dir
from jv import ctx
from jv.verb import JvError, runtime_path


def heard() -> dict:
    """Read only a private, owned regular history file; unsafe/malformed history is missing."""
    try:
        fd = os.open(runtime_path('heardback-heard.json'), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                return {}
            data = json.load(stream)
        if not isinstance(data, dict):
            return {}
        for key in ('current', 'previous'):
            item = data.get(key)
            if item is not None and (not isinstance(item, dict) or
                    not isinstance(item.get('turn'), str) or not item['turn'] or
                    not isinstance(item.get('text'), str) or not item['text'].strip()):
                return {}
        return data
    except (OSError, ValueError):
        return {}


def read(kind: str, target: str | None, destination: str | None) -> tuple[str, str, str]:
    """Return content, revision and label. Pane/tab revisions bind identity, not live text."""
    if kind == 'last_heard':
        previous = heard().get('previous')
        if not previous:
            raise JvError(4, "I don't have what you said before")
        text = previous['text']
        return text, ctx.revision('heard', previous['turn'] + '\0' + text), f'what you said before, {len(text)} characters'
    if kind == "screenshot":
        from jv.shot import latest
        path = latest()
        return str(path), ctx.revision("screenshot", f"{path.name}\0{path.stat().st_mtime_ns}\0{path.stat().st_size}"), "the latest screenshot"
    if kind == "tab":
        tab = ctx._tab()
        url = tab["url"]
        if not url.startswith(("https://", "http://")) or any(unicodedata.category(c) == "Cc" for c in url):
            raise JvError(4, "the current tab has no web link")
        title = "".join(c for c in tab["title"] if not unicodedata.category(c).startswith("C") or c.isspace())
        title = " ".join(title.replace('"', "'").split())[:100]
        return (f'Zen tab "{title}" {url}', ctx.revision("tab", f"{tab['tabId']}\0{url}"),
                "the current tab's link")
    if kind == "pane":
        from jv import tmux
        pane = tmux.resolve(target)
        if pane["id"] == destination:
            raise JvError(4, "the attachment pane is also the destination; choose another pane")
        content = tmux.screen(pane["id"], 200)
        if not content.strip():
            raise JvError(4, "the attachment pane is empty")
        return content, ctx.revision("pane", pane["id"]), f"the last 200 lines of window {pane['window_index']}"
    content, revision = ctx.read(kind)
    return content, revision, f"your {kind}, {len(content)} characters"


def compose(text: str, kind: str, content: str, label: str) -> str:
    return f"{text} (from heardback: {content})" if kind == "tab" else f"{text}\n\n(from heardback: {label})\n{content}"


def _inbox(*, create: bool) -> tuple[Path, int]:
    path = state_dir(create=False) / "inbox"
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise JvError(4, "the attachment inbox is not owned by you")
        os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return path, fd


def _prune(fd: int) -> None:
    cutoff = time.time() - 7 * 86400
    with os.scandir(fd) as entries:
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    os.unlink(entry.name, dir_fd=fd)
            except FileNotFoundError:
                pass  # Another writer may have pruned the same file.


def prune() -> None:
    """Remove old regular inbox files without following links or creating the inbox."""
    try:
        try:
            _, fd = _inbox(create=False)
        except FileNotFoundError:
            return
        try:
            _prune(fd)
        finally:
            os.close(fd)
    except OSError:
        raise JvError(4, "could not safely prune the attachment inbox") from None


def write(kind: str, content: str) -> Path:
    """Prune then create an exclusive private file; content never rides on argv."""
    if kind not in ("clipboard", "selection", "pane", "last_heard"):
        raise JvError(2, "unsupported inbox attachment")
    try:
        path, fd = _inbox(create=True)
        try:
            _prune(fd)
            turn = re.sub(r"[^a-zA-Z0-9_-]", "_", os.environ.get("HEARDBACK_TURN") or str(os.getpid()))[:100]
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time()))
            suffix = 0
            while True:
                name = f"{stamp}-{turn}-{kind}{'-' + str(suffix) if suffix else ''}.txt"
                try:
                    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                    break
                except FileExistsError:
                    suffix += 1
            try:
                with os.fdopen(out, "w", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(content)
            except BaseException:
                os.unlink(name, dir_fd=fd)
                raise
            return path / name
        finally:
            os.close(fd)
    except OSError:
        raise JvError(4, "could not safely write the attachment inbox") from None


def write_screenshot(content: str) -> Path:
    source = Path(content)
    try:
        path, fd = _inbox(create=True)
        try:
            _prune(fd)
            turn = re.sub(r"[^a-zA-Z0-9_-]", "_", os.environ.get("HEARDBACK_TURN") or str(os.getpid()))[:100]
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            suffix = 0
            while True:
                name = f"{stamp}-{turn}-screenshot{'-' + str(suffix) if suffix else ''}.png"
                try:
                    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                    break
                except FileExistsError:
                    suffix += 1
            os.close(out)
            try:
                if source.is_symlink() or not source.is_file():
                    raise OSError("screenshot is not a regular file")
                shutil.copyfile(source, path / name)
                os.chmod(path / name, 0o600, follow_symlinks=False)
            except BaseException:
                os.unlink(name, dir_fd=fd)
                raise
            return path / name
        finally:
            os.close(fd)
    except OSError:
        raise JvError(4, "could not safely write the screenshot attachment") from None
