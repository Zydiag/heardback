import importlib

import pytest

from jv import user
from test_user import OK, _bin_on_path, _reload_user_after, actions  # noqa: F401  (fixtures)

LOFI = '[[action]]\nname = "lofi"\ndescription = "Lo-fi on YouTube."\nphrases = ["lofi"]\nconfirm = false\nrun = [["jvt-echo", "https://youtube.com/lofi"]]\n'


def test_draft_written_only_to_drafts(actions, run_jv_with):
    actions(OK)
    code, out, _ = run_jv_with(user.build()[0], "do", "draft", "lofi", LOFI, "--json")
    assert code == 0 and out["say"] == "Drafted lofi. Say 'add lofi' to make it live."
    assert (user.drafts_dir() / "lofi.toml").read_text() == LOFI and "lofi" not in user.actions_path().read_text()


@pytest.mark.parametrize("argv0", ["bash", "rm", "sudo", "python3", "mkfs.ext4", "setsid"])
def test_draft_banned_binaries(actions, run_jv_with, argv0):
    actions(OK)
    code, _, err = run_jv_with(user.build()[0], "do", "draft", "lofi", LOFI.replace("jvt-echo", argv0))
    assert code == 2 and f"{argv0} is not allowed in a draft" in err


def test_draft_name_mismatch_clash_and_two_entries(actions, run_jv_with):
    actions(OK)
    v = user.build()[0]
    code, _, err = run_jv_with(v, "do", "draft", "other", LOFI)
    assert code == 2 and "lofi" in err
    assert run_jv_with(v, "do", "draft", "echo-it", OK)[2].strip() == "echo-it already exists"
    code, _, err = run_jv_with(v, "do", "draft", "lofi", LOFI + LOFI.replace("lofi", "lofi2"))
    assert code == 2 and "one" in err


def test_draft_too_long(actions, run_jv_with):
    actions(OK)
    code, _, err = run_jv_with(user.build()[0], "do", "draft", "lofi", LOFI + "#" * 4000)
    assert code == 2 and "too long" in err


def test_accept_readback_from_file(actions, run_jv_with):
    actions(OK)
    v = user.build()[0]
    run_jv_with(v, "do", "draft", "lofi", LOFI)
    code, out, _ = run_jv_with(v, "do", "accept", "lofi", "--dry-run", "--json")
    assert code == 3 and out["confirm"] == ("New command lofi: when you say 'lofi', it runs "
                                            "jvt-echo https://youtube.com/lofi, without asking first. Add it?")


def test_readback_variants():
    e = user.load(LOFI.replace('phrases = ["lofi"]', 'phrases = ["lofi", "chill"]')
                  .replace("confirm = false", "confirm = true")
                  .replace('run = [["jvt-echo", "https://youtube.com/lofi"]]', 'run = [["jvt-echo", "a"], ["jvt-echo", "b"]]'))[0][0]
    assert user.readback(e) == "New command lofi: when you say 'lofi' or 'chill', it runs jvt-echo a, then jvt-echo b. Add it?"
    assert user.readback({**e, "phrases": []}) == "New command lofi: it runs jvt-echo a, then jvt-echo b. Add it?"


def test_hand_edited_draft_rechecked(actions, run_jv_with):
    actions(OK)
    user.drafts_dir().mkdir(parents=True)
    (user.drafts_dir() / "lofi.toml").write_text(LOFI.replace("jvt-echo", "bash"))
    code, _, err = run_jv_with(user.build()[0], "do", "accept", "lofi", "--yes")
    assert code == 2 and "not allowed in a draft" in err


def test_accept_yes_appends_keeps_bak_removes_draft(actions, run_jv_with):
    actions(OK)
    v = user.build()[0]
    run_jv_with(v, "do", "draft", "lofi", LOFI)
    assert run_jv_with(v, "do", "accept", "lofi", "--yes")[0] == 0
    assert "do.lofi" in {x.name for x in user.build()[0]}
    assert user.actions_path().with_suffix(".toml.bak").read_text() == OK
    assert user.draft_names() == []


def test_accept_into_missing_file(actions, run_jv_with):
    f = actions(OK)
    f.unlink()
    user.drafts_dir().mkdir(parents=True)
    (user.drafts_dir() / "lofi.toml").write_text(LOFI)
    assert run_jv_with(user.build()[0], "do", "accept", "lofi", "--yes")[0] == 0
    assert f.read_text() == LOFI and not f.with_suffix(".toml.bak").exists()


def test_accept_rolls_back_corrupt_result(actions, run_jv_with):
    actions(OK + "\n[[action]]\nname = ")          # current file already ends mid-entry
    user.drafts_dir().mkdir(parents=True)
    (user.drafts_dir() / "lofi.toml").write_text(LOFI)
    code = run_jv_with(user.build()[0], "do", "accept", "lofi", "--yes")[0]
    assert code == 1 and user.actions_path().read_text() == OK + "\n[[action]]\nname = " and user.draft_names() == ["lofi"]


def test_accept_no_draft(actions, run_jv_with):                     # Review Focus 2
    actions(OK)
    code, _, err = run_jv_with(user.build()[0], "do", "accept", "x", "--json")
    assert code == 1 and "no draft called x" in err


def test_catalogue_choices_are_draft_names(actions, run_jv_with):
    actions(OK)
    importlib.reload(user)
    acc = next(v for v in run_jv_with(user.VERBS, "list", "--json")[1]["verbs"] if v["name"] == "do.accept")
    assert acc["args"]["name"] == {"type": "enum", "choices": []}
    user.drafts_dir().mkdir(parents=True)
    (user.drafts_dir() / "lofi.toml").write_text(LOFI)
    acc = next(v for v in run_jv_with(user.VERBS, "list", "--json")[1]["verbs"] if v["name"] == "do.accept")
    assert acc["args"]["name"]["choices"] == ["lofi"] and acc["confirm"] is True


def test_accept_ignores_other_entries_problems(actions, run_jv_with):
    broken = LOFI.replace('"lofi"', '"play-lofi"').replace("jvt-echo", "no-such-bin-xyz")   # "play-lofi: … not found"
    actions(OK + broken)
    user.drafts_dir().mkdir(parents=True)
    (user.drafts_dir() / "lofi.toml").write_text(LOFI)
    assert run_jv_with(user.build()[0], "do", "accept", "lofi", "--yes")[0] == 0
    assert user.draft_names() == []
