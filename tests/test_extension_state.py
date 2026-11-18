"""Extension protocol exercised with a fake WebExtension API in Node; no browser."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs Node for the fake extension API")
SOURCE = Path(__file__).resolve().parents[1] / "extension/background.js"


def extension(operations, *, duplicate_active=False, focus_changes=False, missing_window=False,
              refuse_focus=False, moved_tab=False):
    code = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(process.argv[1]), calls = [];
let focusedReads = 0, focusedId = 20;
const tabs = [{id:1,windowId:10,active:true,title:'First',url:'https://one.com/'},
              {id:2,windowId:20,active:true,title:'Second',url:'https://two.com/'}];
if (input.duplicate_active) tabs.push({...tabs[1],id:3});
const browser = {
  windows: {
    getLastFocused: async () => ({id:input.focus_changes && focusedReads++ > 0 ? 10 : focusedId,focused:true}),
    get: async id => {if(input.missing_window) throw Error('window gone'); return {id};},
    update: async (id,args) => {calls.push(['focus',id,args]); if(input.refuse_focus) throw Error('focus refused'); focusedId=id;},
  },
  tabs: {
    query: async q => tabs.filter(t => (!q.active || t.active) && (q.windowId === undefined || q.windowId === t.windowId)),
    get: async id => {const t=tabs.find(t=>t.id===id); if(!t) throw Error('tab gone'); return {...t};},
    update: async (id,args) => {
      calls.push(['activate',id,args]); const t=tabs.find(t=>t.id===id);
      if(args.active) for(const other of tabs) if(other.windowId===t.windowId) other.active=false;
      Object.assign(t,args); if(input.moved_tab) t.windowId=30; return {...t};
    },
    create: async args => {
      calls.push(['create',args]); const t={id:99,windowId:args.windowId || 20,title:'',url:args.url || 'about:blank',active:args.active!==false};
      if(t.active) for(const other of tabs) if(other.windowId===t.windowId) other.active=false;
      tabs.push(t); return {...t};
    },
  },
  search: {search: async args => {calls.push(['search',args]);}},
  runtime: {connectNative: () => ({onMessage:{addListener(){}},onDisconnect:{addListener(){}}})},
};
const context = vm.createContext({browser,console,setTimeout:()=>{}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8') + '\nglobalThis.run = handle;', context);
(async () => {
  const replies = [];
  for (const [op,args] of input.operations) {
    try {replies.push(await context.run(op,args));} catch(e) {replies.push({ok:false,error:String(e)});}
  }
  process.stdout.write(JSON.stringify({replies,calls}));
})();
"""
    args = json.dumps({"operations": operations, "duplicate_active": duplicate_active, "focus_changes": focus_changes,
                       "missing_window": missing_window, "refuse_focus": refuse_focus, "moved_tab": moved_tab})
    result = subprocess.run(["node", "-e", code, args, str(SOURCE)], capture_output=True, text=True, check=True, timeout=5)
    return json.loads(result.stdout)


def test_current_tab_identity_is_per_last_focused_browser_window():
    result = extension([["list", {}], ["activate", {"tabId": 1}]])
    state, activated = result["replies"]
    assert state["current_window"] == 20 and state["current_tab"] == 2 and state["creation_guard"]
    assert state["coverage"]["zen_workspaces"] == "unknown"
    assert len([t for t in state["tabs"] if t["active"]]) == 2
    assert activated["tabId"] == 1 and activated["windowId"] == 10
    assert ["focus", 10, {"focused": True}] in result["calls"]


@pytest.mark.parametrize("opts", [{"duplicate_active": True}, {"focus_changes": True}])
def test_uncertain_current_identity_is_explicit(opts):
    assert extension([["list", {}]], **opts)["replies"][0]["current_tab"] is None


@pytest.mark.parametrize("op,payload", [("create", {"url": "https://new.com/"}), ("search", {"query": "new page"})])
def test_guarded_creation_pins_window_and_rejects_changed_target(op, payload):
    good = extension([[op, {**payload, "windowId": 20, "expectedCurrentTab": 2}]])
    assert good["replies"][0]["windowId"] == 20
    assert next(c for c in good["calls"] if c[0] == "create")[1]["windowId"] == 20
    bad = extension([[op, {**payload, "windowId": 10, "expectedCurrentTab": 1}]])
    assert bad["replies"][0]["ok"] is False and not any(c[0] == "create" for c in bad["calls"])


def test_explicit_search_and_play_creation_remain_intentional():
    result = extension([["create", {"url": "https://youtube.com/watch?v=AAA"}], ["search", {"query": "C++?"}]])
    assert all(r["ok"] and r["windowId"] == 20 for r in result["replies"])


def test_exact_activation_returns_separate_focus_evidence():
    result = extension([["list", {}], ["activate", {"tabId": 1, "windowId": 10}]])
    state, focused = result["replies"]
    assert state["navigation_focus"] == 1
    assert focused["target_validated"] and focused["activation_confirmed"]
    assert focused["browser_focus_requested"] and focused["browser_focus_observed"]
    assert focused["url"] == "https://one.com/" and focused["title"] == "First"
    assert len([c for c in result["calls"] if c[0] == "focus"]) == 1


@pytest.mark.parametrize("args,opts", [({"tabId": 1, "windowId": 20}, {}),
                                      ({"tabId": 99, "windowId": 10}, {}),
                                      ({"tabId": 1, "windowId": 10}, {"missing_window": True}),
                                      ({"tabId": True, "windowId": 10}, {})])
def test_exact_activation_rejects_wrong_or_vanished_identity_before_mutation(args, opts):
    result = extension([["activate", args]], **opts)
    assert not result["replies"][0]["ok"] and result["calls"] == []


def test_activation_refusal_is_partial_and_never_repeated():
    result = extension([["activate", {"tabId": 1, "windowId": 10}]], refuse_focus=True)
    reply = result["replies"][0]
    assert reply["ok"] and reply["activation_confirmed"] and reply["browser_focus_requested"]
    assert not reply["browser_focus_observed"]
    assert len(result["calls"]) == 2


def test_tab_moved_during_activation_cannot_raise_another_window():
    result = extension([["activate", {"tabId": 1, "windowId": 10}]], moved_tab=True)
    assert not result["replies"][0]["target_validated"]
    assert not any(c[0] == "focus" for c in result["calls"])


@pytest.mark.parametrize("op,payload", [("create", {"url": "https://new.com/"}), ("search", {"query": "cats"})])
def test_background_creation_is_inactive_and_never_raises_window(op, payload):
    result = extension([[op, {**payload, "active": False}], ["list", {}]])
    created, state = result["replies"]
    assert created["tabId"] == 99 and state["current_tab"] == 2
    assert next(c for c in result["calls"] if c[0] == "create")[1]["active"] is False
    assert not any(c[0] in ("focus", "activate") for c in result["calls"])


def test_legacy_playback_creation_does_not_gain_a_foreground_request():
    result = extension([["create", {"url": "https://youtube.com/watch?v=AAA"}]])
    assert result["calls"] == [["create", {"url": "https://youtube.com/watch?v=AAA"}]]


@pytest.mark.parametrize("case,ok,via,clicks,plays", [
    ({"label": "Play"}, True, "button", 1, 0),
    ({"label": "Pause"}, True, "button", 0, 0),
    ({"label": "Pause", "action": "pause"}, True, "button", 1, 0),
    ({"label": "Play", "disabled": True}, False, None, 0, 0),
    ({"label": "Loading"}, False, None, 0, 0),
    ({"label": None}, False, None, 0, 0),
    ({"host": "youtube.com"}, True, "element", 0, 1),
    ({"host": "youtube.com", "blocked": True}, False, None, 0, 1),
])
def test_spotify_owns_control_even_with_an_unrelated_media_element(case, ok, via, clicks, plays):
    code = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(process.argv[1]);
let clicks = 0, plays = 0;
const button = input.label === null ? null : {
  disabled:!!input.disabled, getAttribute:()=>input.label || 'Play', click(){clicks++;},
};
const media = {readyState:4,networkState:1,paused:true,error:null,
  pause(){this.paused=true;},
  play(){
    plays++;
    if (input.blocked) return Promise.reject({name:'NotAllowedError'});
    this.paused=false;
    return Promise.resolve();
  },
};
const context = vm.createContext({location:{hostname:input.host || 'open.spotify.com'},
  document:{querySelectorAll:()=>[media],querySelector:()=>button}});
const fn = fs.readFileSync(process.argv[2],'utf8').match(/function videoCtl\(action\) \{[\s\S]*?\n\}/)[0];
vm.runInContext(fn, context);
(async()=>{
  const reply = await context.videoCtl(input.action || 'play');
  process.stdout.write(JSON.stringify({reply,clicks,plays}));
})();
"""
    result = subprocess.run(["node", "-e", code, json.dumps(case), str(SOURCE)], capture_output=True,
                            text=True, check=True, timeout=5)
    data = json.loads(result.stdout)
    assert data["reply"]["ok"] is ok and data["reply"].get("via") == via
    assert (data["clicks"], data["plays"]) == (clicks, plays)
    if via == "button":
        assert data["reply"]["clicked"] is bool(clicks)
        assert data["reply"]["before_label"] == data["reply"]["after_label"] == case["label"]
        assert data["reply"]["paused"] is (case["label"] == "Play")
    if not ok:
        assert data["reply"]["diag"] and data["reply"]["reason"]


@pytest.mark.parametrize("case,inserted,submission,clicks", [
    ("draft", False, "not_attempted", 0),
    ("insert_failed", False, "not_attempted", 0),
    ("disabled", True, "not_attempted", 0),
    ("click_only", True, "unverified", 1),
    ("observed", True, "observed", 1),
    ("changed_chat", True, "unverified", 1),
    ("changed_origin", True, "unverified", 1),
    ("no_send", True, "not_attempted", 0),
    ("wrong_host", False, "not_attempted", 0),
])
def test_claude_composer_requires_insertion_and_submission_evidence(case, inserted, submission, clicks):
    code = r"""
const fs = require('fs'), vm = require('vm');
const mode = process.argv[1];
let clicked = 0, time = 0;
const box = {innerText:mode === 'draft' ? 'existing draft' : '', isConnected:true, focus(){}};
const button = {disabled:mode === 'disabled', getAttribute:()=>null,
  click(){
    clicked++;
    if (mode === 'observed' || mode.startsWith('changed_')) box.innerText = '';
    if (mode === 'changed_chat') location.href = 'https://claude.ai/chat/two';
    if (mode === 'changed_origin') {
      location.protocol = 'http:'; location.hostname = 'example.com'; location.href = 'http://example.com/';
    }
  }};
const document = {
  querySelector(sel) {
    if (sel.includes('contenteditable')) return box;
    if (sel.includes('Stop')) return clicked && (mode === 'observed' || mode.startsWith('changed_')) ? {} : null;
    if (sel.includes('Send')) return button;
    return null;
  },
  execCommand(op, ui, text) {if (mode === 'insert_failed') return false; box.innerText = text; return true;},
};
const location = {protocol:'https:',hostname:mode === 'wrong_host' ? 'claude.ai.evil.example' : 'claude.ai',
                  href:'https://claude.ai/chat/one'};
const browser = {runtime:{connectNative:()=>({onMessage:{addListener(){}},onDisconnect:{addListener(){}}})}};
const context = vm.createContext({browser,document,location,console,
  Date:{now:()=>time+=100},setTimeout:fn=>fn()});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8') +
  '\nglobalThis.ask = () => typeInPage("hello", CLAUDE_SELECTORS, ' + (mode !== 'no_send') +
  ', "https://claude.ai/chat/one");', context);
(async()=>process.stdout.write(JSON.stringify({result:await context.ask(),clicked,draft:box.innerText})))();
"""
    run = subprocess.run(["node", "-e", code, case, str(SOURCE)], capture_output=True, text=True, check=True, timeout=5)
    out = json.loads(run.stdout)
    assert out["result"]["inserted"] is inserted and out["result"]["submission"] == submission
    assert out["clicked"] == clicks
    if case == "draft":
        assert out["draft"] == "existing draft"
    if case.startswith("changed_"):
        assert out["result"]["reason"] == "Claude.ai target changed after send"
