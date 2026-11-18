"""Task 4 watches; all state and desktop boundaries are isolated."""
import asyncio
import json
import os
import stat

import pytest

from event_voice import due as deliver_events

from heardback.status import announce, cli, store, watch
from heardback.status.announce import Announcer
from jv import attach, tmux
from test_attach import io
from test_tmux import desktop_config  # noqa: F401

T0 = 1_800_000_000.0


def due(world):
    return deliver_events(world['ann'], muted=world['muted'], at_desk=not world.get('away', False),
                          busy=world.get('busy', False))


def path(pane='%3'):
    return store.state_dir(create=False) / 'watches' / (pane[1:] + '.json')


@pytest.fixture
def world(monkeypatch):
    s = dict(session_id='s', project='project', state='idle', tmux_pane='%3', tmux_target='0:3.0')
    data = dict(now=T0, muted=False, away=False, sessions=[s])
    ann = Announcer(now=lambda: data['now'], sessions=lambda: data['sessions'])
    data['ann'] = ann
    monkeypatch.setenv('HEARDBACK_TURN', 'turn-1')
    return data


def stop(world, *, t=T0 + 1, hook='Stop', **kw):
    world['ann'].feed(dict(session_id='s', hook=hook, t=store.now_iso(t),
                           **{'from': 'working', 'to': 'idle', 'turn_s': 5, 'detail': 'All fixed.', **kw}))


def arm(world):
    assert watch.arm('%3', now=T0)


def test_arm_on_submit(io, monkeypatch):
    monkeypatch.setenv('HEARDBACK_TURN', 'turn')
    tmux._type(dict(target='3', text='Do it', enter=True))
    assert json.loads(path().read_text())['kind'] == 'done'


def test_no_arm_without_turn(io, monkeypatch):
    monkeypatch.delenv('HEARDBACK_TURN', raising=False)
    tmux._type(dict(target='3', text='Do it', enter=True))
    assert not path().exists()


@pytest.mark.parametrize('text,enter', [('hello', False), ('/clear', True), ('exit', True), ('quit', True), ('/exit', True), ('/quit', True)])
def test_no_arm_type_only_clear_exit(io, monkeypatch, text, enter):
    monkeypatch.setenv('HEARDBACK_TURN', 'turn')
    monkeypatch.setattr(store, 'list_sessions', lambda **kw: [])
    tmux._type(dict(target='3', text=text, enter=enter))
    assert not path().exists()


@pytest.mark.parametrize('split,prompt', [(False, 'Do it'), (False, None), (True, 'Do it'), (True, None)])
def test_arm_on_new_split_prompt(io, monkeypatch, split, prompt):
    monkeypatch.setenv('HEARDBACK_TURN', 'turn')
    monkeypatch.setattr(tmux, '_new_dir', lambda a: store.state_dir())
    monkeypatch.setattr(tmux, '_readiness', lambda p: 'pending')
    monkeypatch.setattr(tmux, 'tmux', lambda *a, **kw: ('%90' if split else '%90\t0:90.0')
                        if a[0] in ('new-window', 'split-window') else '')
    (tmux._split if split else tmux._new)(dict(run='claude', prompt=prompt, dir='horizontal', window='3'))
    assert path('%90').exists() == bool(prompt)


def test_done_spoken_at_desk(world):
    arm(world)
    stop(world)
    assert due(world) == ['Window 3 is done: All fixed.']
    assert not path().exists() and due(world) == []


def test_done_muted(world):
    arm(world)
    world['muted'] = True
    stop(world)
    assert due(world) == [] and not path().exists()


def test_done_short_turn(world):
    arm(world)
    stop(world)
    assert due(world) == ['Window 3 is done: All fixed.']


def test_done_no_double(world):
    arm(world)
    world['away'] = True
    stop(world, turn_s=60)
    world['now'] += 10
    assert due(world) == ['Window 3 is done: All fixed.']
    assert due(world) == []


def test_done_stop_failure(world):
    arm(world)
    stop(world, hook='StopFailure', detail='stopped with an error: rate limit')
    assert due(world) == ['Window 3 stopped with an error: rate limit.']


def test_done_old_stop(world):
    arm(world)
    stop(world, t=T0 - 1)
    assert due(world) == [] and path().exists()


def test_done_busy_voice(world):
    arm(world)
    world['busy'] = True
    stop(world)
    assert due(world) == []
    world['busy'] = False
    assert due(world) == ['Window 3 is done: All fixed.']


def test_done_state_moved_on(world):
    arm(world)
    stop(world)
    world['sessions'][0]['state'] = 'working'
    world['ann'].feed(dict(session_id='s', hook='UserPromptSubmit', to='working'))
    assert due(world) == ['Window 3 is done: All fixed.']


def test_done_ignores_debounce(world):
    arm(world)
    world['ann'].last_said['s'] = T0
    stop(world)
    assert due(world) == ['Window 3 is done: All fixed.']


def test_session_end(world):
    arm(world)
    stop(world, hook='SessionEnd', to='ended')
    assert due(world) == ['Window 3 ended before it finished.']


def test_watch_expiry(world):
    arm(world)
    world['now'] += 12 * 3600
    assert due(world) == [] and not path().exists()


def test_watch_newest_wins(world):
    arm(world)
    assert watch.arm('%3', now=T0 + 2)
    stop(world)
    assert path().exists()
    stop(world, t=T0 + 3)
    assert due(world) == ['Window 3 is done: All fixed.']


def test_watch_file_mode_atomic(world, monkeypatch):
    calls = []
    replace = os.replace
    def checked(src, dst, **kw):
        calls.append((src, dst))
        assert src != dst
        return replace(src, dst, **kw)
    monkeypatch.setattr(watch.os, 'replace', checked)
    arm(world)
    assert calls and stat.S_IMODE(path().stat().st_mode) == 0o600
    assert stat.S_IMODE(path().parent.stat().st_mode) == 0o700
    assert set(json.loads(path().read_text())) == {'pane', 'kind', 'armed_at', 'deadline', 'turn'}


@pytest.mark.parametrize('state', ['idle', 'ended', 'working', 'waiting'])
def test_status_watch(world, monkeypatch, run_jv_with, state):
    world['sessions'][0]['state'] = state
    monkeypatch.setattr(store, 'list_sessions', lambda: world['sessions'])
    code, result, _ = run_jv_with(cli.VERBS, 'status', 'watch', '3', '--json')
    assert code == 0
    if state in ('working', 'waiting'):
        assert result['say'] == "I'll tell you when window 3 is done." and path().exists()
    else:
        assert result['say'] == cli.speak.one(world['sessions'][0], world['sessions'], cli.time.time())
        assert not path().exists()


def test_status_watch_no_session(world, monkeypatch, run_jv_with):
    monkeypatch.setattr(store, 'list_sessions', lambda: [])
    code, result, _ = run_jv_with(cli.VERBS, 'status', 'watch', '3', '--json')
    assert code == 1 and 'no Claude Code session in window 3' in result['error']


def test_status_watch_uses_resumed_session_hook_pane(tmp_path, monkeypatch, run_jv_with):
    from heardback.status import hook
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    monkeypatch.setenv('HEARDBACK_TURN', 'turn-1')
    monkeypatch.setattr(store, 'resolve_target', lambda _pane, _socket: '0:3.0')
    pid = [1]
    monkeypatch.setattr(hook, 'find_claude_pid', lambda _ppid: pid[0])
    event = {'session_id': 'shared', 'cwd': '/x/project'}
    hook.handle({'hook_event_name': 'SessionStart', **event}, T0, {'TMUX_PANE': '%91'})
    pid[0] = 2
    hook.handle({'hook_event_name': 'SessionStart', **event}, T0 + 1, {'TMUX_PANE': '%29'})
    pid[0] = 1
    hook.handle({'hook_event_name': 'UserPromptSubmit', **event}, T0 + 2, {'TMUX_PANE': '%91'})
    monkeypatch.setattr(store, 'list_sessions', lambda **_kw: [store.read_raw('shared')])

    code, result, _ = run_jv_with(cli.VERBS, 'status', 'watch', '3', '--json')
    assert code == 0 and result['say'] == "I'll tell you when window 3 is done."
    assert json.loads(path('%91').read_text())['pane'] == '%91'


@pytest.mark.parametrize('directory', [False, True])
def test_watch_symlink_ignored_and_arm_refused(world, tmp_path, directory):
    outside = tmp_path / 'outside'
    if directory:
        outside.mkdir()
        path().parent.parent.mkdir(parents=True, exist_ok=True)
        path().parent.symlink_to(outside, target_is_directory=True)
    else:
        outside.write_text('keep')
        path().parent.mkdir(parents=True)
        path().symlink_to(outside)
    assert not watch.arm('%3', now=T0)
    stop(world)
    due(world)
    assert outside.is_dir() if directory else outside.read_text() == 'keep'
    assert path().parent.is_symlink() if directory else path().is_symlink()


def test_watch_foreign_owner_ignored(world, monkeypatch):
    arm(world)
    monkeypatch.setattr(watch.os, 'getuid', lambda: -1)
    assert not watch.arm('%3', now=T0)
    stop(world)
    assert due(world) == [] and path().exists()


@pytest.mark.parametrize('raw', ['{', '{}', '[]', '{"pane":"%3"}'])
def test_bad_watch_dropped(world, raw):
    arm(world)
    path().write_text(raw)
    stop(world)
    assert due(world) == [] and not path().exists()


@pytest.mark.parametrize('kind', ['status', 'when_free', 'future'])
def test_unhandled_kind_dropped(world, kind):
    arm(world)
    record = json.loads(path().read_text())
    record['kind'] = kind
    path().write_text(json.dumps(record))
    stop(world)
    assert due(world) == [] and not path().exists()


def test_two_stops_one_line(world):
    arm(world)
    stop(world)
    stop(world)
    assert due(world) == ['Window 3 is done: All fixed.']


def test_stop_without_pane_generic_unchanged(world):
    arm(world)
    world['sessions'][0]['tmux_pane'] = None
    world['away'] = True
    stop(world, turn_s=60)
    world['now'] += 10
    assert due(world) == ['Project finished: All fixed.'] and path().exists()


def test_arm_failure_does_not_fail_type(io, monkeypatch):
    monkeypatch.setenv('HEARDBACK_TURN', 'turn')
    store.state_dir().joinpath('watches').write_text('not a directory')
    result = tmux._type(dict(target='3', text='Do it', enter=True))
    assert 'no watch' in result['say'].lower()


def test_arm_failure_does_not_fail_new(io, monkeypatch):
    monkeypatch.setenv('HEARDBACK_TURN', 'turn')
    monkeypatch.setattr(tmux, '_new_dir', lambda a: store.state_dir())
    monkeypatch.setattr(tmux, '_readiness', lambda p: 'pending')
    monkeypatch.setattr(tmux, 'tmux', lambda *a, **kw: '%90\t0:90.0' if a[0] == 'new-window' else '')
    store.state_dir().joinpath('watches').write_text('not a directory')
    assert 'no watch' in tmux._new(dict(run='claude', prompt='Do it'))['say'].lower()


def test_hourly_gc_prunes_inbox(world, monkeypatch):
    ticks, prunes = [], []
    monkeypatch.setattr(announce.time, 'time', lambda: world['now'])
    monkeypatch.setattr(announce.store, 'gc', lambda: ticks.append(world['now']))
    monkeypatch.setattr(attach, 'prune', lambda: prunes.append(world['now']))
    class Tail:
        def read_new(self):
            return []
    async def sleep(_):
        if len(ticks) == 2:
            raise asyncio.CancelledError
        world['now'] += 1800
    monkeypatch.setattr(announce.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(announce.run(world['ann'], None, Tail()))
    assert ticks == prunes == [T0, T0 + 3600]


def test_foreign_owned_watch_file(world, monkeypatch):
    from types import SimpleNamespace
    arm(world)
    real_stat = os.stat
    def foreign(name, **kw):
        info = real_stat(name, **kw)
        if name == '3.json' and kw.get('dir_fd') is not None:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=-1)
        return info
    monkeypatch.setattr(watch.os, 'stat', foreign)
    assert not watch.arm('%3', now=T0 + 1)
    assert watch.take('%3', store.now_iso(T0 + 2), now=T0 + 2) is None
    watch.expire(T0 + 13 * 3600)
    assert path().exists()


def test_newest_replace_keeps_old_until_commit(world, monkeypatch):
    arm(world)
    old = path().read_bytes()
    replace = os.replace
    def checked(src, dst, **kw):
        assert path().read_bytes() == old
        return replace(src, dst, **kw)
    monkeypatch.setattr(watch.os, 'replace', checked)
    assert watch.arm('%3', now=T0 + 2)
    assert json.loads(path().read_text())['armed_at'] == store.now_iso(T0 + 2)
    assert list(path().parent.iterdir()) == [path()]


def test_two_long_stops_close_together(world):
    arm(world)
    world['away'] = True
    stop(world, turn_s=60)
    stop(world, t=T0 + 2, turn_s=60)
    world['now'] += 10
    assert due(world) == ['Window 3 is done: All fixed.']
    world['now'] += 65
    assert due(world) == []


def test_next_unwatched_turn_still_generic(world):
    arm(world)
    world['away'] = True
    stop(world)
    assert due(world) == ['Window 3 is done: All fixed.']
    world['ann'].feed(dict(session_id='s', hook='UserPromptSubmit', to='working'))
    stop(world, t=T0 + 60, turn_s=60)
    world['now'] += 65
    assert due(world) == ['Project finished: All fixed.']


def test_submit_arms_before_status_poll_with_send_timestamp(io, monkeypatch):
    monkeypatch.setenv('HEARDBACK_TURN', 'turn')
    monkeypatch.setattr(store, 'now_iso', lambda t=None: '2027-01-15T08:00:00+00:00')
    def poll(*a):
        assert json.loads(path().read_text())['armed_at'] == '2027-01-15T08:00:00+00:00'
        return None
    monkeypatch.setattr(tmux, '_session_for_pane', poll)
    tmux._type(dict(target='3', text='Do it', enter=True))


def test_no_arm_on_failed_enter(io, monkeypatch):
    from jv.verb import JvError
    monkeypatch.setenv('HEARDBACK_TURN', 'turn')
    def dispatch(*args, **kw):
        if args[-1] == 'Enter':
            raise JvError(1, 'fake send failure')
        return ''
    monkeypatch.setattr(tmux, 'tmux', dispatch)
    with pytest.raises(JvError):
        tmux._type(dict(target='3', text='Do it', enter=True))
    assert not path().exists()


def test_no_arm_on_dry_run(io, monkeypatch, run_jv_with):
    monkeypatch.setenv('HEARDBACK_TURN', 'turn')
    code, _, _ = run_jv_with(tmux.VERBS, 'tmux', 'type', '3', 'Do it', '--enter', '--dry-run', '--json')
    assert code == 0 and not path().exists() and io['sent'] == []


def test_unhandled_watch_dropped_without_event(world):
    assert watch.arm('%3', kind='status', now=T0, deadline=store.now_iso(T0 + 90))
    assert due(world) == [] and not path().exists()


def test_when_free_session_end_silent(world):
    assert watch.arm('%3', kind='when_free', now=T0)
    stop(world, hook='SessionEnd', to='ended')
    assert due(world) == [] and not path().exists()


def test_muted_busy_watch_does_not_reappear(world):
    arm(world)
    world['busy'] = True
    world['muted'] = True
    stop(world)
    assert due(world) == []  # the shared policy drops muted done events, even while busy
    world['muted'] = False
    world['busy'] = False
    assert due(world) == []


def test_status_watch_without_turn(world, monkeypatch, run_jv_with):
    monkeypatch.delenv('HEARDBACK_TURN')
    world['sessions'][0]['state'] = 'working'
    monkeypatch.setattr(store, 'list_sessions', lambda: world['sessions'])
    code, result, _ = run_jv_with(cli.VERBS, 'status', 'watch', '3', '--json')
    assert code == 0 and 'no watch' in result['say'].lower() and not path().exists()


def test_prune_failure_does_not_silence_announcer(world, monkeypatch, capsys):
    from jv.verb import JvError
    arm(world)
    def fail():
        raise JvError(4, 'unsafe inbox')
    monkeypatch.setattr(attach, 'prune', fail)
    monkeypatch.setattr(store, 'gc', lambda: {})
    class Tail:
        def read_new(self):
            return [dict(session_id='s', hook='Stop', t=store.now_iso(T0 + 1),
                         to='idle', detail='All fixed.')]
    said = []
    async def say(line):
        said.append(line["text"])
    async def sleep(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(announce.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(announce.run(world['ann'], say, Tail()))
    assert said == ['Window 3 is done: All fixed.']
    assert 'unsafe inbox' in capsys.readouterr().err


def test_old_long_stop_is_ignored_while_away(world):
    arm(world)
    world['away'] = True
    stop(world, t=T0 - 1, turn_s=60)
    world['now'] += 10
    assert due(world) == [] and path().exists()
