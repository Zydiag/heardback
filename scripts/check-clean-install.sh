#!/usr/bin/env bash
# Build only the scrubbed HEAD export; all installs and desktop probes stay in the container.
set -euo pipefail
cd "$(dirname "$0")/.."
scratch=$(mktemp -d -t heardback-clean.XXXXXX)
image="heardback-clean-install:check-$$"
cleanup() {
  docker image rm -f "$image" >/dev/null 2>&1 || true
  rm -rf -- "$scratch"
}
trap cleanup EXIT
scripts/export-public.sh "$scratch/export"
cat > "$scratch/Dockerfile" <<'DOCKER'
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tmux && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir uv
ENV UV_TOOL_BIN_DIR=/usr/local/bin UV_TOOL_DIR=/opt/uv-tools UV_OFFLINE=0
COPY export /src
COPY check.py /check.py
WORKDIR /src
RUN uv tool install .
CMD ["python", "/check.py"]
DOCKER
cat > "$scratch/check.py" <<'PY'
import json
import os
import subprocess

subprocess.run(["jv", "--help"], check=True)
subprocess.run(["/opt/uv-tools/heardback/bin/python", "-c",
                "from importlib.metadata import distributions; "
                "assert {d.metadata['Name'] for d in distributions()} == {'heardback'}"], check=True)
env = {**os.environ, "JV_TMUX_SOCKET": "heardback-clean"}
subprocess.run(["tmux", "-L", "heardback-clean", "-f", "/dev/null", "new-session", "-d", "-s", "clean"], check=True)
try:
    result = subprocess.run(["jv", "tmux", "list", "--json"], env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["ok"] is True, result.stdout
    print(result.stdout, end="")
finally:
    subprocess.run(["tmux", "-L", "heardback-clean", "kill-server"], check=True)
doctor = subprocess.run(["heardback", "doctor"], capture_output=True, text=True)
print(doctor.stdout, end="")
assert doctor.returncode == 1 and not doctor.stderr, (doctor.returncode, doctor.stderr)
lines = doctor.stdout.splitlines()
assert {line.split()[1].rstrip(":") for line in lines if line.startswith("missing ")} == {
    "pipewire", "claude", "claude-login"}, doctor.stdout
for name in ("python", "tmux", "config", "wake"):
    assert any(line.startswith(f"ok {name}:") for line in lines), doctor.stdout
assert any(line.startswith("warn voice:") for line in lines), doctor.stdout
assert all("; fix: " in line for line in lines if not line.startswith("ok ")), doctor.stdout
assert all(line.startswith(("ok ", "missing ", "warn ")) for line in lines), doctor.stdout
print("clean install passed (PipeWire/Claude intentionally absent)")
PY
docker build -t "$image" "$scratch"
docker run --rm "$image"
