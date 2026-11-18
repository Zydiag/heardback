"""Exercise the fixed GitHub writers with fake API calls, never a live token."""
import json
import os
import shutil
import subprocess

import pytest

from test_workflows import WORKFLOWS, block_values

MOCK_API = """
process.env.RESULT = require('fs').readFileSync(0, 'utf8');
const calls = [];
const api = name => async args => {
  calls.push([name, args]);
  if (name === 'getCommit') return {data: {tree: {sha: 'base-tree'}}};
  if (name === 'getTree') return {data: {tree: [{path: 'scripts/check.sh', mode: '100755'}]}};
  return {data: {sha: 'created-sha'}};
};
const github = {rest: {
  git: Object.fromEntries(['getCommit', 'getTree', 'createTree', 'createCommit', 'createRef'].map(n => [n, api(n)])),
  pulls: {create: api('createPR')},
  issues: {addLabels: api('addLabels'), createComment: api('comment')}
}};
const context = {repo: {owner: 'example', repo: 'heardback'}, issue: {number: 12}, runId: 34};
const core = {notice: () => {}};
"""


def publish(workflow, result):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed to exercise actions/github-script")
    script = list(block_values((WORKFLOWS / workflow).read_text(), "script"))[-1]
    script = script.removeprefix("|\n")
    code = MOCK_API + "\n(async () => {\n" + script + "\n})().then(() => {\n" + \
           "console.log(JSON.stringify({calls}));\n" + \
           "}).catch(e => console.log(JSON.stringify({error: e.message, calls})));"
    env = {**os.environ, "BASE_SHA": "base-sha",
           "BASE_BRANCH": "main", "GITHUB_RUN_ATTEMPT": "1"}
    run = subprocess.run([node, "-e", code], input=json.dumps(result), env=env,
                         capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def proposal(*files):
    return {"title": "Fix parser", "body": "Tests were not run.", "files": list(files)}


def test_fix_publishes_only_new_branch_and_draft_pr():
    result = publish("fix.yml", proposal({"path": "scripts/check.sh", "content": "replacement"},
                                        {"path": "tests/test_new.py", "content": "assert True"}))
    assert "error" not in result
    calls = dict(result["calls"])
    assert calls["getCommit"]["commit_sha"] == "base-sha"
    assert calls["createTree"]["base_tree"] == "base-tree"
    assert [f["mode"] for f in calls["createTree"]["tree"]] == ["100755", "100644"]
    assert calls["createCommit"]["parents"] == ["base-sha"]
    assert calls["createRef"]["ref"] == "refs/heads/bot/fix-12-34-1"
    assert calls["createPR"]["head"] == "bot/fix-12-34-1"
    assert calls["createPR"]["base"] == "main" and calls["createPR"]["draft"]
    assert "tests were not run" in calls["createPR"]["body"]
    assert set(calls) == {"getCommit", "getTree", "createTree", "createCommit", "createRef", "createPR"}


@pytest.mark.parametrize("path", [".github/workflows/leak.yml", ".git/config", "../escape",
                                 "src/../../escape", "/tmp/escape", "src//file", "src/.env",
                                 "src/CLAUDE.md", "CLAUDE.md", "src\\escape", ""])
def test_fix_rejects_unsafe_paths_before_any_write(path):
    result = publish("fix.yml", proposal({"path": path, "content": "untrusted"}))
    assert result["error"] == "Unsafe fix file" and not result["calls"]


@pytest.mark.parametrize("files", [
    [{"path": "src/a.py", "content": "one"}, {"path": "src/a.py", "content": "two"}],
    [{"path": "src/a.py", "content": "x" * 200001}],
    [{"path": "src/a.py", "content": None}],
])
def test_fix_rejects_invalid_files_before_any_write(files):
    result = publish("fix.yml", proposal(*files))
    assert result["error"] == "Unsafe fix file" and not result["calls"]


def test_no_fix_does_not_publish():
    assert publish("fix.yml", proposal()) == {"calls": []}


def test_triage_can_only_label_and_comment():
    result = publish("triage.yml", {"labels": ["bug"], "comment": "Please add reproduction steps."})
    assert [name for name, _ in result["calls"]] == ["addLabels", "comment"]
    assert all(args["issue_number"] == 12 for _, args in result["calls"])
    assert publish("triage.yml", {"labels": [], "comment": ""}) == {"calls": []}
    bad = publish("triage.yml", {"labels": ["bot:fix"], "comment": ""})
    assert bad["error"] == "Invalid triage output" and not bad["calls"]


def test_review_can_only_post_comment():
    result = publish("review.yml", {"comment": "Concrete finding at src/example.py:12."})
    assert [name for name, _ in result["calls"]] == ["comment"]
    assert result["calls"][0][1]["issue_number"] == 12
    assert publish("review.yml", {"comment": ""})["error"] == "Invalid review output"
