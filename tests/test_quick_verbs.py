"""Quick-win verb checks; no real tmux, Hyprland or Zen calls."""
import pytest

from heardback.brain import catalogue, router
from jv import cli, hypr, tmux, zen
from jv.verb import JvError, READBACK_MAX, _readback, validate


@pytest.fixture
def clients(monkeypatch):
    cs = [dict(address='0xaaa', pid=1, **{'class': 'kitty'}, title='~', focusHistoryID=0),
          dict(address='0xbbb', pid=2, **{'class': 'zen'}, title='page', focusHistoryID=1)]
    monkeypatch.setattr(hypr, 'clients', lambda: cs)
    monkeypatch.setattr(hypr, '_active', lambda: '0xaaa')
    monkeypatch.setattr(hypr, '_run', lambda *a: pytest.fail('real hyprctl is forbidden'))
    return cs


def test_readback_refusal_numbers():
    with pytest.raises(JvError) as e:
        _readback('x' * 201)
    assert e.value.msg == "heardback didn't send it: too long to read back (201 characters, the most is 200)"
    assert e.value.extra['say'] == e.value.msg


def test_149_text_longest_window_frame():
    # tmux indices are signed 32-bit integers; this is the largest non-negative one.
    assert len(tmux._submit_check('x' * 149, '2147483647', {})) <= READBACK_MAX


@pytest.mark.parametrize('alias', ['this', 'current', 'active', 'focused'])
def test_close_current_tab_readback(monkeypatch, alias):
    ts = [dict(id=1, pinned=False, current=False, title='engine tab', url='engine'),
          dict(id=2, pinned=False, current=True, title='current page', url='page')]
    monkeypatch.setattr(zen, 'tabs', lambda: ts)
    assert zen.close_matches({'query': alias}) == ([ts[1]], True)
    assert zen._close_check({'query': alias}) == "Close 'current page'?"


def test_close_engine_still_fuzzy(monkeypatch):
    t = dict(id=1, pinned=False, current=False, title='engine', url='page')
    monkeypatch.setattr(zen, 'tabs', lambda: [t])
    assert zen.close_matches({'query': 'engne'}) == ([t], True)


@pytest.mark.parametrize('query', ['kitty', 'kit', 'KIT'])
def test_class_exact_prefix(clients, query):
    assert hypr._client(query)['address'] == '0xaaa'


def test_unknown_client_closest(clients):
    with pytest.raises(JvError) as e:
        hypr._client('no-such-thing')
    assert e.value.extra['closest']


def test_focus_window_routes_to_tmux(monkeypatch):
    monkeypatch.setattr(tmux, 'panes', lambda: [{'window_index': '2', 'window_name': 'work'}])
    cat = catalogue._index(cli.catalogue(tmux.VERBS + hypr.VERBS + zen.VERBS))
    r = router.Router(cat)
    assert r.route('focus window 2') == router.Route('tmux.select', {'window': '2'}, 1.0)
    assert r.route('focus kit') == router.Route('hypr.focus', {'query': 'kit'}, 1.0)


def test_nvim_fixed_argv():
    v = next(v for v in tmux.VERBS if v.name == 'tmux.new')
    assert validate(v, {'run': 'nvim'}) == {'run': 'nvim'}
    assert tmux._run_cmd({'run': 'nvim'}) == ['nvim; exec "${SHELL:-/bin/sh}"']
    with pytest.raises(JvError):
        validate(v, {'run': 'nvim; touch bad'})
    with pytest.raises(JvError, match='needs --run claude'):
        tmux._run_cmd({'run': 'nvim', 'prompt': 'anything'})


@pytest.mark.parametrize('phrase', ['close kitty', 'close the app', 'close this app', 'close the focused window'])
def test_hypr_close_routing(phrase):
    cat = catalogue._index(cli.catalogue(hypr.VERBS))
    r = router.Router(cat).route(phrase)
    assert r and r.verb == 'hypr.close'
    assert r.args == {'query': 'kitty' if phrase == 'close kitty' else 'active'}


def test_hypr_close_requires_yes_and_stable_client(clients, monkeypatch, run_jv_with):
    sent = []
    monkeypatch.setattr(hypr, 'dispatch', sent.append)
    code, out, _ = run_jv_with(hypr.VERBS, 'hypr', 'close', '--json')
    assert code == 3 and out['confirm'] == "Close kitty, titled '~'?" and not sent
    revision = out['source_revision']
    monkeypatch.setattr(hypr, '_active', lambda: '0xbbb')
    code, out, _ = run_jv_with(hypr.VERBS, 'hypr', 'close', '--yes', '--json', '--source-revision', revision)
    assert code == 0 and out['address'] == '0xaaa'
    assert sent == ['hl.dsp.window.close({window = "address:0xaaa"})']


def test_hypr_close_yes_without_revision_reasks(clients, monkeypatch, run_jv_with):
    monkeypatch.setattr(hypr, 'dispatch', lambda *a: pytest.fail('no approval identity'))
    assert run_jv_with(hypr.VERBS, 'hypr', 'close', '--yes', '--json')[0] == 3


def test_hypr_close_changed_identity_refuses(clients, monkeypatch, run_jv_with):
    monkeypatch.setattr(hypr, 'dispatch', lambda *a: pytest.fail('changed identity'))
    _, out, _ = run_jv_with(hypr.VERBS, 'hypr', 'close', '--json')
    clients[0]['title'] = 'different'
    code, _, _ = run_jv_with(hypr.VERBS, 'hypr', 'close', '--yes', '--json', '--source-revision', out['source_revision'])
    assert code == 1


def test_close_this_yes_after_tab_switch_refuses(monkeypatch):
    # Live 16:59: read-back named 'plan', the user switched tabs, the yes closed 'heardback'.
    ts = [dict(id=1, pinned=False, current=True, title='plan', url='a'),
          dict(id=2, pinned=False, current=False, title='heardback', url='b')]
    monkeypatch.setattr(zen, 'tabs', lambda: ts)
    asked = {'query': 'this zen'}
    assert zen._close_check(asked) == "Close 'plan'?"
    ts[0]['current'], ts[1]['current'] = False, True
    with pytest.raises(JvError) as e:
        zen._close_check({'query': 'this', 'source_revision': asked['_source_revision']})
    assert e.value.extra['say'] == "The tabs changed; please ask again."
    ts[0]['current'], ts[1]['current'] = True, False
    assert zen._close_check({'query': 'this', 'source_revision': asked['_source_revision']}) == "Close 'plan'?"


def test_close_this_site_tab(monkeypatch):
    # Live 20:16: "close this YouTube tab" -> query "this youtube" said "no match".
    ts = [dict(id=1, pinned=False, current=True, title='Best of lofi - YouTube', url='https://www.youtube.com/watch?v=x'),
          dict(id=2, pinned=False, current=False, title='heardback Build Plan', url='https://claude.ai/a')]
    monkeypatch.setattr(zen, 'tabs', lambda: ts)
    assert zen.close_matches({'query': 'this youtube'}) == ([ts[0]], True)
    assert zen._close_check({'query': 'this youtube'}) == "Close 'Best of lofi'?"
    ts[0]['current'], ts[1]['current'] = False, True   # current tab isn't YouTube: match YouTube tabs as usual
    assert zen.close_matches({'query': 'this youtube'}) == ([ts[0]], False)


def test_close_readback_and_say_are_short(monkeypatch):
    # Live 20:24: the read-back and "closed ..." spoke the whole YouTube title.
    t = dict(id=1, pinned=False, current=True, url='https://www.youtube.com/watch?v=x',
             title='Best of lofi hip hop 2021 ✨ [beats to relax/study to] - YouTube')
    monkeypatch.setattr(zen, 'tabs', lambda: [t])
    monkeypatch.setattr(zen, '_remove', lambda ts: len(ts))
    assert zen._close_check({'query': 'this youtube'}) == "Close 'Best of lofi hip hop 2021'?"
    assert zen._close({'query': 'this youtube'})['say'] == "closed Best of lofi hip hop 2021"
