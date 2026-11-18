"""One call at a time: permission, isolated real CLI, result, then next call."""
import contextlib
import io
import json
import os

import fake_claude as fake
from jv import cli
from heardback.brain import catalogue


def turn(text):
    body = fake.request_body(text)
    plans = json.loads(os.environ["PERSONAL_STREAM"])
    plan = next(v for k, v in plans.items() if k in body)
    fake.out({"type": "system", "subtype": "init", "tools": fake.TOOLS, "model": "claude-haiku-5-5"})
    cat = {v["name"]: v for v in cli.catalogue(cli.all_verbs())["verbs"]}
    for i, call in enumerate(plan.get("calls", [])):
        msg_id = "m0" if plan.get("same_message") else f"m{i}"
        fake.out({"type": "assistant", "message": {"id": msg_id, "content": [{"type": "tool_use", "id": str(i),
            "name": call["name"], "input": call.get("args", {})}]}})
        fake.out({"type": "control_request", "request_id": str(i), "request": {
            "subtype": "can_use_tool", "tool_name": call["name"], "input": call.get("args", {}), "tool_use_id": str(i)}})
        answer = None
        while answer is None:
            msg = fake.next_msg()
            if msg.get("type") == "control_response":
                answer = msg["response"]["response"]
            elif msg.get("type") == "control_request":
                fake.ack(msg)
        data, error = answer.get("message", ""), True
        if answer["behavior"] == "allow":
            os.environ["HEARDBACK_TURN"] = catalogue.current_turn()
            entry = next(e for e in cat.values() if catalogue.tool_name(e) == call["name"].removeprefix("mcp__jv__"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                rc = cli.run(catalogue.build_argv(entry, answer["updatedInput"], "--yes", "--json")[1:])
            data, error = output.getvalue(), rc != 0
        fake.out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": str(i),
            "content": data, "is_error": error}]}})
    fake.reply(plan.get("text", "I saved everything.\n"))


fake.turn = turn
fake.main()
