#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PATH="${HOME}/.local/bin:${HOME}/.npm-global/bin:${PATH}"
command -v uv >/dev/null
command -v pm2 >/dev/null
command -v codex >/dev/null

# Keep the existing saved process list recoverable, including stopped apps
# absent from the current daemon. Never print process environments.
backup_dir="${HOME}/.local/state/slop/deploy-$(date +%Y%m%dT%H%M%S)-$$"
mkdir -p "$backup_dir"
pm2_dir="${PM2_HOME:-${HOME}/.pm2}"
if [[ -f "${pm2_dir}/dump.pm2" ]]; then
  cp "${pm2_dir}/dump.pm2" "${backup_dir}/dump.pm2"
fi

uv tool install --force "$repo_dir"
pm2 startOrRestart "${repo_dir}/ecosystem.config.js" --only slop-token-refresh --update-env
pm2 save

python3 - "$pm2_dir" "$backup_dir" <<'PY'
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
