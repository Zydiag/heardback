#!/bin/sh
# Full suite, isolated from the live desktop: tmux shim, no GPU, scratch XDG dirs.
cd "$(dirname "$0")/.." || exit 1
s=$(mktemp -d)
trap 'rm -rf "$s"' EXIT
PATH="$PWD/scripts/test-shim:$PATH" TMUX= CUDA_VISIBLE_DEVICES= \
XDG_STATE_HOME="$s/state" XDG_CONFIG_HOME="$s/config" XDG_DATA_HOME="$s/data" \
  uv run pytest -q -p no:cacheprovider "$@"
