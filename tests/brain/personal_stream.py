"""Task 7 fake provider: scripted stream with real isolated personal CLI calls."""
import contextlib
from datetime import datetime
import io
import json
import os

import fake_claude as fake
from jv import cli, reminder
from heardback.brain import catalogue


def turn(text):
    plan = json.loads(os.environ["PERSONAL_STREAM"])[fake.request_body(text)]
    fake.out({"type": "system", "subtype": "init", "tools": fake.TOOLS, "model": "claude-haiku-5-5"})
    calls = plan.get("calls", [])
    for i, call in enumerate(calls):
        fake.out({"type": "assistant", "message": {"id": "m0", "content": [{"type": "tool_use", "id": str(i),
                  "name": call["name"], "input": call.get("args", {})}]}})
        fake.out({"type": "control_request", "request_id": str(i), "request": {
            "subtype": "can_use_tool", "tool_name": call["name"], "input": call.get("args", {}), "tool_use_id": str(i)}})
    answers = {}
    while len(answers) < len(calls):
        msg = fake.next_msg()
        if msg.get("type") == "control_response":
            answers[msg["response"]["request_id"]] = msg["response"]["response"]
        elif msg.get("type") == "control_request":
            fake.ack(msg)
    cat = {v["name"]: v for v in cli.catalogue(cli.all_verbs())["verbs"]}
    ordered = list(enumerate(calls))
    for i, call in reversed(ordered) if plan.get("reverse_results") else ordered:
        answer = answers[str(i)]
        data, error = answer.get("message", ""), True
        if answer["behavior"] == "allow":
            if "result" in call:
                data, error = call["result"], call.get("error", False)
            else:
                fake.wait(call.get("delay", 0), False)
                os.environ["HEARDBACK_TURN"] = catalogue.current_turn()
                if "now" in call:
                    reminder._now = lambda: datetime.fromisoformat(call["now"])
                entry = next(e for e in cat.values() if catalogue.tool_name(e) == call["name"].removeprefix("mcp__jv__"))
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    rc = cli.run(catalogue.build_argv(entry, answer["updatedInput"], "--yes", "--json")[1:])
                data, error = output.getvalue(), rc != 0
        fake.wait(call.get("result_delay", 0), False)
        fake.out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": str(i),
                  "content": data if isinstance(data, str) else json.dumps(data), "is_error": error}]}})
    fake.reply(plan.get("text", "I saved everything.\n"))


fake.turn = turn
fake.main()
