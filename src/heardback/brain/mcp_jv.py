"""stdio MCP transport for jv; acting calls carry the brain's single-use grant."""
import json
import os
import queue
import subprocess
import sys
import threading

from heardback.brain import catalogue, play_result
from heardback.brain.catalogue import build_argv, tool_name
from jv import grants, trace
from jv.verb import observation_result

TIMEOUT_S = float(os.environ.get("HEARDBACK_JV_TIMEOUT_S") or catalogue.JV_TIMEOUT_S)   # env: tests only
ASK_USER = {
    "name": "ask_user",
    "description": "Ask the user one short question and end your turn. Their spoken answer is your next message. Use it for every question; never end a reply with a question.",
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["question"],
                    "properties": {"question": {"type": "string", "maxLength": 200},
                                   "options": {"type": "array", "maxItems": 5,
                                               "items": {"type": "string", "maxLength": 40}}}}}


# The 12 verbs Claude called most in the replay set (2026-11-17); fixed, so a new verb never changes the tool list.
DIRECT = ("tmux_type", "tmux_new", "zen_open", "media_media", "tmux_close", "zen_play", "hypr_open", "tmux_list",
          "ctx_ctx", "tmux_select", "tmux_read", "status_list")
JV_FIND = {
    "name": catalogue.FIND,
    "description": "Find jv verbs for a request (volume, brightness, wallpaper, bluetooth, settings, system info, reminders, "
                   "notes, memory, web search, video, tabs, windows, sessions, screenshots...). Returns each match's verb, "
                   "rules and args; then call jv_run. Read-only.",
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["query"],
                    "properties": {"query": {"type": "string", "maxLength": 100}}}}
JV_RUN = {
    "name": catalogue.RUN,
    "description": "Run one jv verb found with jv_find: verb is its name (e.g. volume_volume), args its arguments. "
                   "Follow the verb's rules; the same read-backs and refusals apply as for a direct tool.",
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["verb"],
                    "properties": {"verb": {"type": "string", "maxLength": 60}, "args": {"type": "object"}}}}


def _by_tool(cat: dict) -> dict[str, dict]:
    return {tool_name(e): e for e in cat.values() if e.get("claude", True)}


def _schema(entry: dict) -> dict:
    return {"name": tool_name(entry), "description": entry["description"], "inputSchema": entry["params"]}


def tools(cat: dict) -> list[dict]:
    by = _by_tool(cat)
    return [_schema(by[n]) for n in DIRECT if n in by] + [JV_FIND, JV_RUN, ASK_USER]


def find(cat: dict, query) -> dict:
    """Catalogue entries whose name or description shares the most words with the query (our own text only)."""
    words = {w for w in str(query).lower().replace("_", " ").replace(".", " ").split() if len(w) > 2}
    by = _by_tool(cat)
    score = {n: sum(w in f"{n} {e['description']}".lower() for w in words) for n, e in by.items()}
    hits = sorted((n for n in by if score[n]), key=lambda n: -score[n])[:6]
    return {"verbs": [{"verb": n, "description": by[n]["description"], "args": by[n]["params"]} for n in hits],
            "all": sorted(by)}


def call(cat: dict, name: str, args: dict, *, expected_turn: str | None = None, expected_attempt: str | None = None) -> dict:
    if os.environ.get("HEARDBACK_REPLAY") == "1":   # scripts/replay.py check --claude: record, run nothing
        with open(os.environ["HEARDBACK_REPLAY_LOG"], "a") as f:
            f.write(json.dumps({"name": name, "args": args}) + "\n")
        if name != catalogue.FIND:   # jv_find answers for real: it reads only `cat`, spawns nothing
            return {"content": [{"type": "text", "text": '{"ok":true,"say":"done"}'}], "isError": False}
    if name == "ask_user":
        return {"content": [{"type": "text", "text": '{"ok":true}'}], "isError": False}
    if name == catalogue.FIND:
        return {"content": [{"type": "text", "text": json.dumps(find(cat, (args or {}).get("query", "")))}], "isError": False}
    entry, args = catalogue.resolve(cat, name, args)   # never parse the name; jv_run: the inner verb and args only
    if entry is None:
        text, rc = ("error: unknown verb or args; call jv_find" if name == catalogue.RUN else "error: unknown tool"), 1
    else:
        argv = build_argv(entry, args, "--json")
        tid = expected_turn or ""
        try:
            # Queued calls are checked again here before launch.
            # A launch admitted before revocation may finish; revocation never promises cancellation.
            with catalogue.execution_lock():
                tid = catalogue.current_turn() if expected_turn is None else expected_turn
                if os.environ.get("HEARDBACK_EXECUTION_GATE") == "1" and (
                        expected_turn is None or not catalogue.execution_allowed(tid, expected_attempt or "")):
                    return {"content": [{"type": "text", "text": "heardback refused: this request has ended; no action was dispatched."}],
                            "isError": True}
                env = {**os.environ, "HEARDBACK_TURN": tid,
                       "HEARDBACK_GRANT": grants.find(entry["name"], args, tid, expected_attempt or "")
                       if entry.get("effect") != "read" else ""}
                p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     stdin=subprocess.DEVNULL, env=env)
            try:
                stdout, stderr = p.communicate(timeout=catalogue.jv_timeout(argv, TIMEOUT_S))
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
                raise
            rc, text = p.returncode, stdout
            if rc:
                try:
                    json.loads(stdout)   # exit 2 candidates / error JSON: Claude reads it as is
                except ValueError:
                    text = f"error: {stderr.strip() or f'exit {rc}'}"
        except subprocess.TimeoutExpired:
            text, rc = "error: timed out", 1
        except OSError:
            text, rc = "error: action execution unavailable", 1
    if entry and entry.get("observes"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            text = json.dumps(observation_result(data, True))
    if entry and entry["name"] in {"zen.play", "media"}:
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        ok = rc == 0 and isinstance(data, dict) and data.get("ok") is True
        result = {"ok": ok, "say": ("opened the video" if ok else "couldn't start the video")
                  if entry["name"] == "zen.play" else
                  ("completed the media action" if ok else "couldn't verify the media action")}
        if entry.get("observes"):
            if isinstance(data, dict) and "observed" in data:
                result["observed"] = data["observed"] is True
            result = observation_result(result, True)
        if entry["name"] == "media" and isinstance(data, dict) and data.get("clarification") in ("music_source", "media_player"):
            result.update({k: data[k] for k in ("clarification", "candidates", "player_labels", "source", "action", "target", "intent")
                           if k in data})
        if isinstance(data, dict) and isinstance(data.get("say"), str) and data["say"].strip():
            try:
                result["receipt"] = play_result.save(data["say"], tid, expected_attempt or "")
            except OSError as e:
                trace.error("mcp", tid, "save play speech", e)
        text, rc = json.dumps(result), 0 if ok else rc or 1
    return {"content": [{"type": "text", "text": text}], "isError": rc != 0}


def handle(msg: dict, *, expected_turn: str | None = None, expected_attempt: str | None = None) -> dict | None:
    method, params = msg.get("method"), msg.get("params") or {}
    if "id" not in msg:   # notifications/*
        return None
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32602, "message": "invalid params"}}
    if method == "initialize":
        res = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
               "serverInfo": {"name": "jv", "version": "1"}}
    elif method == "ping":
        res = {}
    elif method == "tools/list":
        res = {"tools": tools(catalogue.get())}
    elif method == "tools/call":
        res = call(catalogue.get(), str(params.get("name")), params.get("arguments") or {}, expected_turn=expected_turn, expected_attempt=expected_attempt)
    else:
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "unknown method"}}
    return {"jsonrpc": "2.0", "id": msg["id"], "result": res}


def _read_requests(inbox: queue.Queue) -> None:
    try:
        for line in sys.stdin:
            # Bind at transport receipt, before the serial executor can queue behind another command.
            inbox.put((line, *catalogue.execution_snapshot()))
    finally:
        inbox.put(None)


def main() -> None:
    inbox = queue.Queue()
    threading.Thread(target=_read_requests, args=(inbox,), daemon=True).start()
    while (received := inbox.get()) is not None:
        line, expected_turn, expected_attempt = received
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        try:
            reply = handle(msg, expected_turn=expected_turn, expected_attempt=expected_attempt)
        except Exception as e:   # one bad request must not take Claude's tools away for the process lifetime
            print(f"mcp_jv: {msg.get('method')} failed: {e!r}", file=sys.stderr)
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": "internal error"}} \
                if "id" in msg else None
        if reply is not None:
            print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
