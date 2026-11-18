"""New-session attachments with fake desktop I/O and isolated private inboxes."""
import json
import os
from pathlib import Path
import shlex
import stat
import time

import pytest

from jv import attach, ctx, tmux
from jv.verb import JvError
from test_attach import io, pane, send
from test_tmux import desktop_config  # noqa: F401


@pytest.fixture
def new_io(io, monkeypatch):
    (Path.home() / 'claude').mkdir()
    (Path.home() / 'work/projects/webapp').mkdir(parents=True)
    monkeypatch.setattr(tmux, '_readiness', lambda p: 'pending')
    def dispatch(*args, **kw):
        if args[0] == 'capture-pane':
            return io['prompt']
        io['sent'].append((args, kw))
        return '%90\t0:90.0' if args[0] == 'new-window' else ''
    monkeypatch.setattr(tmux, 'tmux', dispatch)
    return io


def inbox():
    return Path(os.environ['XDG_STATE_HOME']) / 'heardback/inbox'


def start(run_jv_with, kind='clipboard', prompt='explain', flags=(), run='claude'):
    args = ['tmux', 'new', '--json']
    if run is not None:
        args += ['--run', run]
    if kind is not None:
        args += ['--attach', kind]
    if prompt is not None:
        args += ['--prompt=' + prompt]
    return run_jv_with(tmux.VERBS, *args, *flags)


def argv(data):
    command = next(a[-1] for a, _ in data['sent'] if a[0] == 'new-window')
    return shlex.split(command.split('; exec ')[0])


@pytest.mark.parametrize('run', [None, 'shell', 'nvim'])
def test_new_attach_needs_claude(new_io, run_jv_with, run):
    assert start(run_jv_with, run=run)[0] == 2
    assert not new_io['sent'] and not inbox().exists()


def test_new_attach_tab_prompt(new_io, run_jv_with):
    assert start(run_jv_with, 'tab')[0] == 0
    assert argv(new_io) == ['claude', '--', 'explain (from heardback: Zen tab "Private title" https://example.com/private)']
    assert not inbox().exists()


@pytest.mark.parametrize('kind', ['tab', 'clipboard'])
def test_attach_to_ready_fresh_session_needs_no_readback(new_io, monkeypatch, run_jv_with, kind):
    assert start(run_jv_with, kind=None, prompt=None)[0] == 0
    new_io['panes'].append(pane(90, cmd='claude'))
    sessions = [dict(tmux_pane='%90', state='idle', session_id='fresh')]
    monkeypatch.setattr(tmux, 'load_sessions', lambda include_ended=False: sessions)
    monkeypatch.setattr(tmux, '_session_for_pane', lambda *a, **kw: sessions[0])
    assert tmux.prompt_draft('%90') is False
    code, out, _ = send(run_jv_with, kind, target='90')
    assert code == 0 and 'confirm' not in out
    assert new_io['sent'][-1] == (('send-keys', '-t', '%90', 'Enter'), {})


def test_new_attach_clipboard_file(new_io, monkeypatch, run_jv_with):
    monkeypatch.setenv('HEARDBACK_TURN', '20261108-123456-1')
    assert start(run_jv_with)[0] == 0
    files = list(inbox().iterdir())
    assert len(files) == 1
    path = files[0]
    assert path.read_text() == new_io['clipboard']
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(inbox().stat().st_mode) == 0o700
    assert path.name.endswith('-20261108-123456-1-clipboard.txt')
    assert argv(new_io) == ['claude', '--add-dir', str(inbox()), '--',
        f'explain (from heardback: your clipboard, 28 characters, saved at {path}; read it first)']


def test_new_attach_dry_run_writes_nothing(new_io, run_jv_with):
    code, out, _ = start(run_jv_with, flags=('--dry-run',))
    assert code == 0 and out['source_revision'] == ctx.revision('clipboard', new_io['clipboard'])
    assert not inbox().exists() and not new_io['sent']


def test_new_attach_prune(new_io, run_jv_with):
    inbox().mkdir(parents=True)
    old, fresh, directory = [inbox() / name for name in ['old.txt', 'fresh.txt', 'directory']]
    old.write_text('old')
    fresh.write_text('fresh')
    directory.mkdir()
    past = time.time() - 8 * 86400
    os.utime(old, (past, past))
    os.utime(directory, (past, past))
    assert start(run_jv_with)[0] == 0
    assert not old.exists() and fresh.read_text() == 'fresh' and directory.is_dir()
    # The exported prune also works independently, for the later announcer task.
    os.utime(fresh, (past, past))
    attach.prune()
    assert not fresh.exists() and directory.is_dir()


def test_new_attach_exclusive(new_io, monkeypatch, run_jv_with):
    now = time.time()
    monkeypatch.setattr(attach.time, 'time', lambda: now)
    monkeypatch.setenv('HEARDBACK_TURN', 'same-turn')
    assert start(run_jv_with)[0] == 0
    first = next(inbox().iterdir())
    new_io['clipboard'] = 'second attachment'
    assert start(run_jv_with)[0] == 0
    assert len(list(inbox().iterdir())) == 2 and first.read_text() == 'secret clipboard\nsecond line'
    assert {p.read_text() for p in inbox().iterdir()} == {'secret clipboard\nsecond line', 'second attachment'}


def test_new_attach_other_dir(new_io, run_jv_with):
    assert start(run_jv_with, flags=('--dir', 'web app'))[0] == 0
    command = next(a for a, _ in new_io['sent'] if a[0] == 'new-window')
    assert command[command.index('-c') + 1] == str(Path.home() / 'work/projects/webapp')
    assert argv(new_io)[:4] == ['claude', '--add-dir', str(inbox()), '--']


def test_new_readback_tainted(new_io, monkeypatch, run_jv_with):
    monkeypatch.setenv(tmux.TAINT_ENV, '1')
    new_io['clipboard'] = 'x' * 412
    code, out, _ = start(run_jv_with)
    assert code == 3 and out['confirm'] == "Open Claude in your claude folder and send 'explain' with your clipboard, 412 characters?"
    assert not new_io['sent'] and not inbox().exists()
    assert start(run_jv_with, flags=('--yes',))[0] == 3
    assert start(run_jv_with, flags=('--yes', '--source-revision', out['source_revision']))[0] == 0


def test_new_readback_outside(new_io, run_jv_with):
    (Path.home() / 'other').mkdir()
    code, out, _ = start(run_jv_with, flags=('--dir', '~/other'))
    assert code == 3 and out['confirm'] == "Open Claude in other and send 'explain' with your clipboard, 28 characters? It's outside your work folders."
    assert not new_io['sent'] and not inbox().exists()


def test_new_no_prompt_no_add_dir(new_io, run_jv_with):
    assert start(run_jv_with, kind=None, prompt=None)[0] == 0
    assert argv(new_io) == ['claude'] and not inbox().exists()


@pytest.mark.parametrize('prompt', ['!ls', '  !ls', '/clear', ' /clear'])
def test_new_attach_mode_switch_refused(new_io, run_jv_with, prompt):
    assert start(run_jv_with, prompt=prompt)[0] == 4
    assert not new_io['sent'] and not inbox().exists()


def test_new_attach_existing_dir_private(new_io, run_jv_with):
    inbox().mkdir(parents=True, mode=0o755)
    assert start(run_jv_with)[0] == 0
    assert stat.S_IMODE(inbox().stat().st_mode) == 0o700


def test_new_attach_symlink_refused(new_io, tmp_path, run_jv_with):
    outside = tmp_path / 'outside'
    outside.mkdir()
    inbox().parent.mkdir(parents=True)
    inbox().symlink_to(outside, target_is_directory=True)
    assert start(run_jv_with)[0] == 4
    assert not list(outside.iterdir()) and not new_io['sent']


def test_new_attach_foreign_owner_refused(new_io, monkeypatch, run_jv_with):
    inbox().mkdir(parents=True)
    monkeypatch.setattr(attach.os, 'getuid', lambda: -1)
    assert start(run_jv_with)[0] == 4
    assert not list(inbox().iterdir()) and not new_io['sent']


@pytest.mark.parametrize('kind', [None, 'tab', 'clipboard'])
def test_new_dash_instruction_after_separator(new_io, run_jv_with, kind):
    assert start(run_jv_with, kind=kind, prompt='--dangerously-skip-permissions')[0] == 0
    command = argv(new_io)
    assert command[-2] == '--' and command[-1].startswith('--dangerously-skip-permissions')
    assert len(command[command.index('--') + 1:]) == 1


def test_new_attach_pane_file(new_io, run_jv_with):
    assert start(run_jv_with, 'pane', flags=('--attach-target', '12'))[0] == 0
    assert next(inbox().iterdir()).read_text() == new_io['capture']
    assert 'the last 200 lines of window 12' in argv(new_io)[-1]
    assert new_io['captures'] == [('%12', 200), ('%12', 200)]


def test_new_attach_screenshot_png_private_and_add_dir(new_io, run_jv_with, monkeypatch):
    shots = Path(os.environ['XDG_STATE_HOME']) / 'heardback' / 'shots'
    shots.mkdir(parents=True)
    image = shots / '20261116-test.png'
    image.write_bytes(b'fake png')
    monkeypatch.setenv('HEARDBACK_TURN', 'shot-turn')
    code, result, _ = start(run_jv_with, 'screenshot')
    assert code == 0 and result['ok'] is True
    files = list(inbox().glob('*.png'))
    assert len(files) == 1 and files[0].read_bytes() == b'fake png'
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(inbox().stat().st_mode) == 0o700
    command = argv(new_io)
    assert command[:4] == ['claude', '--add-dir', str(inbox()), '--']
    assert f'saved at {files[0]}; read it first' in command[-1]
    assert str(image) not in json.dumps(result)


def test_new_attach_screenshot_dry_run_has_no_path_or_copy(new_io, run_jv_with):
    shots = Path(os.environ['XDG_STATE_HOME']) / 'heardback' / 'shots'
    shots.mkdir(parents=True)
    (shots / 'latest.png').write_bytes(b'fake png')
    code, result, _ = start(run_jv_with, 'screenshot', flags=('--dry-run',))
    assert code == 0 and 'source_revision' in result
    assert str(shots) not in json.dumps(result)
    assert not inbox().exists() and not new_io['sent']


def test_new_attach_pane_needs_target(new_io, run_jv_with):
    assert start(run_jv_with, 'pane')[0] == 2
    assert not new_io['sent'] and not inbox().exists()


@pytest.mark.parametrize('kind', [None, 'tab', 'clipboard'])
def test_new_attach_target_only_pane(new_io, run_jv_with, kind):
    assert start(run_jv_with, kind, flags=('--attach-target', '12'))[0] == 2
    assert not new_io['sent'] and not inbox().exists()


@pytest.mark.parametrize('kind', ['tab', 'clipboard', 'selection', 'pane'])
def test_new_attach_private_results(new_io, run_jv_with, kind):
    flags = ('--attach-target', '12') if kind == 'pane' else ()
    for dry in [('--dry-run',), ()]:
        code, out, _ = start(run_jv_with, kind, flags=(*flags, *dry))
        assert code == 0
        result = json.dumps(out)
        for secret in [new_io['tab']['title'], new_io['tab']['url'], new_io['clipboard'], new_io['selection'], new_io['capture']]:
            assert json.dumps(secret)[1:-1] not in result
        assert str(inbox()) not in result


def test_new_prune_no_follow(new_io, tmp_path):
    inbox().mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.write_text('keep')
    past = time.time() - 8 * 86400
    os.utime(outside, (past, past))
    (inbox() / 'link').symlink_to(outside)
    attach.prune()
    assert outside.read_text() == 'keep' and (inbox() / 'link').is_symlink()


def test_new_prune_inbox_symlink_refused(new_io, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    old = outside / 'old.txt'
    old.write_text('keep')
    past = time.time() - 8 * 86400
    os.utime(old, (past, past))
    inbox().parent.mkdir(parents=True)
    inbox().symlink_to(outside, target_is_directory=True)
    with pytest.raises(JvError):
        attach.prune()
    assert old.read_text() == 'keep'


@pytest.mark.parametrize('kind', ['tab', 'clipboard', 'selection', 'pane'])
def test_new_attach_changed(new_io, run_jv_with, kind):
    flags = ('--attach-target', '12') if kind == 'pane' else ()
    _, out, _ = start(run_jv_with, kind, flags=(*flags, '--dry-run'))
    if kind == 'tab':
        new_io['tab']['tabId'] += 1
    elif kind == 'pane':
        new_io['panes'][1]['id'] = '%99'
    else:
        new_io[kind] = 'changed'
    code, out, _ = start(run_jv_with, kind, flags=(*flags, '--source-revision', out['source_revision']))
    assert code == 4 and 'changed after read-back' in out['error']
    assert not new_io['sent'] and not inbox().exists()


@pytest.mark.parametrize('prompt', [None, ''])
def test_new_attach_default_prompt(new_io, run_jv_with, prompt):
    assert start(run_jv_with, prompt=prompt)[0] == 0
    assert argv(new_io)[-1].startswith('Look at this. (from heardback:')


@pytest.mark.parametrize('kind', ['tab', 'clipboard', 'selection', 'pane'])
def test_new_attach_binds_preflight_to_dispatch(new_io, monkeypatch, run_jv_with, kind):
    if kind == 'tab':
        reads = iter([new_io['tab'].copy(), {**new_io['tab'], 'tabId': 99}])
        monkeypatch.setattr(ctx, '_tab', lambda: next(reads))
    elif kind == 'pane':
        def screen(pid, lines=15):
            new_io['panes'][1] = {**new_io['panes'][1], 'id': '%99'}
            return new_io['capture']
        monkeypatch.setattr(tmux, 'screen', screen)
    else:
        reads = iter(['first', 'changed'])
        monkeypatch.setattr(ctx, 'paste', lambda primary=False: next(reads))
    flags = ('--attach-target', '12') if kind == 'pane' else ()
    code, out, _ = start(run_jv_with, kind, flags=flags)
    assert code == 4 and 'changed after read-back' in out['error']
    assert not new_io['sent'] and not inbox().exists()


def test_new_attach_live_pane_content_allowed(new_io, run_jv_with):
    flags = ('--attach-target', '12')
    _, out, _ = start(run_jv_with, 'pane', flags=(*flags, '--dry-run'))
    new_io['capture'] = 'new output'
    assert start(run_jv_with, 'pane', flags=(*flags, '--source-revision', out['source_revision']))[0] == 0
    assert next(inbox().iterdir()).read_text() == 'new output'


def test_new_attach_tab_title_change_allowed(new_io, run_jv_with):
    _, out, _ = start(run_jv_with, 'tab', flags=('--dry-run',))
    new_io['tab']['title'] = 'new title'
    assert start(run_jv_with, 'tab', flags=('--source-revision', out['source_revision']))[0] == 0
    assert '"new title"' in argv(new_io)[-1]
    assert not inbox().exists()


def test_new_attach_exclusive_flags_and_private_before_write(new_io, monkeypatch, run_jv_with):
    inbox().mkdir(parents=True, mode=0o755)
    real_open = os.open
    creates = []
    def checked_open(path, flags, mode=0o777, **kwargs):
        if flags & os.O_CREAT:
            assert stat.S_IMODE(inbox().stat().st_mode) == 0o700
            assert flags & os.O_EXCL and mode == 0o600
            creates.append(path)
        return real_open(path, flags, mode, **kwargs)
    monkeypatch.setattr(attach.os, 'open', checked_open)
    assert start(run_jv_with)[0] == 0
    assert len(creates) == 1


def test_new_attach_write_failure_cleans_file(new_io, monkeypatch, run_jv_with):
    real_chmod = os.fchmod
    def fail_file(fd, mode):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('fake disk error')
        real_chmod(fd, mode)
    monkeypatch.setattr(attach.os, 'fchmod', fail_file)
    code, out, _ = start(run_jv_with)
    assert code == 4 and out['error'] == 'could not safely write the attachment inbox'
    assert not list(inbox().iterdir()) and not new_io['sent']


def test_new_prune_missing_creates_nothing(new_io):
    attach.prune()
    assert not inbox().exists()


def test_new_attach_turn_cannot_escape_inbox(new_io, monkeypatch, run_jv_with):
    monkeypatch.setenv('HEARDBACK_TURN', '../../outside')
    assert start(run_jv_with)[0] == 0
    assert len(list(inbox().iterdir())) == 1
    assert next(inbox().iterdir()).parent == inbox()


def test_new_attach_readback_cap(new_io, monkeypatch, run_jv_with):
    monkeypatch.setenv(tmux.TAINT_ENV, '1')
    code, out, _ = start(run_jv_with, prompt='x' * 200)
    assert code == 4 and 'characters, the most is 200' in out['error']
    assert not new_io['sent'] and not inbox().exists()


def test_new_attach_schema(new_io, run_jv_with):
    code, cat, _ = run_jv_with(tmux.VERBS, 'list', '--json')
    assert code == 0
    verb = next(v for v in cat['verbs'] if v['name'] == 'tmux.new')
    assert verb['params']['properties']['attach']['enum'] == ['tab', 'clipboard', 'selection', 'pane', 'last_heard', 'screenshot']
    assert all(flag in verb['argv'] for flag in ['--attach', '--attach-target', '--source-revision'])


def test_new_attach_launch_failure_removes_secret(new_io, monkeypatch, run_jv_with):
    inbox().mkdir(parents=True)
    keep = inbox() / 'existing.txt'
    keep.write_text('keep')
    def fail_launch(*args, **kw):
        assert args[0] == 'new-window'
        files = [p for p in inbox().iterdir() if p != keep]
        assert len(files) == 1 and files[0].read_text() == new_io['clipboard']
        raise JvError(1, 'fake launch failure')
    monkeypatch.setattr(tmux, 'tmux', fail_launch)
    code, out, _ = start(run_jv_with)
    assert code == 1 and out['error'] == 'fake launch failure'
    assert list(inbox().iterdir()) == [keep] and keep.read_text() == 'keep'
    assert new_io['clipboard'] not in json.dumps(out)
