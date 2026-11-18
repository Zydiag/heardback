"""Task 5: fake panes, scratch transcripts and voice; no desktop or provider IO."""
import asyncio
import json
import time

import pytest

from event_voice import due as deliver_events

from heardback.status import announce, cli, store, watch
from heardback.status.hook import first_sentence
from jv import tmux
from test_attach import pane

T0 = 1_800_000_000.0
RULE = '─' * 40


def box(text=''):
    return f'Old reply.\n{RULE}\n❯ {text}\n{RULE}\n  ? for shortcuts\n'


@pytest.mark.parametrize('terminator', ['\x1b\\', '\x07'])
def test_prompt_draft_ignores_osc_links_outside_box(monkeypatch, terminator):
    raw = f'Old \x1b]8;id=x;https://example.com{terminator}reply\x1b]8;;{terminator}\n{RULE}\n❯\u00a0\n{RULE}\n'
    monkeypatch.setattr(tmux, 'tmux', lambda *args: raw)
    assert tmux.prompt_draft('%3') is False


def test_prompt_draft_keeps_link_text_inside_box(monkeypatch):
    raw = f'Old reply\n{RULE}\n❯ draft \x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\\n{RULE}\n'
    monkeypatch.setattr(tmux, 'tmux', lambda *args: raw)
    assert tmux.prompt_draft('%3') is True


def test_prompt_draft_detects_typed_text_after_link_above(monkeypatch):
    raw = f'Old \x1b]8;;https://example.com\x1b\\reply\x1b]8;;\x1b\\\n{RULE}\n❯ typed draft\n{RULE}\n'
    monkeypatch.setattr(tmux, 'tmux', lambda *args: raw)
    assert tmux.prompt_draft('%3') is True


@pytest.mark.parametrize('control', ['\x07', '\x1b[?25l'])
def test_prompt_draft_still_rejects_other_controls(monkeypatch, control):
    monkeypatch.setattr(tmux, 'tmux', lambda *args: f'Old {control}\n{box()}')
    assert tmux.prompt_draft('%3') is None


def due(world):
    return deliver_events(world['ann'], muted=world['muted'], at_desk=not world.get('away', False),
                          busy=world.get('busy', False))


def path():
    return store.state_dir(create=False) / 'watches/3.json'


@pytest.fixture
def world(monkeypatch, tmp_path):
    s = dict(session_id='s', project='project', state='idle', tmux_pane='%3', tmux_target='0:3.0')
    data = dict(now=T0, sessions=[s], capture=box(), sent=[], muted=False, clients='')
    monkeypatch.setenv('HEARDBACK_TURN', 'turn-1')
    monkeypatch.delenv('HEARDBACK_TAINTED', raising=False)
    monkeypatch.setattr(store, 'list_sessions', lambda **kw: data['sessions'])
    monkeypatch.setattr(tmux, 'load_sessions', lambda **kw: data['sessions'])
    monkeypatch.setattr(tmux, 'panes', lambda: [pane(3, cmd='claude')])
    def call(*args, **kw):
        if args[0] == 'list-clients':
            return data['clients']
        if args[0] == 'capture-pane':  # a running turn draws its spinner unless the test interrupted it
            running = data['sessions'][0]['state'] == 'working' and not data.get('interrupted')
            return ('✻ Wandering… (1m 18s · ↓ 4.4k tokens)\n' if running else '') + data['capture']
        data['sent'].append((args, kw))
        return ''
    monkeypatch.setattr(tmux, 'tmux', call)
    monkeypatch.setattr(time, 'time', lambda: data['now'])
    data['ann'] = announce.Announcer(now=lambda: data['now'], sessions=lambda: data['sessions'])
    data['transcript'] = tmp_path / 'transcript.ndjson'
    s['transcript_path'] = str(data['transcript'])
    return data


def ask(run_jv_with, *flags):
    return run_jv_with(cli.VERBS, 'status', 'ask', '3', '--json', *flags)


def confirmed(run_jv_with):
    code, out, _ = ask(run_jv_with, '--dry-run')
    return ask(run_jv_with, '--yes', '--source-revision', out['source_revision'])


def reply(world, text, **kw):
    world['transcript'].write_text(json.dumps({'type': 'assistant', 'message': {'content': text}}) + '\n')
    kw.setdefault('detail', first_sentence(text))
    stop(world, **kw)


def stop(world, **kw):
    world['ann'].feed(dict(session_id='s', hook='Stop', t=store.now_iso(world['now'] + 1),
                           **{'from': 'working', 'to': 'idle', 'turn_s': 60, 'detail': 'Finished.'}) | kw)


def armed(world, run_jv_with):
    assert ask(run_jv_with)[0] == 0


def test_ask_idle_types_line(world, run_jv_with):
    code, out, _ = ask(run_jv_with)
    assert code == 0 and out['say'] == "Asking window 3; I'll read out its answer."
    assert world['sent'] == [(('send-keys', '-t', '%3', '-l', '--', watch.STATUS_LINE), {}), (('send-keys', '-t', '%3', 'Enter'), {})]
    record = json.loads(path().read_text())
    assert record['kind'] == 'status' and store.epoch(record['deadline']) == T0 + 90


def test_ask_tainted_readback(world, monkeypatch, run_jv_with):
    monkeypatch.setenv('HEARDBACK_TAINTED', '1')
    code, out, _ = ask(run_jv_with)
    assert code == 3 and out['confirm'] == 'Ask the Claude session in window 3 for its status?'
    assert not world['sent'] and not path().exists()


@pytest.mark.parametrize('state', ['working', 'waiting'])
def test_ask_busy_offer(world, run_jv_with, state):
    world['sessions'][0]['state'] = state
    world['sessions'][0]['waiting_for'] = 'permission'
    code, out, _ = ask(run_jv_with)
    assert code == 3 and out['confirm'].endswith("Ask it when it's free?")
    assert ('working' if state == 'working' else 'permission') in out['confirm']
    assert not world['sent'] and not path().exists()


def test_ask_offer_yes_now_idle(world, run_jv_with):
    world['sessions'][0]['state'] = 'working'
    code, offer, _ = ask(run_jv_with)
    assert code == 3
    world['sessions'][0]['state'] = 'idle'
    assert ask(run_jv_with, '--yes', '--source-revision', offer['source_revision'])[0] == 0
    assert world['sent'] and json.loads(path().read_text())['kind'] == 'status'


def test_ask_offer_yes_still_busy(world, run_jv_with):
    world['sessions'][0]['state'] = 'working'
    code, out, _ = confirmed(run_jv_with)
    assert code == 0 and out['say'] == "I'll ask window 3 when it's free."
    record = json.loads(path().read_text())
    assert record['kind'] == 'when_free' and store.epoch(record['deadline']) == T0 + 1800
    assert not world['sent']


def fire_free(world, run_jv_with, monkeypatch):
    from heardback.brain import catalogue
    world['sessions'][0]['state'] = 'working'
    assert confirmed(run_jv_with)[0] == 0
    world['sessions'][0]['state'] = 'idle'
    calls = []
    async def run(argv, **kw):
        calls.append((argv, kw))
        code, out, err = run_jv_with(cli.VERBS, *argv[1:])
        return code, json.dumps(out), err
    monkeypatch.setattr(catalogue, 'run_jv', run)
    stop(world)
    asyncio.run(world['ann'].ask_free())
    return calls


def test_when_free_fires(world, run_jv_with, monkeypatch):
    calls = fire_free(world, run_jv_with, monkeypatch)
    assert calls == [(['jv', 'status', 'ask', '%3', '--yes', '--json', '--expect-session', 's'], {'turn': 'turn-1'})]
    assert json.loads(path().read_text())['kind'] == 'status'
    assert len(world['sent']) == 2
    stop(world)  # an old Stop cannot consume a newly armed status watch twice
    asyncio.run(world['ann'].ask_free())
    assert len(calls) == 1


def test_when_free_draft(world, run_jv_with, monkeypatch):
    world['capture'] = box('my draft')
    fire_free(world, run_jv_with, monkeypatch)
    assert not world['sent'] and not path().exists()
    assert due(world) == ["Window 3 has something typed in its prompt; I didn't ask."]


def test_when_free_expiry(world, run_jv_with):
    world['sessions'][0]['state'] = 'working'
    assert confirmed(run_jv_with)[0] == 0
    world['now'] += 1800
    assert due(world) == ["Window 3 is still busy after 30 minutes; I didn't ask it."]
    assert not world['sent'] and not path().exists() and due(world) == []


def test_when_free_replaced_session(world, run_jv_with, monkeypatch):
    from heardback.brain import catalogue
    world['sessions'][0]['state'] = 'working'
    assert confirmed(run_jv_with)[0] == 0
    world['sessions'][0]['session_id'] = 'replacement'
    world['sessions'][0]['state'] = 'idle'
    async def forbidden(*a, **kw):
        pytest.fail('replacement must not receive a jv submit')
    monkeypatch.setattr(catalogue, 'run_jv', forbidden)
    stop(world, session_id='replacement')
    asyncio.run(world['ann'].ask_free())
    lines = due(world)
    assert len(lines) == 1 and 'changed' in lines[0]
    assert not world['sent'] and not path().exists()


@pytest.mark.parametrize('text', ['my draft', 'first line\n  second line', '\n  second line',
                                 '\x1b[32mTry "fix a bug"\x1b[0m', '\x1b[37mTry "fix a bug"\x1b[0m', '\x1b[90m[Pasted text #1 +20 lines]', '\x1b[38;2;200;200;200mrm the branch', '\x1b[38;5;250mreal draft', '\x1b[2mmy draft'])
def test_ask_draft_refused(world, run_jv_with, text):
    world['capture'] = box(text)
    assert ask(run_jv_with)[0] == 4
    assert not world['sent'] and not path().exists()


def test_ask_box_unreadable(world, run_jv_with):
    world['capture'] = 'just output\n❯ not a clearly bounded prompt'
    code, out, _ = ask(run_jv_with)
    assert code == 4 and "can't see window 3's prompt box clearly" in out['say']
    assert not world['sent']


@pytest.mark.parametrize('text', ['\x1b[2mTry "fix a bug"\x1b[22m', '\x1b[90mTry "fix a bug"\x1b[0m',
                                 '\x1b[38;5;244mTry "fix a bug"\x1b[0m',
                                 '\x1b[38;2;120;120;120mTry "fix a bug"\x1b[0m', '   \n    '])
def test_ask_placeholder_or_whitespace_ok(world, run_jv_with, text):
    world['capture'] = box(text)
    assert ask(run_jv_with)[0] == 0 and len(world['sent']) == 2


def test_ask_not_claude(world, run_jv_with):
    world['sessions'].clear()
    code, out, _ = ask(run_jv_with)
    assert code == 1 and out['say'] == "There's no Claude Code session in window 3 I can ask."
    assert not world['sent']


def test_ask_ended(world, run_jv_with):
    world['sessions'][0]['state'] = 'ended'
    code, out, _ = ask(run_jv_with)
    assert code == 0 and 'ended' in out['say'] and not world['sent']


@pytest.mark.parametrize('guard', ['trusting', 'starting', 'showing_prompt'])
def test_ask_pane_guards(world, run_jv_with, monkeypatch, guard):
    monkeypatch.setattr(tmux, guard, lambda *a: True)
    assert ask(run_jv_with)[0] == 4 and not world['sent']


def test_ask_own_manager_pane(world, run_jv_with, monkeypatch):
    monkeypatch.setenv('TMUX_PANE', '%3')
    assert ask(run_jv_with)[0] == 4 and not world['sent']


def test_ask_full_idle(world, run_jv_with):
    code, out, _ = ask(run_jv_with, '--full')
    assert code == 0 and out['say'] == 'Sent status to window 3.'
    assert world['sent'] == [(('send-keys', '-t', '%3', '-l', '--', 'status'), {}), (('send-keys', '-t', '%3', 'Enter'), {})]
    assert not path().exists()


def test_ask_full_busy(world, run_jv_with):
    world['sessions'][0]['state'] = 'working'
    code, out, _ = ask(run_jv_with, '--full', '--yes')
    assert code == 0 and 'working' in out['say'] and not world['sent'] and not path().exists()


def test_status_reply_spoken(world, run_jv_with):
    armed(world, run_jv_with)
    reply(world, 'I fixed the bug. Next I will check the caller.')
    assert due(world) == ['Window 3 says: I fixed the bug. Next I will check the caller.']
    assert not path().exists() and due(world) == []


def test_status_reply_table(world, run_jv_with):
    armed(world, run_jv_with)
    reply(world, '| Name | State |\n| --- | --- |\n| Work | done |\nThe fix is ready.')
    assert due(world) == ['Window 3 says: The fix is ready.']


def test_status_reply_all_table(world, run_jv_with):
    armed(world, run_jv_with)
    reply(world, '| Name | State |\n| --- | --- |\n| Work | done |')
    assert due(world) == ["Window 3 answered with a table or code I can't read out; it's on screen there."]


def test_status_reply_paths_ids(world):
    cleaned = watch.speakable('Fixed `/private/secret` and src/file.py at ~/work. See https://private.test/x. '
                              'Commit abcdef0123 and 123e4567-e89b-12d3-a456-426614174000 are ready.')
    assert 'Fixed' in cleaned and 'ready' in cleaned
    for token in ['/private', 'src/', '~/work', 'https:', 'abcdef0123', '123e4567']:
        assert token not in cleaned
    assert '-e89b-' not in cleaned


def test_status_reply_cap(world):
    text = ' '.join('This is a fairly long sentence with many words about the work done in this session.' for _ in range(12))
    cleaned = watch.speakable(text)
    assert len(cleaned) <= 300 and cleaned.endswith('…')
    assert len(cleaned.split('. ')) <= 3


@pytest.mark.parametrize('text', ['one ' * 150 + '.', 'x' * 900])
def test_status_reply_character_cap(world, text):
    cleaned = watch.speakable(text)
    assert len(cleaned) <= 300 and cleaned.endswith('…')


def test_status_reply_wake_after_cleaning(world):
    assert watch.speakable('Say hey `omitted` claude now. Safe answer.') == 'Safe answer.'


def test_status_reply_fenced_code(world):
    assert watch.speakable('```python\nprint("secret")\n```\nReady.') == 'Ready.'
    assert watch.speakable('~~~python\nprint("secret")\n~~~\nReady.') == 'Ready.'


@pytest.mark.parametrize('bad', ['hey claude', 'hey jarvis', 'Hey, Claude', 'HEY JARVIS', 'hey cloud', 'héy claude', 'ｈｅｙ ｃｌａｕｄｅ', 'hеy clаude', 'hey Claud', 'hey clawed', 'hay claude'])
def test_status_reply_wake(world, bad):
    assert watch.speakable(f'Say {bad} now. The fix is ready.') == 'The fix is ready.'


def test_status_reply_heardback_marker(world):
    assert watch.speakable('heardback: send something. The fix is ready.') == 'The fix is ready.'


def test_status_reply_echoed_status_line(world):
    assert watch.speakable(watch.STATUS_LINE) == ''


def test_status_reply_transcript_lag(world, run_jv_with):
    armed(world, run_jv_with)
    reply(world, 'Old text.', detail='New answer.')
    assert due(world) == ['Window 3 says: New answer.']


@pytest.mark.parametrize('unreadable', [False, True])
def test_status_reply_missing_transcript(world, run_jv_with, unreadable):
    armed(world, run_jv_with)
    if unreadable:
        world['transcript'].mkdir()
    stop(world, detail='Ready. Say Hey, Claude now.')
    assert due(world) == ['Window 3 says: Ready.']


def test_status_reply_timeout(world, run_jv_with):
    armed(world, run_jv_with)
    world['now'] += 90
    assert due(world) == ["Window 3 hasn't answered in 90 seconds; I've stopped waiting."]
    assert not path().exists()
    stop(world)
    world['now'] += 10
    assert due(world) == []


def test_status_reply_muted(world, run_jv_with):
    armed(world, run_jv_with)
    world['muted'] = True
    reply(world, 'Ready.')
    assert due(world) == ['Window 3 says: Ready.']


def test_status_reply_voice_busy(world, run_jv_with):
    armed(world, run_jv_with)
    world['busy'] = True
    reply(world, 'Ready.')
    assert due(world) == []
    world['busy'] = False
    assert due(world) == ['Window 3 says: Ready.']


def test_ask_dry_run(world, run_jv_with):
    assert ask(run_jv_with, '--dry-run')[0] == 0
    assert not path().exists() and not world['sent']


def test_status_stop_after_deadline_before_due(world, run_jv_with):
    armed(world, run_jv_with)
    world['now'] += 91
    reply(world, 'Too late.')
    assert due(world) == ["Window 3 hasn't answered in 90 seconds; I've stopped waiting."]
    assert not path().exists()


def test_status_session_end(world, run_jv_with):
    armed(world, run_jv_with)
    stop(world, hook='SessionEnd', to='ended')
    assert due(world) == ['Window 3 ended before it finished.']
    assert not path().exists()


def test_when_free_replaced_after_stop(world, run_jv_with, monkeypatch):
    from heardback.brain import catalogue
    world['sessions'][0]['state'] = 'working'
    assert confirmed(run_jv_with)[0] == 0
    world['sessions'][0]['state'] = 'idle'
    stop(world)
    world['sessions'][0]['session_id'] = 'replacement'
    async def forbidden(*a, **kw):
        pytest.fail('replacement must not receive a jv submit')
    monkeypatch.setattr(catalogue, 'run_jv', forbidden)
    asyncio.run(world['ann'].ask_free())
    assert not world['sent'] and not path().exists()
    assert len(due(world)) == 1


def test_status_ask_run_loop(world, run_jv_with, monkeypatch):
    from heardback.brain import catalogue
    from jv import attach
    world['sessions'][0]['state'] = 'working'
    assert confirmed(run_jv_with)[0] == 0
    world['sessions'][0]['state'] = 'idle'
    async def run(argv, **kw):
        rc, out, err = run_jv_with(cli.VERBS, *argv[1:])
        return rc, json.dumps(out), err
    monkeypatch.setattr(catalogue, 'run_jv', run)
    monkeypatch.setattr(store, 'gc', lambda: {})
    monkeypatch.setattr(attach, 'prune', lambda: None)
    class Tail:
        def read_new(self):
            return [dict(session_id='s', hook='Stop', t=store.now_iso(T0 + 1), to='idle')]
    said = []
    async def say(line):
        said.append(line["text"])
    async def sleep(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(announce.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(announce.run(world['ann'], say, Tail()))
    assert len(world['sent']) == 2 and json.loads(path().read_text())['kind'] == 'status'
    assert said == ["Asking window 3; I'll read out its answer."]


def test_full_status_with_previous_watch(world, run_jv_with):
    armed(world, run_jv_with)
    assert ask(run_jv_with, '--full')[0] == 0
    reply(world, 'This is the full status answer.')
    assert due(world) == []
    assert not path().exists()


def test_when_free_replaced_at_subprocess_boundary(world, run_jv_with, monkeypatch):
    from heardback.brain import catalogue
    world['sessions'][0]['state'] = 'working'
    assert confirmed(run_jv_with)[0] == 0
    world['sessions'][0]['state'] = 'idle'
    stop(world)
    async def run(argv, **kw):
        assert argv == ['jv', 'status', 'ask', '%3', '--yes', '--json', '--expect-session', 's']
        world['sessions'][0]['session_id'] = 'replacement'
        rc, out, err = run_jv_with(cli.VERBS, *argv[1:])
        return rc, json.dumps(out), err
    monkeypatch.setattr(catalogue, 'run_jv', run)
    asyncio.run(world['ann'].ask_free())
    assert not world['sent']
    assert not path().exists()
    assert due(world) == ["Window 3's Claude session changed; I didn't ask."]


@pytest.mark.parametrize('kind', ['done', 'status', 'when_free'])
def test_full_status_removes_watch_before_submit(world, run_jv_with, monkeypatch, kind):
    assert watch.arm('%3', kind, now=T0, deadline=store.now_iso(T0 - 1), session_id='s')
    submit = tmux._status_submit
    def checked(*args):
        assert not path().exists()
        submit(*args)
    monkeypatch.setattr(tmux, '_status_submit', checked)
    assert ask(run_jv_with, '--full')[0] == 0
    assert not path().exists()


def test_full_status_dry_run_preserves_watch(world, run_jv_with):
    armed(world, run_jv_with)
    world['sent'].clear()
    before = path().read_bytes()
    assert ask(run_jv_with, '--full', '--dry-run')[0] == 0
    assert path().read_bytes() == before and not world['sent']


def test_full_status_clear_failure_refuses(world, run_jv_with):
    directory = store.state_dir() / 'watches'
    directory.write_text('not a directory')
    code, out, _ = ask(run_jv_with, '--full')
    assert code == 4 and 'watch' in out['say'] and not world['sent']


@pytest.mark.parametrize('state', ['idle', 'working', 'waiting', 'ended'])
def test_expect_session_mismatch_refuses(world, run_jv_with, state):
    world['sessions'][0]['state'] = state
    code, out, _ = ask(run_jv_with, '--expect-session', 'other', '--yes')
    assert code == 4 and out['say'] == "Window 3's Claude session changed; I didn't ask."
    assert not world['sent'] and not path().exists()


def test_expect_session_matches(world, run_jv_with):
    assert ask(run_jv_with, '--expect-session', 's')[0] == 0
    assert len(world['sent']) == 2 and json.loads(path().read_text())['kind'] == 'status'


def test_expect_session_schema_is_internal():
    from jv.cli import catalogue
    entry = next(e for e in catalogue(cli.VERBS)['verbs'] if e['name'] == 'status.ask')
    assert entry['params']['properties']['expect_session']['type'] == 'string'
    assert 'internal' in entry['params']['properties']['expect_session']['description'].lower()
    assert '--expect-session' in entry['argv'] and 'status of window {who:str}' in entry['phrases']


@pytest.mark.parametrize('mode', ['reply', 'timeout', 'table'])
def test_status_reply_not_in_claude(world, run_jv_with, monkeypatch, tmp_path, mode):
    from heardback.brain import brain, catalogue, mcp_jv, router
    from jv import cli as jv_cli

    # Run the actual MCP -> jv command boundary in process, with the same fake panes.
    class Process:
        def __init__(self, argv, **kw):
            self.returncode, out, self.err = run_jv_with(cli.VERBS, *argv[1:])
            self.out = json.dumps(out)
        def communicate(self, timeout=None):
            return self.out, self.err
    cat = {e['name']: e for e in jv_cli.catalogue(cli.VERBS)['verbs']}
    monkeypatch.delenv('HEARDBACK_EXECUTION_GATE', raising=False)
    catalogue.set_current_turn('turn-1')
    with monkeypatch.context() as m:
        m.setattr(mcp_jv.subprocess, 'Popen', Process)
        result = mcp_jv.call(cat, 'status_ask', {'who': '3'})
    assert not result['isError']
    tool_text = result['content'][0]['text']
    assert json.loads(tool_text) == {'ok': True, 'say': "Asking window 3; I'll read out its answer."}

    monkeypatch.setattr(catalogue, 'get', lambda: cat)
    async def run(argv, *a, **kw):
        rc, out, err = run_jv_with(cli.VERBS, *argv[1:])
        return rc, json.dumps(out), err
    monkeypatch.setattr(catalogue, '_run_jv', run)
    b = brain.Brain(claude_cmd=['fake-claude'], MIN_TURN_GAP_S=0)
    b.router = router.Router(cat)
    b.allowed = b._allowed(cat)

    class FakeClaude:
        alive, execution_owner = True, 'fake-owner'
        def __init__(self):
            self.inputs, self.responses = [], []
            self.sent = asyncio.Event()
        async def send(self, text):
            self.inputs.append(text)
            self.sent.set()
            return True
        async def respond(self, rid, response):
            self.responses.append(response)

    class Connection:
        def __init__(self):
            self.messages = []
            self.done = asyncio.Event()
        def is_closing(self):
            return False
        def write(self, raw):
            message = json.loads(raw)
            self.messages.append(message)
            if message['type'] == 'done':
                self.done.set()
        async def drain(self):
            pass

    async def go():
        conn, voice = Connection(), Connection()
        b.voice = voice
        fake = b.claude = FakeClaude()
        try:
            await b.to_claude('Ask for the status of window 3.', conn, 'request')
            await asyncio.wait_for(fake.sent.wait(), 1)
            await b._on_event({'type': 'assistant', 'message': {'content': [{
                'type': 'tool_use', 'id': 'status', 'name': 'mcp__jv__status_ask', 'input': {'who': '3'}}]}})
            await b._can_use_tool(fake, b.turns[0], 'permission', {
                'tool_name': 'mcp__jv__status_ask', 'input': {'who': '3'}})
            assert fake.responses[-1]['behavior'] == 'allow'
            await b._on_event({'type': 'user', 'message': {'content': [{
                'type': 'tool_result', 'tool_use_id': 'status', 'content': tool_text}]}})
            await b._on_event({'type': 'result', 'subtype': 'success', 'result': 'Requested.', 'usage': {}})
            await b._speak_jv(router.Route('status.ask', {'who': '3'}, 1.0), 0, tool_text, '', conn, 'ask status')
            if mode == 'timeout':
                world['now'] += 90
            else:
                reply(world, 'Confidential zebra reply.' if mode == 'reply' else '| Secret | Zebra |')
            lines = world['ann'].due()
            assert len(lines) == 1
            await b.announce(lines[0])
            assert voice.messages[-1] == {'type': 'say', 'text': lines[0]['text'], 'final': True, 'announce': True}
            fake.sent.clear()
            await b.to_claude('explain a simple concept', conn, 'next')
            await asyncio.wait_for(fake.sent.wait(), 1)
            forbidden = ['Confidential zebra reply', '| Secret | Zebra |', lines[0]['text']]
            inputs = json.dumps(fake.inputs + fake.responses)
            trace = (store.state_dir() / 'trace.jsonl').read_text()
            for text in forbidden:
                assert text not in inputs and text not in trace and text not in repr(b.recent)
                assert text not in json.dumps(result)
        finally:
            b.claude = None
            if b._idle:
                b._idle.cancel()
            for task in list(b._tasks):
                task.cancel()
            await asyncio.gather(*b._tasks, return_exceptions=True)
    asyncio.run(go())


@pytest.mark.parametrize('token', [
    'sk-ant-api03-AbCdEf123456GhIjKl789', 'ghp_A1b2C3d4E5f6G7h8I9j0',
    'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature',
    'user@example.com', r'C:\Users\me\secret.txt', 'Example123456'])
def test_secret_tokens_removed(world, token):
    assert watch.speakable(f'Ready {token} now.') == 'Ready now.'


@pytest.mark.parametrize('state', ['working', 'waiting'])
def test_deferred_busy_never_rearms(world, run_jv_with, state):
    world['sessions'][0]['state'] = state
    code, out, _ = ask(run_jv_with, '--yes', '--expect-session', 's')
    assert code == 4 and out['say'] == "Window 3 is busy again; I didn't ask."
    assert not path().exists() and not world['sent']


def test_full_busy_discards_watch(world, run_jv_with):
    world['sessions'][0]['state'] = 'working'
    assert confirmed(run_jv_with)[0] == 0
    assert ask(run_jv_with, '--full')[0] == 0
    assert not path().exists() and not world['sent']


@pytest.mark.parametrize('revision', [None, 'status:' + '0' * 64])
def test_busy_requires_matching_revision(world, run_jv_with, revision):
    world['sessions'][0]['state'] = 'working'
    flags = ['--source-revision', revision] if revision else []
    code, out, _ = ask(run_jv_with, '--yes', *flags)
    assert code == 3 and out['confirm'].endswith("Ask it when it's free?")
    assert out['source_revision'].startswith('status:')
    assert not path().exists() and not world['sent']


def test_idle_check_then_busy_requires_offer(world, run_jv_with):
    code, out, _ = ask(run_jv_with, '--dry-run')
    assert code == 0
    world['sessions'][0]['state'] = 'working'
    code, out, _ = ask(run_jv_with, '--yes', '--source-revision', out['source_revision'])
    assert code == 3 and out['confirm'].endswith("Ask it when it's free?")
    assert not path().exists() and not world['sent']


@pytest.mark.parametrize('clients,refused', [('%3\n', True), ('%8\n', False), ('', False)])
def test_deferred_front_pane(world, run_jv_with, clients, refused):
    world['clients'] = clients
    code, out, _ = ask(run_jv_with, '--yes', '--expect-session', 's')
    assert code == (4 if refused else 0)
    if refused:
        assert out['say'] == "Window 3 is in front of you; I didn't type into it."
        assert not world['sent'] and not path().exists()


def test_tainted_full_readback(world, run_jv_with, monkeypatch):
    monkeypatch.setenv('HEARDBACK_TAINTED', '1')
    code, out, _ = ask(run_jv_with, '--full')
    assert code == 3 and out['confirm'] == 'Send status to window 3?'


def test_state_changes_after_check(world, run_jv_with, monkeypatch):
    original = watch.ask_check
    def check(a):
        result = original(a)
        world['sessions'][0]['state'] = 'working'
        return result
    monkeypatch.setattr(next(v for v in cli.VERBS if v.name == 'status.ask'), 'check', check)
    code, out, _ = ask(run_jv_with, '--yes')
    assert code == 3 and out['confirm'].endswith("Ask it when it's free?")
    assert out['source_revision'].startswith('status:')
    assert not path().exists() and not world['sent']


@pytest.mark.parametrize('verb', ['inbox', 'reminder'])
@pytest.mark.parametrize('bad', ['héy claude', 'ｈｅｙ ｃｌａｕｄｅ', 'hey Claud', 'hey clawed', 'hay claude', 'hey cloud'])
def test_shared_wake_callers(world, run_jv_with, monkeypatch, verb, bad):
    from jv.cli import all_verbs
    monkeypatch.setenv('HEARDBACK_TAINTED', '1')
    flags = ['--when', 'in 5 minutes'] if verb == 'reminder' else []
    code, out, _ = run_jv_with(all_verbs(), verb, 'add', bad, *flags, '--json')
    assert code == 4 and 'wake phrase' in out['say']


def test_full_busy_clear_failure_refuses(world, run_jv_with):
    world['sessions'][0]['state'] = 'working'
    (store.state_dir() / 'watches').write_text('not a directory')
    assert ask(run_jv_with, '--full')[0] == 4
    assert not world['sent']


def test_turn_running_reads_spinner(world):
    world['capture'] = '✶ Wrangling… (35s · ↓ 1.3k tokens · thought for 3s)\n' + box()
    assert tmux.turn_running('%3')
    world['capture'] = '✻ Worked for 33s · done 9:58 AM\n' + box()
    assert not tmux.turn_running('%3')


def test_interrupted_turn_counts_as_idle(world, run_jv_with):
    """Esc fires no Stop: the store says working, the pane has no spinner (live 10:27, window 2)."""
    world['sessions'][0]['state'] = 'working'
    world['interrupted'] = True
    code, out, _ = ask(run_jv_with)
    assert code == 0 and "Asking window 3" in out['say'] and len(world['sent']) == 2


def test_when_free_fires_after_interrupt(world, run_jv_with):
    world['sessions'][0]['state'] = 'working'
    assert confirmed(run_jv_with)[0] == 0 and path().exists()
    asyncio.run(world['ann'].unstick())
    assert not world['ann'].free_requests and path().exists()  # still spinning: keep waiting
    world['interrupted'] = True
    world['ann'].next_unstick = 0.0
    asyncio.run(world['ann'].unstick())
    assert len(world['ann'].free_requests) == 1 and not path().exists()
