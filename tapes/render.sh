#!/usr/bin/env bash
# Render a demo GIF from the repo root: public/tapes/render.sh demo|readback|status
# Isolation is set here, before vhs starts, so the recorded shell can never see real
# tmux sessions, Claude Code sessions, config or state, even if a tape line misfires.
set -euo pipefail
name=$1
tape="$(dirname "$0")/$name.tape"
root=$(mktemp -d)
export HOME=$root XDG_CONFIG_HOME=$root/.config XDG_STATE_HOME=$root/.local/state
export JV_TMUX_SOCKET=heardback-demo PATH="$PWD/.venv/bin:/usr/local/bin:/usr/bin:/bin"
unset CLAUDE_CONFIG_DIR TMUX TMUX_PANE
mkdir -p "$XDG_CONFIG_HOME" "$XDG_STATE_HOME/heardback/sessions"
pid=
cleanup() { tmux -L heardback-demo kill-server 2>/dev/null || true; [ -n "$pid" ] && kill "$pid" 2>/dev/null; rm -rf -- "$root"; }
trap cleanup EXIT
tmux -L heardback-demo -f /dev/null new-session -d -s demo
case $name in
  readback) tmux -L heardback-demo new-window -n webapp ;;
  status)
    # A process named "claude" so the two fake sessions count as live.
    bash -c 'printf claude > /proc/$$/comm; sleep 300; :' & pid=$!
    now=$(date -Iseconds)
    for s in "webapp working" "api waiting"; do
      set -- $s
      printf '{"session_id":"demo-%s","project":"%s","state":"%s","waiting_for":"your input","claude_pid":%s,"tmux_pane":null,"cwd":"/projects/%s","updated_at":"%s"}\n' \
        "$1" "$1" "$2" "$pid" "$1" "$now" > "$XDG_STATE_HOME/heardback/sessions/$1.json"
    done ;;
esac
vhs "$tape"
