"""Authenticated parse-once times across separate confirmation and execution."""
import base64
from datetime import datetime, timedelta
import json
import os
from pathlib import Path

import pytest

from heardback.tasks import personal
from jv import reminder


@pytest.fixture
def receipt(run_jv, monkeypatch):
    monkeypatch.setenv("HEARDBACK_TAINTED", "1")
    monkeypatch.setenv("HEARDBACK_TURN", "receipt-turn")
    rc, data, _ = run_jv("reminder", "add", "--when", "in 10 minutes", "--dry-run", "--json", "--", "call mum")
    assert rc == 3
    monkeypatch.delenv("HEARDBACK_TAINTED")
    return data["source_revision"], run_jv


def test_confirmed_receipt_never_reparses(receipt, monkeypatch):
    revision, run = receipt
    def fail(*a, **kw):
        raise AssertionError("confirmed execution reparsed")
    monkeypatch.setattr(reminder, "parse_when", fail)
    rc, data, _ = run("reminder", "add", "--when", "in 10 minutes", "--source-revision", revision,
                      "--yes", "--json", "--", "call mum")
    assert rc == 0 and data["reminder"]["request_key"].startswith("reminder:receipt-turn:")


@pytest.mark.parametrize("change", ["due", "text", "when", "turn", "prefix"])
def test_forged_or_rebound_receipt_cannot_choose_time(receipt, monkeypatch, change):
    revision, run = receipt
    text, when = "call mum", "in 10 minutes"
    if change == "due":
        encoded, signature = revision.removeprefix("reminder-add:").split(".")
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        payload[2] += 86400
        revision = "reminder-add:" + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode() + "." + signature
    elif change == "text":
        text = "pay rent"
    elif change == "when":
        when = "in 2 hours"
    elif change == "turn":
        monkeypatch.setenv("HEARDBACK_TURN", "other-turn")
    else:
        revision = revision.removeprefix("reminder-add:")
    rc, data, _ = run("reminder", "add", "--when", when, "--source-revision", revision,
                      "--yes", "--json", "--", text)
    assert rc == 1 and data["say"] == "That changed, please say it again."
    assert personal.list_reminders() == []


def test_expired_due_refuses(receipt, monkeypatch):
    revision, run = receipt
    monkeypatch.setattr(reminder, "_now", lambda: datetime.now().astimezone() + timedelta(hours=1))
    rc, data, _ = run("reminder", "add", "--when", "in 10 minutes", "--source-revision", revision,
                      "--yes", "--json", "--", "call mum")
    assert rc == 1 and data["say"] == "That changed, please say it again."
    assert personal.list_reminders() == []


@pytest.mark.parametrize("kind", ["symlink", "mode", "size"])
def test_unsafe_key_refuses(run_jv, monkeypatch, kind):
    path = Path(os.environ["XDG_RUNTIME_DIR"]) / "heardback-reminder.key"
    if kind == "symlink":
        target = path.with_suffix(".target")
        target.write_bytes(b"x" * 32)
        path.symlink_to(target)
    else:
        path.write_bytes(b"x" * (33 if kind == "size" else 32))
        path.chmod(0o644 if kind == "mode" else 0o600)
    monkeypatch.setenv("HEARDBACK_TAINTED", "1")
    rc, data, _ = run_jv("reminder", "add", "--when", "in 10 minutes", "--dry-run", "--json", "--", "call mum")
    assert rc == 1 and data["say"] == "I couldn't safely confirm that reminder."


def test_clean_add_parses_once(run_jv, monkeypatch):
    original = reminder.parse_when
    calls = []
    def parse(*a, **kw):
        calls.append(1)
        return original(*a, **kw)
    monkeypatch.setattr(reminder, "parse_when", parse)
    rc, data, _ = run_jv("reminder", "add", "--when", "in 10 minutes", "--json", "--", "call mum")
    assert rc == 0 and calls == [1]


@pytest.mark.parametrize("text,when,want", [
    ("I need to start work at 5.40", "5.40", "start work"),
    ("to call mum", None, "call mum"),
    ("call mum", "5.40", "call mum"),
    ("I need to", "5.40", "I need to"),
])
def test_tidy_text(text, when, want):
    assert reminder._tidy(text, when) == want
