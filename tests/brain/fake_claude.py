"""Fake stream-json Claude process, driven by cues in heardback's current-request envelope."""
import json
import os
import queue
import sys
import threading
import time

PID = os.getpid()
LOG = os.environ.get("FAKE_CLAUDE_LOG")
# unset: list the MCP catalogue; "" = MCP failed to load
_tools = os.environ.get("FAKE_CLAUDE_TOOLS")
if _tools is None and "--mcp-config" in sys.argv:
    from heardback.brain import catalogue, mcp_jv
    _tools = ",".join("mcp__jv__" + t["name"] for t in mcp_jv.tools(catalogue.get()))
TOOLS = [t for t in (_tools or "").split(",") if t]
inbox: "queue.Queue[str | None]" = queue.Queue()
pending: list[dict] = []   # user messages that arrived mid-turn
cost = 0.0
rid = 0


def log(**kw) -> None:
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps({"pid": PID, "t": time.time(), **kw}) + "\n")


def out(msg: dict) -> None:
    print(json.dumps(msg), flush=True)


def reader() -> None:
    for line in sys.stdin:
        log(**{"in": line.rstrip("\n")})
        inbox.put(line)
    inbox.put(None)


def ack(msg: dict) -> None:
    out({"type": "control_response", "response": {"subtype": "success", "request_id": msg.get("request_id")}})


def reported_model(source: str) -> str | None:
    if os.environ.get("FAKE_CLAUDE_NO_MODEL_EVIDENCE") == "1":
        return None
    override = os.environ.get(f"FAKE_CLAUDE_{source.upper()}_MODEL")
    if override == "none":
        return None
    return override or "claude-haiku-5-5"


def request_body(content: str) -> str:
    marker = "\n\nCurrent request:\n"
    return content.split(marker, 1)[1] if marker in content else content


def next_msg(timeout: float | None = None) -> dict | None:
    """Next parsed stdin message; exits on EOF; None on timeout."""
    try:
        line = inbox.get(timeout=timeout)
    except queue.Empty:
        return None
    if line is None:
        sys.exit(0)
    try:
        return json.loads(line)
    except ValueError:
        return {}


def result(text: str, subtype: str = "success", is_error: bool = False, tokens: int = 100) -> None:
    out({"type": "result", "subtype": subtype, "is_error": is_error, "result": text, "num_turns": 1,
         "total_cost_usd": cost,
         "usage": {"input_tokens": tokens, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                   "output_tokens": 5}})


def tool_use(name: str, parent: str | None = None) -> None:
    out({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"toolu_{name}", "name": name, "input": {}}]}, "parent_tool_use_id": parent})


def reply(*parts: str, tokens: int = 100) -> None:
    """Shapes as captured live (fixtures/stream_live.ndjson): deltas, then the whole `assistant` message."""
    for p in parts:
        out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": p}},
             "parent_tool_use_id": None})
    message = {"role": "assistant", "usage": {"input_tokens": tokens}, "content": [{"type": "text", "text": "".join(parts)}]}
    if model := reported_model("assistant"):
        message["model"] = model
    out({"type": "assistant", "message": message,
         "parent_tool_use_id": None})
    result("".join(parts), tokens=tokens)


def wait(seconds: float, interruptible: bool) -> bool:
    """Sleep in 0.05 s steps; True if an interrupt ended it."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        msg = next_msg(0.05)
        if msg is None:
            continue
        if msg.get("type") == "control_request":
            ack(msg)
            if interruptible and msg.get("request", {}).get("subtype") == "interrupt":
                return True
        elif msg.get("type") == "user":
            pending.append(msg)
    return False


def turn(text: str) -> None:
    global cost, rid
    text = request_body(text)
    init = {"type": "system", "subtype": "init", "tools": TOOLS}
    if model := reported_model("init"):
        init["model"] = model
    out(init)   # live: init per turn
    cue, *args = text.split(" ", 3) if text.startswith("cue:") else ("", text)
    if cue in ("cue:slow", "cue:mute"):
        if wait(float(args[0]), cue == "cue:slow"):
            return result("", "error_during_execution", True)
    elif cue == "cue:crash":
        sys.exit(1)
    elif cue == "cue:cost":
        cost += float(args[0])
        return reply("You said: ", f"{text}.", tokens=int(args[1]))
    elif cue == "cue:ask":
        return reply("Which one?")
    elif cue == "cue:asklate":               # live 2026-10-31: the question isn't the last sentence
        return reply("Which window should I type into: the new terminal I just opened, or a Claude session? ",
                     "Tell me the name and I'll send it.")
    elif cue == "cue:asknl":                 # the question streams whole: nothing left to flush
        return reply("Which one?\n")
    elif cue in ("cue:tool", "cue:afterread", "cue:typefirst"):   # afterread/typefirst: zen_read in the same message
        rid += 1
        req = f"fk-{rid}"
        name, raw = text.split(" ", 2)[1:]

        def block(block_id: str, tool: str, tool_input: dict) -> None:   # live: one event per block, one message id
            message = {"id": f"msg_{req}", "role": "assistant", "content": [
                {"type": "tool_use", "id": block_id, "name": tool, "input": tool_input}]}
            if model := reported_model("assistant"):
                message["model"] = model
            out({"type": "assistant", "message": message, "parent_tool_use_id": None})

        if cue == "cue:afterread":
            block(f"toolu_read_{req}", "mcp__jv__zen_read", {})
        block(f"toolu_{req}", name, json.loads(raw))
        out({"type": "control_request", "request_id": req,   # live order: tool_use, then ask
             "request": {"subtype": "can_use_tool", "tool_name": name, "input": json.loads(raw)}})
        if cue == "cue:typefirst":   # live: the ask lands before the next block of the same message
            block(f"toolu_read_{req}", "mcp__jv__zen_read", {})
        while True:
            msg = next_msg()
            if msg.get("type") == "control_response" and msg.get("response", {}).get("request_id") == req:
                r = msg["response"].get("response", {})
                deny = r.get("behavior") != "allow"
                data = [{"type": "text", "text": '{"ok":true}'}]
                if not deny and os.environ.get("FAKE_CLAUDE_MCP") == "1":
                    from heardback.brain import catalogue, mcp_jv
                    config = json.loads(sys.argv[sys.argv.index("--mcp-config") + 1])
                    # Keep the test's fake executables on PATH while using the brain's actual gate env.
                    os.environ.update({k: v for k, v in config["mcpServers"]["jv"]["env"].items() if k != "PATH"})
                    tid, attempt = catalogue.execution_snapshot()
                    execution = mcp_jv.call(catalogue.get(), name.removeprefix("mcp__jv__"), r["updatedInput"],
                                            expected_turn=tid, expected_attempt=attempt)
                    deny, data = execution["isError"], execution["content"]
                    log(execution=execution)
                out({"type": "user", "message": {"role": "user", "content": [   # live shape (stream_live.ndjson)
                    {"type": "tool_result", "tool_use_id": f"toolu_{req}", "is_error": deny,
                     "content": r.get("message") if r.get("behavior") != "allow" else data}]},
                     "parent_tool_use_id": None})
                return reply("allowed." if r.get("behavior") == "allow" else f"denied: {r.get('message')}.")
            if msg.get("type") == "user":
                pending.append(msg)
            elif msg.get("type") == "control_request":
                ack(msg)
    elif cue == "cue:tools":                 # cue:tools a,b: Claude ran these tools (a subagent's don't count)
        for n in args[0].split(","):
            tool_use(f"mcp__jv__{n}")
        tool_use("mcp__jv__sub_tool", parent="toolu_x")
        return reply("Done.")
    elif cue == "cue:limit":
        out({"type": "rate_limit_event", "rate_limit_info": {
            "status": args[1], "resetsAt": int(args[2]), "rateLimitType": "five_hour", "unifiedWindows": {
                "five_hour": {"utilization": float(args[0]), "resetsAt": int(args[2]), "status": args[1]}}}})
    elif cue == "cue:auth":
        return result("Not logged in · Please run /login", is_error=True)
    elif cue == "cue:maxturns":
        return result("", "error_max_turns", True)
    elif cue == "cue:error":   # any other error result, e.g. a budget stop
        return result("", args[0], True)
    reply("You said: ", f"{text}.")


def main() -> None:
    log(argv=sys.argv)
    threading.Thread(target=reader, daemon=True).start()
    while True:
        msg = pending.pop(0) if pending else next_msg()
        if msg.get("type") == "user":
            turn(str(msg.get("message", {}).get("content", "")))
        elif msg.get("type") == "control_request":
            ack(msg)


if __name__ == "__main__":
    main()
