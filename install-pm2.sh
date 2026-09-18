#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PATH="${HOME}/.local/bin:${HOME}/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"
command -v uv >/dev/null
command -v codex >/dev/null
command -v node >/dev/null
if ! command -v pm2 >/dev/null; then
  command -v npm >/dev/null || { echo "Install npm before installing PM2." >&2; exit 1; }
  npm install --global --prefix "${HOME}/.local" pm2
fi

# Keep the existing saved process list recoverable, including stopped apps
# absent from the current daemon. Never print process environments.
backup_dir="${HOME}/.local/state/slop/deploy-$(date +%Y%m%dT%H%M%S)-$$"
mkdir -p "$backup_dir"
pm2_dir="${PM2_HOME:-${HOME}/.pm2}"
if [[ -f "${pm2_dir}/dump.pm2" ]]; then
  cp "${pm2_dir}/dump.pm2" "${backup_dir}/dump.pm2"
fi

if [[ "${1:-}" != --skip-install ]]; then
  uv tool install --force --python '>=3.11' "$repo_dir"
fi
tool_dir="$(uv tool dir)"
tool_bin="$(uv tool dir --bin)"
export PATH="${tool_bin}:${PATH}"
export SLOP_BIN="${tool_bin}/slop"
service_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/slop"
mkdir -p "$service_dir"
cp "${repo_dir}/ecosystem.config.js" "${service_dir}/ecosystem.config.js"
pm2 startOrRestart "${service_dir}/ecosystem.config.js" --only slop-token-refresh --update-env
pm2 save

"${tool_dir}/slop/bin/python" - "$pm2_dir" "$backup_dir" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

current = Path(sys.argv[1]) / "dump.pm2"
backup = Path(sys.argv[2]) / "dump.pm2"
if backup.exists():
    saved = json.loads(current.read_text())
    names = {app.get("name") for app in saved}
    missing = [app for app in json.loads(backup.read_text()) if app.get("name") not in names]
    if missing:
        fd, temp = tempfile.mkstemp(dir=current.parent, prefix=".slop-dump-")
        with os.fdopen(fd, "w") as file:
            json.dump(saved + missing, file, indent=2)
        os.replace(temp, current)
PY

slop --version
echo "PM2 process installed: slop-token-refresh"
echo "If PM2 is not already enabled at boot, run pm2 startup and follow its instructions."
