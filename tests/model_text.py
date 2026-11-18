"""Everything Claude can read as policy: the system prompt plus every verb's description (E2: rules live there)."""
from heardback.brain.claude_proc import PROMPT
from jv.cli import all_verbs, catalogue


def model_text() -> str:
    return "\n".join([PROMPT.read_text(), *(v["description"] for v in catalogue(all_verbs())["verbs"])])


def description(verb: str) -> str:
    return next(v["description"] for v in catalogue(all_verbs())["verbs"] if v["name"] == verb)
