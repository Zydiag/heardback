"""Dashboard reminder edits: real server on port 0, real store in a temp state dir, real coordinator tick."""
import asyncio
import http.client
import json
from types import SimpleNamespace

import pytest

from heardback import coordinator
from heardback.brain.brain import Brain
from heardback.dash import server, sources
from heardback.status.announce import Announcer
from heardback.status import away
from heardback.tasks import personal

NOW = 1800000000


class Socket:
    def __init__(self, brain):
        self.brain, self.sent = brain, []

    def is_closing(self):
        return False

    def write(self, data):
        msg = json.loads(data)
        self.sent.append(msg)
        if msg.get("delivery_id"):
            asyncio.get_running_loop().create_task(self.brain.handle(
                {"type": "delivery", "id": msg["delivery_id"], "result": "spoken"}, self))

    async def drain(self):
        pass


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sources.time, "time", lambda: NOW)
    s = server.start(0)
    yield s
    s.shutdown()


def post(srv, data, headers=None, body=None):
    port = srv.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        c.request("POST", "/api/reminder", body=body if body is not None else json.dumps(data).encode(),
                  headers={"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                           "Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        return r.status, r.read().decode()
    finally:
        c.close()


def pending():
    return personal.list_reminders(state="pending")


def test_add_edit_cancel(srv):
    assert post(srv, {"op": "add", "text": " buy milk ", "due_at": NOW + 600}) == (204, "")
    (row,) = pending()
    assert (row["text"], row["due_at"]) == ("buy milk", NOW + 600)
    assert sources.reminders()["rows"][0]["text"] == "buy milk"
    assert post(srv, {"op": "edit", "id": row["reminder_id"], "text": "buy oat milk", "due_at": NOW + 900})[0] == 204
    (row,) = pending()
    assert (row["text"], row["due_at"], row["revision"]) == ("buy oat milk", NOW + 900, 2)
    assert post(srv, {"op": "cancel", "id": row["reminder_id"]})[0] == 204
    assert pending() == []
    assert post(srv, {"op": "cancel", "id": row["reminder_id"]})[0] == 409   # no longer pending


@pytest.mark.parametrize("data,headers,body,code", [
    ({"op": "add", "text": "x", "due_at": NOW + 60}, {"Origin": "http://evil.example"}, None, 403),
    ({"op": "add", "text": "x", "due_at": NOW + 60}, {"Origin": ""}, None, 403),
    ({"op": "add", "text": "x", "due_at": NOW + 60}, {"Host": "evil.example"}, None, 403),
    ({"op": "add", "text": "x", "due_at": NOW + 60}, {"Content-Type": "text/plain"}, None, 415),
    (None, {}, b"x" * 8193, 413),
    (None, {}, b"[]", 400),
    ({"op": "add", "text": "x", "due_at": NOW - 1}, {}, None, 400),        # past time
    ({"op": "add", "text": "x", "due_at": True}, {}, None, 400),
    ({"op": "add", "text": "   ", "due_at": NOW + 60}, {}, None, 400),     # empty text
    ({"op": "add", "text": "x" * 301, "due_at": NOW + 60}, {}, None, 400),  # over the voice limit
    ({"op": "nope"}, {}, None, 400),
])
def test_guards_refuse(srv, data, headers, body, code):
    assert post(srv, data, headers, body)[0] == code
    assert pending() == []


def test_edit_refuses_bad_values_and_keeps_row(srv):
    post(srv, {"op": "add", "text": "keep", "due_at": NOW + 600})
    rid = pending()[0]["reminder_id"]
    for bad in ({"text": "", "due_at": NOW + 5}, {"text": "keep", "due_at": NOW - 5}):
        assert post(srv, {"op": "edit", "id": rid, **bad})[0] == 400
    assert (pending()[0]["text"], pending()[0]["due_at"]) == ("keep", NOW + 600)


@pytest.fixture
def rig(monkeypatch):
    clock = SimpleNamespace(now=float(NOW))
    monkeypatch.setattr(coordinator.time, "time", lambda: clock.now)
    monkeypatch.setattr(away, "is_muted", lambda now: False)
    monkeypatch.setattr(coordinator, "volume_now", lambda: (50, False))
    b = Brain(announcer=Announcer(), voice_token="test-voice-token")
    b.voice = Socket(b)
    return b, coordinator.Coordinator(b), clock


def said(b):
    return [m["text"] for m in b.voice.sent if m["type"] == "say"]


def test_coordinator_sees_edited_time(srv, rig):
    b, c, clock = rig
    post(srv, {"op": "add", "text": "call mum", "due_at": NOW + 60})
    rid = pending()[0]["reminder_id"]
    post(srv, {"op": "edit", "id": rid, "text": "call dad", "due_at": NOW + 3600})
    clock.now = NOW + 120               # past the OLD time
    asyncio.run(c.tick())
    assert said(b) == []
    clock.now = NOW + 3601              # past the NEW time
    asyncio.run(c.tick())
    assert any("call dad" in t.lower() for t in said(b)) and not any("call mum" in t.lower() for t in said(b))


def test_coordinator_never_fires_cancelled(srv, rig):
    b, c, clock = rig
    post(srv, {"op": "add", "text": "call mum", "due_at": NOW + 60})
    post(srv, {"op": "cancel", "id": pending()[0]["reminder_id"]})
    clock.now = NOW + 120
    asyncio.run(c.tick())
    assert said(b) == []
