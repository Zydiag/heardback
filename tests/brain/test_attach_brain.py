"""Attachment permissions and trusted heard history, with fake process boundaries."""
import asyncio
import json
import os
from pathlib import Path
import stat
from unittest.mock import AsyncMock

import pytest

from brainutil import FAKE, FIX, _inputs, run_brain, script, utt
from heardback.brain import brain, catalogue
from jv import cli, tmux
from jv.verb import runtime_path

KINDS = ['tab', 'clipboard', 'selection', 'pane', 'last_heard']
PROMPT_LINE = 'To send this page, this tab or link, the clipboard, the selection, a window\'s output or the user\'s previous words to Claude Code, call tmux_type (a named session) or tmux_new with run claude (none named) once, with attach and only the user\'s instruction as text, under 100 characters. Never read it with ctx_ctx first and never copy it into text. "This error" or "this output" means attach pane with attach_target "current" when a terminal is on screen; otherwise ask which window. heardback reads out a session\'s answers itself; never repeat or guess what another session said.'


def entry(name):
    return next(v for v in cli.catalogue(tmux.VERBS)['verbs'] if v['name'] == name)


def args_for(name, kind):
    return {'attach': kind, **({'target': '3', 'text': 'Explain it'} if name == 'tmux.type'
                              else {'run': 'claude', 'prompt': 'Explain it'}),
            **({'attach_target': '12'} if kind == 'pane' else {})}


@pytest.fixture
def attach_env(env, monkeypatch):
    data = json.loads((FIX / 'catalogue.json').read_text())
    data['verbs'] = [v for v in data['verbs'] if v['name'] not in ('tmux.type', 'tmux.new')]
    data['verbs'] += [entry('tmux.type'), entry('tmux.new')]
    path = env / 'attach-catalogue.json'
    path.write_text(json.dumps(data))
    monkeypatch.setenv('FAKE_JV_CATALOGUE', str(path))
    return env


@pytest.mark.parametrize('name', ['tmux.type', 'tmux.new'])
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('rc', [0, 3])
@pytest.mark.parametrize('revision', [None, 42, 'clipboard:' + 'b' * 64, 'bad', 'PREFIX:' + 'a' * 64,
                                      'PREFIX:' + 'A' * 64, 'PREFIX:' + 'a' * 63, 'PREFIX:' + 'a' * 64 + '\n'])
def test_dry_run_binds_attach(env, monkeypatch, name, kind, rc, revision):
    prefix = 'heard' if kind == 'last_heard' else kind
    if isinstance(revision, str):
        revision = revision.replace('PREFIX', prefix)
    monkeypatch.setattr(catalogue, 'run_jv', AsyncMock(return_value=(rc, json.dumps({
        'confirm': 'Send it?', 'source_revision': revision}), '')))
    result = asyncio.run(brain.Brain().dry_run(entry(name), args_for(name, kind)))
    if revision in (prefix + ':' + 'a' * 64, prefix + ':' + 'b' * 64):
        assert result == (rc, 'Send it?' if rc == 3 else '', revision)
    else:
        assert result == (1, "couldn't bind the attachment to this request", None)


@pytest.mark.parametrize('name', ['tmux.type', 'tmux.new'])
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('rc', [0, 3])
def test_dry_run_valid_attach(env, monkeypatch, name, kind, rc):
    revision = ('heard' if kind == 'last_heard' else kind) + ':' + 'a' * 64
    monkeypatch.setattr(catalogue, 'run_jv', AsyncMock(return_value=(rc, json.dumps({
        'confirm': 'Send it?', 'source_revision': revision}), '')))
    assert asyncio.run(brain.Brain().dry_run(entry(name), args_for(name, kind))) == (
        rc, 'Send it?' if rc == 3 else '', revision)


@pytest.mark.parametrize('name', ['tmux.type', 'tmux.new'])
@pytest.mark.parametrize('kind', KINDS)
def test_attach_revision_passed_clean(attach_env, name, kind):
    revision = ('heard' if kind == 'last_heard' else kind) + ':' + 'a' * 64
    script(attach_env, {name.replace('.', ' ') + ' --dry-run': {
        'rc': 0, 'stdout': json.dumps({'source_revision': revision})}})
    args = args_for(name, kind)
    out = run_brain(attach_env, [utt('cue:tool mcp__jv__' + name.replace('.', '_') + ' ' + json.dumps(args))], claude_cmd=FAKE)
    assert not [m for m in out if m['type'] == 'confirm']
    responses = [m['response']['response'] for _, m in _inputs(attach_env) if m.get('type') == 'control_response']
    assert responses == [{'behavior': 'allow', 'updatedInput': {**args, 'source_revision': revision}}]


@pytest.mark.parametrize('name', ['tmux.type', 'tmux.new'])
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('tainted', [False, True])
def test_attach_no_taint(env, name, kind, tainted):
    b = brain.Brain()
    b.claude = object()
    b._tainted = b.claude if tainted else None
    before = b._tainted
    asyncio.run(b._on_event({'type': 'assistant', 'message': {'content': [{
        'type': 'tool_use', 'id': 'a', 'name': 'mcp__jv__' + name.replace('.', '_'),
        'input': args_for(name, kind)}]}}))
    asyncio.run(b._on_event({'type': 'user', 'message': {'content': [{
        'type': 'tool_result', 'tool_use_id': 'a', 'content': '{"ok":true,"say":"sent"}'}]}}))
    assert b._tainted is before


def heard_path():
    return Path(runtime_path('heardback-heard.json'))


@pytest.fixture
def intake(env):
    b = brain.Brain()
    b.utterance = AsyncMock()
    b.voice = object()
    return b


def hear(b, text, turn='one', speaker_ok=True, **extra):
    asyncio.run(b.handle({**utt(text, speaker_ok), 'turn': turn, **extra}, b.voice))


def test_heard_file_written(intake):
    hear(intake, 'a long spoken request')
    path = heard_path()
    first = path.stat()
    assert stat.S_IMODE(first.st_mode) == 0o600
    assert json.loads(path.read_text()) == {'current': {'turn': 'one', 'text': 'a long spoken request'}, 'previous': None}
    # Holding the old inode open proves replacement rather than in-place truncation.
    with path.open() as old:
        hear(intake, 'send what I just said to Claude', 'two', parent='one')
        assert path.stat().st_ino != first.st_ino
        assert json.load(old)['current']['text'] == 'a long spoken request'
    assert json.loads(path.read_text()) == {'current': {'turn': 'two', 'text': 'send what I just said to Claude'},
                                         'previous': {'turn': 'one', 'text': 'a long spoken request'}}


def test_heard_untrusted_skipped(intake):
    hear(intake, 'untrusted before history', speaker_ok=False)
    assert not heard_path().exists()
    hear(intake, 'trusted words')
    before = heard_path().read_bytes()
    hear(intake, 'someone else', 'two', speaker_ok=False)
    assert heard_path().read_bytes() == before


@pytest.mark.parametrize('text', ['yes', 'no'])
def test_heard_confirm_words_without_pending(intake, text):
    hear(intake, 'trusted request')
    before = heard_path().read_bytes()
    hear(intake, text, 'reply')
    assert heard_path().read_bytes() == before


@pytest.mark.parametrize('text', ['yes', 'no', 'yes and open another window'])
@pytest.mark.parametrize('wire', ['utterance', 'confirm_reply'])
def test_heard_confirm_reply_does_not_shift(intake, text, wire):
    hear(intake, 'trusted request')
    before = heard_path().read_bytes()
    async def go():
        question = brain.Awaiting(asyncio.get_running_loop().create_future(), True, 'id', 'Send?',
                                      asyncio.get_running_loop().time() + 10, None)
        intake.awaiting[None] = question
        await intake.handle({'type': wire, 'text': text, 'speaker_ok': True,
                             'confirmation_id': 'id', 'ok': text == 'yes'}, None)
    asyncio.run(go())
    assert heard_path().read_bytes() == before


def test_heard_correction_followup_does_not_shift(intake):
    b = intake
    b.to_claude = AsyncMock()
    b._bg = lambda coro: coro.close()
    hear(b, 'original request')
    before = heard_path().read_bytes()
    turn = brain.Turn(None, 'original request', tid='one', terminal=True,
                      acts=[dict(state='denied', reason='other', readback='Send?', answer='yes and X')])
    asyncio.run(b.finish(turn))
    b.to_claude.assert_awaited_once()
    assert heard_path().read_bytes() == before


def test_heard_worker_intake(attach_env):
    run_brain(attach_env, [{**utt('hello there'), 'turn': 'first'},
                           {**utt('another request'), 'turn': 'second'}], claude_cmd=FAKE)
    assert json.loads(heard_path().read_text()) == {
        'current': {'turn': 'second', 'text': 'another request'},
        'previous': {'turn': 'first', 'text': 'hello there'}}


@pytest.mark.parametrize('damage', ['symlink', 'owner', 'mode', 'json'])
def test_heard_unsafe_file_replaced(intake, monkeypatch, damage, tmp_path):
    path = heard_path()
    victim = tmp_path / 'victim'
    victim.write_text('leave alone')
    if damage == 'symlink':
        path.symlink_to(victim)
    else:
        path.write_text('broken' if damage == 'json' else json.dumps({
            'current': {'turn': 'old', 'text': 'untrusted old words'}, 'previous': None}))
        path.chmod(0o644 if damage == 'mode' else 0o600)
    with monkeypatch.context() as patch:
        if damage == 'owner':
            patch.setattr(os, 'getuid', lambda: -1)
        hear(intake, 'fresh trusted words')
    assert not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {'current': {'turn': 'one', 'text': 'fresh trusted words'}, 'previous': None}
    assert victim.read_text() == 'leave alone'


def test_heard_not_in_tool_input_trace(attach_env):
    secret = 'private spoken content that belongs only in the heard file'
    revision = 'heard:' + 'a' * 64
    script(attach_env, {'tmux type --dry-run': {'rc': 0, 'stdout': json.dumps({'source_revision': revision})}})
    args = args_for('tmux.type', 'last_heard')
    run_brain(attach_env, [utt(secret), utt('cue:tool mcp__jv__tmux_type ' + json.dumps(args))], claude_cmd=FAKE)
    from heardback.status.store import state_dir
    events = [json.loads(line) for line in (state_dir() / 'trace.jsonl').read_text().splitlines()]
    inputs = [e for e in events if e.get('ev') in ('tool_use', 'permission')]
    assert inputs and secret not in json.dumps(inputs)


def test_prompt_lines():
    from model_text import description
    assert PROMPT_LINE in description('tmux.type')
    assert '"Claude Code" refers to the terminal' in Path(brain.__file__).with_name('prompt.md').read_text()
