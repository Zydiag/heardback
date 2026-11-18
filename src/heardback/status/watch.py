"""Private pane watches shared by jv and the announcer; newest arm wins."""
import fcntl
import hashlib
import json
import os
import re
import stat
import textwrap
import time
import unicodedata
import uuid
from contextlib import contextmanager

from heardback.status import store

DONE_S = 12 * 3600
STATUS_S = 90
FREE_S = 30 * 60
STATUS_LINE = ("heardback: status. Reply in 2-3 short spoken sentences: what you're working on, "
               "what's done, what's next. No tables, lists, code, paths or ids. Don't start or change anything.")


def speakable(reply):
    """Outside session text goes only to voice, with wake phrases and screen-only text removed."""
    from jv.verb import JvError, refuse_wake
    text = reply or ''
    for fence in ('```', '~~~'):
        text = re.sub(fence + r'.*?(?:' + fence + r'|$)', ' ', text, flags=re.S)
    lines = []
    for line in text.splitlines():
        if (line.lstrip().startswith('|') or re.fullmatch(r'[|\-: ]*', line)
                or 'reply in 2-3 short spoken sentences' in line.casefold()):
            continue
        lines.append(re.sub(r'^\s*(?:#+|[-*+>]|\d+[.)])\s*', '', line))
    sentences = []
    for sentence in re.split(r'(?<=[.!?])\s+', '\n'.join(lines)):
        if (re.search(r'\bheardback\s*:', sentence, re.I)
                or any(c.isalpha() and not c.isascii() for c in unicodedata.normalize('NFKD', sentence))):
            continue
        try:
            refuse_wake(sentence, 'status answer')
        except JvError:
            continue
        sentence = re.sub(r'`[^`]*(?:`|$)', '', sentence)
        sentence = re.sub(r'\S*/\S*|~\S*', '', sentence)
        sentence = ' '.join(token for token in sentence.split()
                            if not any(c in token for c in '@=\\')
                            and not (len(token) >= 12 and any(c.isalpha() for c in token)
                                     and any(c.isdigit() or c in '_-' for c in token)))
        sentence = re.sub(r'\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b|\b[0-9a-fA-F]{7,}\b', '', sentence)
        sentence = ' '.join(sentence.split()).strip()
        try:
            refuse_wake(sentence, 'status answer')  # removing markup can join a previously split wake phrase
        except JvError:
            continue
        if sentence and any(c.isalnum() for c in sentence):
            sentences.append(sentence)
    text = ' '.join(sentences[:3])
    if len(text) > 300:
        text = textwrap.shorten(text, width=300, placeholder='…')
    elif len(sentences) > 3:
        text = text.rstrip('.,;:') + '…'
    try:
        refuse_wake(text, 'status answer')
    except JvError:
        return ''
    return text


def _ask_target(a):
    from heardback.status import cli, who
    from jv.verb import JvError
    sessions = store.user_sessions(store.list_sessions(), include_ended=True)
    try:
        s = cli._find(a['who'], sessions)
    except JvError as e:
        if e.code != 1 or e.extra.get('candidates'):
            raise
        target = who.normalize_window_target(a['who'])
        target = f'window {target}' if target.isdigit() else target
        line = f"There's no Claude Code session in {target} I can ask."
        raise JvError(1, line, say=line) from None
    if not s.get('tmux_pane'):
        line = "There's no Claude Code session in that tmux window I can ask."
        raise JvError(1, line, say=line)
    if a.get('expect_session') is not None and a['expect_session'] != s['session_id']:
        line = f"Window {who.window(s)}'s Claude session changed; I didn't ask."
        raise JvError(4, line, say=line)
    if s['tmux_pane'] == os.environ.get('TMUX_PANE'):
        line = "I won't ask for status in the pane I'm running from."
        raise JvError(4, line, say=line)
    from jv import tmux
    if s.get('state') == 'working' and not tmux.busy(s):
        s = {**s, 'state': 'idle'}  # interrupted turn: the store stays as the hooks left it
    return s, sessions


def _ask_revision(s):
    return 'status:' + hashlib.sha256(json.dumps([s['session_id'], s.get('state')]).encode()).hexdigest()


def ask_check(a):
    from heardback.status import speak, who
    from jv import tmux
    s, sessions = _ask_target(a)
    revision = _ask_revision(s)
    a['_source_revision'] = revision
    if a.get('expect_session') and s.get('state') in ('working', 'waiting'):
        from jv.verb import JvError
        line = f"Window {who.window(s)} is busy again; I didn't ask."
        raise JvError(4, line, say=line)
    if s.get('state') == 'ended':
        return None
    if s.get('state') in ('working', 'waiting'):
        if a.get('source_revision') != revision:
            a.pop('source_revision', None)
        return None if a.get('full') else speak.one(s, sessions, time.time()) + " Ask it when it's free?"
    tmux._status_ready(s['tmux_pane'], s['session_id'])
    if tmux._tainted():
        if a.get('source_revision') != revision:
            a.pop('source_revision', None)
        return (f"Send status to window {who.window(s)}?" if a.get('full') else
                f"Ask the Claude session in window {who.window(s)} for its status?")
    return None


def ask(a):
    from heardback.status import speak, who
    from jv import tmux
    from jv.verb import JvError
    s, sessions = _ask_target(a)
    now, w = time.time(), who.window(s)
    if a.get('expect_session') and s.get('state') in ('working', 'waiting'):
        line = f"Window {w} is busy again; I didn't ask."
        raise JvError(4, line, say=line)
    if a.get('full') and not discard(s['tmux_pane']):
        line = f"I couldn't remove window {w}'s earlier watch, so I didn't type into it."
        raise JvError(4, line, say=line)
    if s.get('state') == 'ended' or a.get('full') and s.get('state') in ('working', 'waiting'):
        return {'say': speak.one(s, sessions, now)}
    if s.get('state') in ('working', 'waiting'):
        revision = _ask_revision(s)
        if a.get('source_revision') != revision:
            line = speak.one(s, sessions, now) + " Ask it when it's free?"
            raise JvError(3, line, confirm=line, source_revision=revision)
        if arm(s['tmux_pane'], 'when_free', now=now, deadline=store.now_iso(now + FREE_S), session_id=s['session_id']):
            return {'say': f"I'll ask window {w} when it's free."}
        line = 'No watch was armed; I could not save the status watch.'
        raise JvError(1, line, say=line)
    if a.get('expect_session') and s['tmux_pane'] in tmux.tmux('list-clients', '-F', '#{pane_id}').splitlines():
        line = f"Window {w} is in front of you; I didn't type into it."
        raise JvError(4, line, say=line)
    tmux._status_submit(s['tmux_pane'], s['session_id'], 'status' if a.get('full') else STATUS_LINE)
    if a.get('full'):
        return {'say': f'Sent status to window {w}.'}
    if arm(s['tmux_pane'], 'status', now=now, deadline=store.now_iso(now + STATUS_S), session_id=s['session_id']):
        return {'say': f"Asking window {w}; I'll read out its answer."}
    return {'say': f'Sent the status request to window {w}, but I could not save its answer watch.'}


@contextmanager
def _directory(create=False):
    path = store.state_dir(create=False) / 'watches'
    if create:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise OSError('watch directory is not owned by you')
        os.fchmod(fd, 0o700)
        # Serialize replacement and unlink so a consumer cannot remove a newer watch.
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def _filename(pane):
    if not isinstance(pane, str) or not re.fullmatch(r'%\d+', pane):
        raise ValueError('invalid watch pane')
    return pane[1:] + '.json'


def _read(fd, name):
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise OSError('unsafe watch file')
    stream = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(stream) as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError('unsafe watch file')
        try:
            record = json.load(f)
        except ValueError:
            record = None
    if (not isinstance(record, dict) or not {'pane', 'kind', 'armed_at', 'deadline', 'turn'} <= set(record)
            or set(record) - {'pane', 'kind', 'armed_at', 'deadline', 'turn', 'session_id'}
            or record.get('pane') != '%' + name[:-5] or not isinstance(record.get('kind'), str)
            or record['kind'] not in ('done', 'status', 'when_free')
            or not isinstance(record.get('turn'), str) or not record['turn']
            or ('session_id' in record and (not isinstance(record['session_id'], str) or not record['session_id']))
            or not store.epoch(record.get('armed_at'))
            or (record.get('deadline') is not None and not store.epoch(record['deadline']))):
        os.unlink(name, dir_fd=fd)
        return None
    return record


def arm(pane, kind='done', *, now=None, deadline=None, session_id=None):
    """Best effort; terminal jv calls without a voice turn never arm watches."""
    turn = os.environ.get('HEARDBACK_TURN')
    if not turn:
        return False
    try:
        name = _filename(pane)
        if kind not in ('done', 'status', 'when_free'):
            return False
        record = dict(pane=pane, kind=kind, armed_at=store.now_iso(now),
                      deadline=deadline, turn=turn)
        if session_id is not None:
            record['session_id'] = session_id
        with _directory(create=True) as fd:
            try:
                _read(fd, name)
            except FileNotFoundError:
                pass
            tmp = '.' + uuid.uuid4().hex + '.tmp'
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            try:
                with os.fdopen(out, 'w') as f:
                    os.fchmod(f.fileno(), 0o600)
                    json.dump(record, f)
                os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(tmp, dir_fd=fd)
                except FileNotFoundError:
                    pass
        return True
    except (OSError, ValueError):
        return False


def discard(pane):
    """A full status request replaces any earlier watch, including an expired one."""
    try:
        with _directory() as fd:
            name = _filename(pane)
            _read(fd, name)
            os.unlink(name, dir_fd=fd)
        return True
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False


def take(pane, event_t, *, now):
    """Unlink before returning; stale events leave newer watches in place."""
    try:
        with _directory() as fd:
            name = _filename(pane)
            record = _read(fd, name)
            if record is None or store.epoch(event_t) < store.epoch(record['armed_at']):
                return None
            if _expired(record, now) and record['kind'] != 'done':
                return None  # due() owns timeout speech, including a Stop arriving after the deadline
            os.unlink(name, dir_fd=fd)
            return record if not _expired(record, now) else None
    except (OSError, ValueError):
        return None


def peek(pane):
    try:
        with _directory() as fd:
            return _read(fd, _filename(pane))
    except (OSError, ValueError):
        return None


def pending(pane=None):
    """Avoid session-store reads for events when there are no watches."""
    try:
        with _directory() as fd:
            if pane is not None:
                return _read(fd, _filename(pane)) is not None
            return any(re.fullmatch(r'\d+\.json', name) for name in os.listdir(fd))
    except (OSError, ValueError):
        return False


def _expired(record, now):
    return (now >= store.epoch(record['armed_at']) + DONE_S if record['kind'] == 'done'
            else record['deadline'] is not None and now >= store.epoch(record['deadline']))


def expire(now):
    """Unlink expired watches once; the announcer formats status timeout lines."""
    expired = []
    try:
        with _directory() as fd:
            for name in os.listdir(fd):
                if not re.fullmatch(r'\d+\.json', name):
                    continue
                try:
                    record = _read(fd, name)
                    if record and (record['kind'] != 'done' and not record.get('session_id') or _expired(record, now)):
                        os.unlink(name, dir_fd=fd)
                        if record.get('session_id') and record['kind'] != 'done':
                            expired.append(record)
                except OSError:
                    continue
    except OSError:
        pass
    return expired
