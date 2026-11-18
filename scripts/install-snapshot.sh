#!/usr/bin/env bash
# Install a snapshot of `main` (or $1) as the uv tool the service runs (~/.local/bin/heardbackd, jv, ...).
# No restart: run `systemctl --user restart heardback` yourself afterwards.
# Dry run: UV_TOOL_DIR=$(mktemp -d) UV_TOOL_BIN_DIR=$(mktemp -d) scripts/install-snapshot.sh
set -euo pipefail
ref=${1:-main}
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
commit=$(git -C "$repo" rev-parse --short "$ref")
dir=$(mktemp -d -t heardback-snapshot.XXXXXX)
trap 'rm -rf "$dir"' EXIT
git -C "$repo" archive "$ref" | tar -x -C "$dir"
# pin to uv.lock (uv tool install ignores the lockfile otherwise)
(cd "$dir" && uv export --frozen --no-hashes --extra voice --no-dev --no-emit-project -o pins.txt -q)
uv tool install --force -c "$dir/pins.txt" "${dir}[voice]"
py="$(uv tool dir)/heardback/bin/python"
"$py" -c 'import faster_whisper, kokoro, silero_vad, torch; print("voice extra ok, cuda:", torch.cuda.is_available())'
echo "installed heardback $commit ($ref) into $(uv tool dir)/heardback"
