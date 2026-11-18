"""Quick-win regressions with in-process Claude and jv boundaries."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from heardback.brain import brain, catalogue, memory
from heardback.brain.brain import Brain, Awaiting, Turn


@pytest.fixture
def rig(env, monkeypatch):
    b = Brain(claude_cmd=['fake-only'], MIN_TURN_GAP_S=0)
    proc = SimpleNamespace(alive=True, pid=123, execution_owner='fake',
                           send=AsyncMock(return_value=True), kill=AsyncMock())
    b.claude = proc
    b._send = AsyncMock()
    b._revoke_execution = lambda turn: True
    b._write_stream = lambda turn: None
    b._bg = lambda coro: coro.close()
    b.reset = AsyncMock()
    monkeypatch.setattr(memory, 'read_memory', lambda: SimpleNamespace(revision='same', status='missing', size=0, reason=None))
    b._memory_context_revision = 'same'
    monkeypatch.setattr(catalogue, 'set_current_turn', lambda *a, **kw: 'attempt')
    return b, proc


def result(tokens=45000, text='Done.', cost=0.001):
    return dict(type='result', subtype='success', result=text, usage={'input_tokens': tokens}, total_cost_usd=cost)


@pytest.mark.parametrize('last,expected', [(15000, None), (40001, 'tokens')], ids=['three-calls-sum-45k', 'last-over-40k'])
def test_last_call_context(rig, monkeypatch, last, expected):
    b, _ = rig
    turn = Turn(None, 'hello', tid='test', sent=True)
    b.turns.append(turn)
    events = []
    monkeypatch.setattr(brain.trace, 'emit', lambda *a, **kw: events.append(kw))
    async def go():
        for size in (15000, 15000, last):
            await b._on_event({'type': 'assistant', 'message': {'usage': {'input_tokens': size - 1000, 'cache_read_input_tokens': 500, 'cache_creation_input_tokens': 500}, 'content': []}})
        assert b._bound(result(), turn=turn) == expected
    asyncio.run(go())
    assert events[-1]['tokens'] == 45000 and events[-1]['context_tokens'] == last


@pytest.mark.parametrize('pending', ['correction', 'question'])
def test_reset_after_followup(rig, pending):
    b, proc = rig
    b.to_claude = AsyncMock()
    async def go():
        t = Turn(None, 'original', tid='one', sent=True, context_tokens=41000)
        if pending == 'correction':
            t.confirm_reason = 'other'
            t.terminal = True
            t.acts = [dict(state='denied', reason='other', readback='Close window 3?', answer='yes and open a new window')]
        b.turns.append(t)
        await b._on_result(t, result(text='Which one?' if pending == 'question' else 'Done.'))
        assert b.claude is proc and b._followup_reset == 'tokens'
        if pending == 'question':
            b._clear_awaiting(None, 'answered')
        follow = Turn(None, b.to_claude.await_args.args[0] if pending == 'correction' else 'the second one', tid='two', followup=True)
        b.turns.append(follow)
        await b._kick()
        proc.send.assert_awaited_once_with(follow.text)
        assert follow.sent
        jobs = []
        b._bg = jobs.append
        await b._on_result(follow, result(tokens=1))
        assert b.claude is None and not b._model_waiting()
        for job in jobs:
            await job
        b.reset.assert_awaited_once_with('tokens', proc)
    asyncio.run(go())


def test_idle_reset_still_fires(rig):
    b, _ = rig
    b.proc_turns, b._followup_reset = 1, 'tokens'
    asyncio.run(b._idle_reset())
    b.reset.assert_awaited_once_with('idle')


@pytest.mark.parametrize('heard', ['yes and open a new window', 'Yes, close it.', 'Yes close the right pane'])
def test_action_answer_never_approves_old_request(rig, heard):
    b, _ = rig
    async def go():
        t = Turn(None, 'close window 3', tid='one')
        f = asyncio.get_running_loop().create_future()
        outcome = brain.act(t, 'close', 'tmux_close')
        b.awaiting[None] = Awaiting(f, True, 'id', 'Close window 3?', time.monotonic() + 60, t, outcome=outcome)
        b._answer(None, True, heard)  # even a voice client that calls it yes cannot approve
        assert f.result() == (False, heard)
        assert outcome['readback'] == 'Close window 3?' and outcome['answer'] == heard
        assert t.terminal is True and t.confirm_reason == 'other'
    asyncio.run(go())


def test_no_is_not_a_correction(rig):
    b, proc = rig
    async def go():
        t = Turn(None, 'close window 3', tid='one')
        f = asyncio.get_running_loop().create_future()
        b.awaiting[None] = Awaiting(f, True, 'id', 'Close window 3?', time.monotonic() + 60, t)
        b._answer(None, False, 'no')
        assert t.terminal is True and t.confirm_reason == 'no'
        proc.send.assert_not_awaited()
    asyncio.run(go())


def test_fast_correction_carries_all_context(rig):
    b, _ = rig
    async def ask(conn, sentence, speaker_ok, *, owner, outcome):
        outcome.update(readback=sentence, answer='yes and open a new window')
        owner.confirm_reason = 'other'
        return False, 'yes and open a new window'
    b.ask = ask
    b.to_claude = AsyncMock()
    asyncio.run(b._confirm_fast(None, {}, None, 'close window 3', 'Close window 3?', 'original-id'))
    b.to_claude.assert_awaited_once_with('heardback asked: "Close window 3?". The user answered: "yes and open a new window". Original request: "close window 3".', None, b.to_claude.await_args.args[2], followup=True, parent='original-id')


def test_first_process_cost_and_second_turn(rig):
    b, proc = rig
    async def go():
        first = Turn(None, 'explain', tid='first')
        b.turns.append(first)
        await b._kick()
        assert b._bound(result(cost=.053), turn=first) is None
        b.turns.clear()
        second = Turn(None, 'explain more', tid='second')
        b.turns.append(second)
        await b._kick()
        assert b._bound(result(cost=.11), turn=second) == 'message-cost'
    asyncio.run(go())


def test_completed_correction_say_has_turn(rig):
    b, _ = rig
    async def go():
        t = Turn(None, 'correction', tid='time-b1', sent=True)
        b.turns.append(t)
        await b._on_result(t, result(tokens=1))
    asyncio.run(go())
    say = [call.args[1] for call in b._send.await_args_list if call.args[1]['type'] == 'say']
    assert say == [{'type': 'say', 'text': 'Done.', 'final': True, 'turn': 'time-b1'}]


def test_memory_change_waits_through_followup(rig, monkeypatch):
    b, proc = rig
    monkeypatch.setattr(memory, 'read_memory', lambda: SimpleNamespace(revision='changed', status='missing', size=0, reason=None))
    async def go():
        t = Turn(None, 'answer', tid='followup', followup=True)
        b.turns.append(t)
        await b._kick()
        assert b._memory_context_revision == 'same' and b._followup_reset == 'memory'
        proc.send.assert_awaited_once_with('answer')
        jobs = []
        b._bg = jobs.append
        await b._on_result(t, result(tokens=1))
        for job in jobs:
            await job
        b.reset.assert_awaited_once_with('memory', proc)
    asyncio.run(go())


@pytest.mark.parametrize('heard', ['yes and open a new window', 'Yes, close it.', 'Yes close the right pane'])
def test_correction_wire_keeps_process_until_followup(env, heard):
    from brainutil import FAKE, _inputs, pids, script, serving, talk, utt, user_messages
    from heardback.brain.brain import sock_path
    sentence = 'Close 7 tabs?'
    script(env, {'zen close-unused --dry-run': {'rc': 3, 'stdout': '{"confirm":"Close 7 tabs?"}'}})
    b = Brain(claude_cmd=FAKE, TURN_RESET=1)
    async def go():
        async with serving(b, sock_path()):
            return await talk(sock_path(), [utt('cue:tool mcp__jv__zen_close-unused {}')], count=1,
                              replies={'confirm': {'type': 'confirm_reply', 'ok': True, 'text': heard, 'speaker_ok': True}})
    out = asyncio.run(go())
    messages = user_messages(env)
    assert len(messages) == 2
    assert brain.correction(sentence, heard, 'cue:tool mcp__jv__zen_close-unused {}') in messages[-1]
    assert len(pids(env)) == 2 and len(set(pids(env))) == 1
    decisions = [m['response']['response'] for _, m in _inputs(env) if m.get('type') == 'control_response']
    assert decisions and all(d['behavior'] == 'deny' for d in decisions)
    assert any(m.get('type') == 'say' and '-b' in m.get('turn', '') for m in out)


def test_error_result_with_correction_defers_reset(rig):
    b, proc = rig
    async def go():
        t = Turn(None, 'original', tid='one', sent=True, terminal=True, confirm_reason='other',
                 acts=[dict(state='denied', reason='other', readback='Close?', answer='corrected request')])
        b.turns.append(t)
        await b._on_result(t, {**result(tokens=1), 'subtype': 'error_during_execution', 'is_error': True})
        assert b.claude is proc and b._followup_reset == 'error-error_during_execution'
        b.reset.assert_not_awaited()
    asyncio.run(go())
