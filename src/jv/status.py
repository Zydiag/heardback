"""Claude Code session status: verbs live in heardback.status.cli."""
from heardback.status.cli import VERBS  # noqa: F401
from heardback.status.store import list_sessions, user_sessions


def load_sessions(*, include_ended: bool = False, include_internal: bool = False,
                  create_state_dir: bool = True) -> list[dict]:
    # No tmux lookups here: tmux verbs need stable pane IDs and live user sessions.
    sessions = list_sessions(resolve=False, create_state_dir=create_state_dir)
    if include_internal:
        return sessions if include_ended else [s for s in sessions if s.get("state") != "ended"]
    return user_sessions(sessions, include_ended=include_ended)
