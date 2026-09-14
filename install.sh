#!/usr/bin/env bash
set -euo pipefail

REPO="${SLOP_REPO:-https://github.com/theodorecharles/slop}"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

if [[ -d .git && -f pyproject.toml && -d slop ]]; then
  uv tool install --force --editable .
else
  uv tool install --force "git+${REPO}"
fi

echo
echo "Installed slop. Run: slop"
if alias slop >/dev/null 2>&1; then
  echo "Warning: a shell alias named slop is shadowing the new command. Remove it from your zshrc/bashrc."
fi
