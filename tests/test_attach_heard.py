"""Previous spoken words use the existing paste and private-inbox paths."""
import asyncio
import hashlib
import json
from pathlib import Path
import stat
from unittest.mock import AsyncMock

import pytest

from heardback.brain.brain import Brain
from jv import attach, tmux
from jv.verb import runtime_path
from test_attach import io, send
from test_attach_new import argv, inbox, new_io, start
from test_tmux import desktop_config  # noqa: F401

SECRET = 'a private long request\nwith another line'


def heard(previous=SECRET, turn='first'):
    path = Path(runtime_path('heardback-heard.json'))
    path.write_text(json.dumps({'current': {'turn': 'now', 'text': 'send what I just said to Claude'},
                               'previous': None if previous is None else {'turn': turn, 'text': previous}}))
    path.chmod(0o600)
    return path


def dispatch(run_jv_with, new, flags=()):
    return start(run_jv_with, 'last_heard', flags=flags) if new else send(run_jv_with, 'last_heard', flags=flags)


def test_attach_heard_paste(io, run_jv_with):
    heard()
    assert send(run_jv_with, 'last_heard')[0] == 0
    assert io['sent'] == [(('load-buffer', '-b', 'jv', '-'), {
        'input': f'Explain it\n\n(from heardback: what you said before, {len(SECRET)} characters)\n{SECRET}'}),
        (('paste-buffer', '-b', 'jv', '-d', '-p', '-t', '%3'), {}),
        (('send-keys', '-t', '%3', 'Enter'), {})]


def test_new_attach_heard_file(new_io, run_jv_with):
    heard()
    assert start(run_jv_with, 'last_heard')[0] == 0
    path = next(inbox().iterdir())
    assert path.read_text() == SECRET and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert argv(new_io) == ['claude', '--add-dir', str(inbox()), '--',
        f'explain (from heardback: what you said before, {len(SECRET)} characters, saved at {path}; read it first)']
    assert SECRET.splitlines()[0] not in json.dumps(argv(new_io))


@pytest.mark.parametrize('new', [False, True])
@pytest.mark.parametrize('previous', [None, ''])
def test_heard_missing(new_io, run_jv_with, new, previous):
    heard(previous)
    code, out, _ = dispatch(run_jv_with, new)
    assert code == 4 and out['error'] == "I don't have what you said before"
    assert not new_io['sent'] and not inbox().exists()


@pytest.mark.parametrize('new', [False, True])
def test_heard_file_missing(new_io, run_jv_with, new):
    code, out, _ = dispatch(run_jv_with, new)
    assert code == 4 and out['error'] == "I don't have what you said before"
    assert not new_io['sent'] and not inbox().exists()


@pytest.mark.parametrize('damage', ['symlink', 'owner', 'mode', 'json', 'schema', 'directory'])
@pytest.mark.parametrize('new', [False, True])
def test_heard_unsafe_file_refused(new_io, monkeypatch, tmp_path, run_jv_with, damage, new):
    path = heard()
    if damage == 'symlink':
        target = tmp_path / 'outside.json'
        target.write_bytes(path.read_bytes())
        target.chmod(0o600)
        path.unlink()
        path.symlink_to(target)
    elif damage == 'owner':
        monkeypatch.setattr(attach.os, 'getuid', lambda: -1)
    elif damage == 'mode':
        path.chmod(0o644)
    elif damage in ('json', 'schema'):
        path.write_text('bad json' if damage == 'json' else json.dumps({'previous': {'turn': 42, 'text': SECRET}}))
    elif damage == 'directory':
        path.unlink()
        path.mkdir()
    code, out, _ = dispatch(run_jv_with, new)
    assert code == 4 and out['error'] == "I don't have what you said before"
    assert not new_io['sent'] and not inbox().exists()


@pytest.mark.parametrize('new', [False, True])
def test_heard_changed_by_trusted_utterance(new_io, run_jv_with, new):
    heard()
    _, out, _ = dispatch(run_jv_with, new, ('--dry-run',))
    assert out['source_revision'] == 'heard:' + hashlib.sha256(('heard\0first\0' + SECRET).encode()).hexdigest()
    b = Brain()
    b.utterance = AsyncMock()
    b.voice = object()
    asyncio.run(b.handle({'type': 'utterance', 'speaker_ok': True, 'text': 'another trusted request', 'turn': 'next'}, b.voice))
    code, out, _ = dispatch(run_jv_with, new, ('--yes', '--source-revision', out['source_revision']))
    assert code == 4 and 'changed after read-back' in out['error']
    assert not new_io['sent'] and not inbox().exists()


@pytest.mark.parametrize('new', [False, True])
def test_heard_request_itself_is_send_request(new_io, run_jv_with, new):
    text = 'send what I just said to Claude'
    heard(text)
    assert dispatch(run_jv_with, new)[0] == 0
    actual = next(inbox().iterdir()).read_text() if new else new_io['sent'][0][1]['input']
    assert text in actual


@pytest.mark.parametrize('new', [False, True])
@pytest.mark.parametrize('tainted', [False, True])
def test_heard_no_content_in_result(new_io, monkeypatch, run_jv_with, new, tainted):
    heard()
    monkeypatch.setenv(tmux.TAINT_ENV, '1' if tainted else '0')
    code, out, _ = dispatch(run_jv_with, new, ('--dry-run',))
    assert code == (3 if tainted else 0) and SECRET.splitlines()[0] not in json.dumps(out)
    assert not new_io['sent'] and not inbox().exists()
    if tainted:
        assert f'what you said before, {len(SECRET)} characters' in out['confirm']
    code, out, _ = dispatch(run_jv_with, new, ('--yes', '--source-revision', out['source_revision']))
    assert code == 0 and SECRET.splitlines()[0] not in json.dumps(out)


def test_new_heard_schema(new_io, run_jv_with):
    _, data, _ = run_jv_with(tmux.VERBS, 'list', '--json')
    v = next(v for v in data['verbs'] if v['name'] == 'tmux.new')
    assert 'last_heard' in v['params']['properties']['attach']['enum']
