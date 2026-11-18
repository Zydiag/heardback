"""Security regressions for both the source tree and the public export."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (ROOT / "public/.github/workflows" if (ROOT / "public").is_dir()
             else ROOT / ".github/workflows")


def block_values(text, key):
    """Read inline values and indented YAML scalars used by our workflows."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = re.match(rf"^(\s*)(?:-\s+)?(['\"]?){key}\2\s*:\s*(.*)$", line)
        if not match:
            continue
        column = line.index(key)
        value = [match[3]]
        for following in lines[i + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) <= column:
                break
            value.append(following[column + 2:])
        yield "\n".join(value)


def check_workflow(text):
    assert "pull_request_target" not in text, "forbidden privileged PR event"
    for run in block_values(text, "run"):
        assert not run.lstrip().startswith("*"), "run aliases hide shell text"
        assert not re.search(r"\$\{\{.*?github\s*(?:\.\s*event|\[\s*['\"]event['\"]\s*\]).*?\}\}",
                             run, re.S), "event data interpolated into shell"


def test_public_workflows():
    paths = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert paths, "workflows missing from source/export"
    for path in paths:
        check_workflow(path.read_text())


@pytest.mark.parametrize("bad", [
    "on: pull_request_target\n",
    "      - run: echo '${{ github.event.issue.title }}'\n",
    '      - "run": echo "${{ github.event.issue.body }}"\n',
    "      - run: |\n          echo ${{ github.event.issue.body }}\n",
    "      - run: >-\n          echo ${{\n            github.event.issue.body\n          }}\n",
    "      - run: echo ${{ github['event'].issue.body }}\n",
    "      - run: *hidden\n",
])
def test_rejects_unsafe_workflow(bad):
    with pytest.raises(AssertionError):
        check_workflow(bad)


def test_event_in_env_is_data():
    check_workflow('      - run: echo "$TITLE"\n        env:\n'
                   '          TITLE: ${{ github.event.issue.title }}\n')


@pytest.mark.parametrize("name", ["triage", "review", "fix"])
def test_bot_has_no_builtin_tools(name):
    text = (WORKFLOWS / f"{name}.yml").read_text()
    # The pinned action treats --tools "" as a bare flag, losing the empty value.
    assert re.search(r"(?m)^\s+--tools=$", text)
    assert "--permission-mode default" in text
    assert "--strict-mcp-config" in text
