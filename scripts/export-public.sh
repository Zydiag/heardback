#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 ]]; then
  echo "usage: $0 <dest>" >&2
  exit 2
fi

dest=$1
if [[ -e $dest ]]; then
  echo "destination already exists: $dest" >&2
  exit 1
fi

denylist="$HOME/.config/heardback/export-denylist.txt"
if [[ ! -f $denylist ]]; then
  echo "missing private export denylist: $denylist" >&2
  exit 1
fi

mapfile -t paths < <(git ls-tree -r --name-only HEAD | while IFS= read -r path; do
  case "$path" in
    src/*|tests/*|extension/*|systemd/*|examples/config.toml|pyproject.toml|uv.lock|scripts/install-snapshot.sh|scripts/export-public.sh|scripts/check-clean-install.sh|scripts/gen-commands-doc.py|scripts/test-shim/*|scripts/test-full.sh|public/*) echo "$path" ;;
  esac
done)

mkdir -p -- "$dest"
cleanup() {
  status=$?
  if (( status != 0 )); then rm -rf -- "$dest"; fi
  exit "$status"
}
trap cleanup EXIT

git archive HEAD "${paths[@]}" | tar -x -C "$dest"
if [[ -d $dest/public ]]; then
  cp -a "$dest/public/." "$dest/"
  rm -rf -- "$dest/public"
fi

if ! gitleaks dir --redact "$dest" >/dev/null 2>&1; then
  echo "gitleaks found a possible secret" >&2
  exit 1
fi
if rg --hidden --no-ignore -i -f "$denylist" "$dest" >/dev/null 2>&1; then
  echo "private denylist matched exported content" >&2
  exit 1
fi
if find "$dest" -type f -size +1000000c -print -quit | rg -q .; then
  echo "export contains a file over 1 MB" >&2
  exit 1
fi
if find "$dest" -type f \( -name .env -o -name '*.sqlite3' -o -name '*.jsonl' \) -print -quit | rg -q .; then
  echo "export contains a prohibited data file" >&2
  exit 1
fi

trap - EXIT
echo "public export created at $dest"
