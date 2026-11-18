"""Memory writes use disposable config files, never the user's memory."""
import concurrent.futures
import os
import stat

import pytest

from heardback.brain import memory


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = memory.memory_path()
    assert path == tmp_path / "config/heardback/memory.md"
    assert path.is_relative_to(tmp_path)
    path.parent.mkdir(parents=True)
    return path


def call(run_jv, action, text, *options):
    return run_jv("fact", action, *options, "--json", "--", text)


def save(run_jv, action, text):
    rc, data, _ = call(run_jv, action, text)
    assert rc == 3 and "source_revision" in data
    return call(run_jv, action, text, "--yes", "--source-revision", data["source_revision"])


@pytest.mark.parametrize("text", ["sample fact", "# heading\nsecond line", "x" * 160, "é" * 160])
def test_remember_creates_private_file(run_jv, isolated_memory, text):
    assert save(run_jv, "remember", text)[:2] == (0, {"say": "Saved.", "ok": True, "observed": True})
    body = isolated_memory.read_text()
    assert "## Remembered\n- " + " ".join(text.split()) + "\n" in body
    assert stat.S_IMODE(isolated_memory.stat().st_mode) == 0o600


@pytest.mark.parametrize("text", ["", "x" * 161, "é" * 161, "a\x00b"])
def test_invalid_text_never_asks_or_returns_two(run_jv, isolated_memory, text):
    rc, data, _ = call(run_jv, "remember", text)
    assert rc in (1, 4) and data["say"] and "confirm" not in data
    assert not isolated_memory.exists()


@pytest.mark.parametrize("delta", [0, 1])
@pytest.mark.parametrize("text", ["abc", "ééé"])
def test_byte_cap_checked_before_asking(run_jv, isolated_memory, delta, text):
    suffix = "\n\n## Remembered\n- " + text + "\n"
    old = "x" * (memory.MAX_BYTES - len(suffix.encode()) + delta)
    isolated_memory.write_text(old)
    rc, data, _ = call(run_jv, "remember", text)
    assert rc == (3 if delta == 0 else 1)
    if delta:
        assert data["say"] == "Memory is full, nothing saved"
    else:
        assert save(run_jv, "remember", text)[0] == 0
        assert isolated_memory.stat().st_size == memory.MAX_BYTES


@pytest.mark.parametrize("action", ["remember", "forget"])
def test_hand_edit_invalidates_yes(run_jv, isolated_memory, action):
    isolated_memory.write_text("- sample fact\n")
    rc, data, _ = call(run_jv, action, "sample fact")
    assert rc == 3
    isolated_memory.write_text("- sample fact\n- hand edit\n")
    before = isolated_memory.read_bytes()
    assert call(run_jv, action, "sample fact", "--yes", "--source-revision", data["source_revision"])[0] == 1
    assert isolated_memory.read_bytes() == before


def test_crash_before_replace_keeps_original(run_jv, isolated_memory, monkeypatch):
    from jv import facts
    isolated_memory.write_text("original\n")
    def crash(*args):
        raise OSError("private failure")
    monkeypatch.setattr(facts.os, "replace", crash)
    assert save(run_jv, "remember", "sample fact")[0] == 1
    assert isolated_memory.read_text() == "original\n"
    assert list(isolated_memory.parent.iterdir()) == [isolated_memory]


@pytest.mark.parametrize("kind", ["symlink", "invalid", "oversize", "directory"])
def test_bad_file_refused(run_jv, isolated_memory, kind):
    if kind == "symlink":
        target = isolated_memory.parent / "target"
        target.write_text("untouched")
        isolated_memory.symlink_to(target)
    elif kind == "directory":
        isolated_memory.mkdir()
    else:
        isolated_memory.write_bytes(b"\xff" if kind == "invalid" else b"x" * 8193)
    for action in ("remember", "forget"):
        assert call(run_jv, action, "sample fact")[0] in (1, 4)
    if kind == "symlink":
        assert target.read_text() == "untouched" and isolated_memory.is_symlink()


def test_section_insertion_preserves_other_sections(run_jv, isolated_memory):
    isolated_memory.write_text("# Facts\n\n## Remembered\n- old\n\n## Other\n- retained\n")
    assert save(run_jv, "remember", "sample fact")[0] == 0
    assert isolated_memory.read_text() == "# Facts\n\n## Remembered\n- old\n\n- sample fact\n## Other\n- retained\n"


@pytest.mark.parametrize("text", ["it", "IT", "that", "this", "a", ""])
def test_vague_forget_refused(run_jv, text):
    rc, data, _ = call(run_jv, "forget", text)
    assert rc == 1 and data["say"] == "Say what to forget."


@pytest.mark.parametrize("body,say", [
    ("## sample\n\n", "Nothing in memory matches."),
    ("- sample\n" * 4, "4 lines match, please edit the file."),
    ("- " + "sample" * 40 + "\n", None),
])
def test_forget_no_unheard_deletion(run_jv, isolated_memory, body, say):
    isolated_memory.write_text(body)
    rc, data, _ = call(run_jv, "forget", "sample")
    assert rc == (1 if say else 4)
    if say:
        assert data["say"] == say
    assert isolated_memory.read_text() == body


def test_forget_matches_lines_case_insensitively(run_jv, isolated_memory):
    isolated_memory.write_text("# SAMPLE\n\n- Sample one\nplain sample two\n- keep\n")
    rc, data, _ = call(run_jv, "forget", "SAMPLE")
    assert rc == 3 and "- Sample one" in data["confirm"] and "plain sample two" in data["confirm"]
    assert save(run_jv, "forget", "SAMPLE")[:2] == (0, {"say": "Forgotten.", "ok": True, "observed": True})
    assert isolated_memory.read_text() == "# SAMPLE\n\n- keep\n"


def test_yes_needs_revision_and_touch_does_not_conflict(run_jv, isolated_memory):
    isolated_memory.write_text("original\n")
    assert call(run_jv, "remember", "sample fact", "--yes")[0] == 3
    rc, data, _ = call(run_jv, "remember", "sample fact")
    os.utime(isolated_memory)
    assert call(run_jv, "remember", "sample fact", "--yes", "--source-revision", data["source_revision"])[0] == 0


def test_two_writers_share_lock(isolated_memory):
    from jv import facts
    from jv.verb import JvError
    isolated_memory.write_text("original\n")
    args = [{"text": text} for text in ("one fact", "two fact")]
    for a in args:
        facts.VERBS[0].check(a)
        a["source_revision"] = a["_source_revision"]
    def write(a):
        try:
            return facts.VERBS[0].fn(a)["say"]
        except JvError as exc:
            return exc.code
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, args))
    assert sorted(map(str, results)) == ["1", "Saved."]
    assert sum(text in isolated_memory.read_text() for text in ("one fact", "two fact")) == 1


def test_saved_only_after_replace(run_jv, isolated_memory, monkeypatch):
    from jv import facts
    replace = facts.os.replace
    replaced = []
    def commit(src, dst):
        assert "sample fact" not in isolated_memory.read_text()
        replace(src, dst)
        replaced.append(True)
    monkeypatch.setattr(facts.os, "replace", commit)
    assert save(run_jv, "remember", "sample fact")[1]["say"] == "Saved."
    assert replaced


@pytest.mark.parametrize("argv", [["fact"], ["fact", "remember"], ["fact", "forget", "--unknown"],
                                    ["fact", "remember", "x" * 4001]])
def test_cli_errors_never_exit_two(run_jv, argv):
    rc, data, err = run_jv(*argv, "--json")
    assert rc in (1, 4) and data["say"]
    assert "x" * 30 not in err


@pytest.mark.parametrize("size,expected", [(200, 3), (201, 4)])
def test_forget_readback_boundary(run_jv, isolated_memory, size, expected):
    line = "sample" + "x" * (size - len("Forget these lines: ?") - 6)
    isolated_memory.write_text(line + "\n")
    rc, data, _ = call(run_jv, "forget", "sample")
    assert rc == expected
    if expected == 3:
        assert len(data["confirm"]) == size


def test_three_matches_allowed(run_jv, isolated_memory):
    isolated_memory.write_text("- sample\n" * 3 + "- keep\n")
    assert save(run_jv, "forget", "sample")[0] == 0
    assert isolated_memory.read_text() == "- keep\n"


def test_recheck_before_replace_preserves_hand_edit(run_jv, isolated_memory, monkeypatch):
    from jv import facts
    isolated_memory.write_text("original\n")
    fsync = facts.os.fsync
    def edit(fd):
        fsync(fd)
        isolated_memory.write_text("hand edit during staging\n")
    monkeypatch.setattr(facts.os, "fsync", edit)
    assert save(run_jv, "remember", "sample fact")[0] == 1
    assert isolated_memory.read_text() == "hand edit during staging\n"


def test_fsync_failure_preserves_original(run_jv, isolated_memory, monkeypatch):
    from jv import facts
    isolated_memory.write_text("original\n")
    def fail(fd):
        raise OSError("private fixture error")
    monkeypatch.setattr(facts.os, "fsync", fail)
    rc, data, err = save(run_jv, "remember", "sample fact")
    assert rc == 1 and "private fixture error" not in str(data) + err
    assert isolated_memory.read_text() == "original\n"


def test_lock_symlink_refused(run_jv, isolated_memory):
    target = isolated_memory.parent / "target"
    target.write_text("untouched")
    lock = isolated_memory.parents[2] / "heardback-memory.lock"
    lock.symlink_to(target)
    assert save(run_jv, "remember", "sample fact")[0] == 1
    assert target.read_text() == "untouched" and not isolated_memory.exists()


def test_remember_preserves_trailing_blank_lines(run_jv, isolated_memory):
    before = "# Test\n\n\n\n"
    isolated_memory.write_text(before)
    assert save(run_jv, "remember", "sample fact")[0] == 0
    assert isolated_memory.read_text().startswith(before + "## Remembered")


def test_whitespace_only_full_file_refused(run_jv, isolated_memory):
    isolated_memory.write_text(" " * memory.MAX_BYTES)
    rc, data, _ = call(run_jv, "remember", "sample fact")
    assert rc == 1 and data["say"] == "Memory is full, nothing saved"
    assert isolated_memory.stat().st_size == memory.MAX_BYTES
