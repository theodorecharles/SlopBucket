#!/usr/bin/env bash
set -euo pipefail

REPO="${SLOP_REPO:-https://github.com/theodorecharles/SlopBucket.git}"
export PATH="${HOME}/.local/bin:${PATH}"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

if [[ -d .git && -f pyproject.toml && -d slop ]]; then
  uv tool install --force --editable .
else
  uv tool install --force "git+${REPO}"
fi

python3 - <<'PY'
from pathlib import Path
import os, re

home = Path.home()
candidates = [
    home / ".zshrc",
    home / ".zshenv",
    home / ".zprofile",
    home / ".zlogin",
    home / ".bashrc",
    home / ".bash_profile",
    home / ".profile",
    home / ".config/fish/config.fish",
]
zsh_custom = os.environ.get("ZSH_CUSTOM")
candidates.append(Path(zsh_custom) if zsh_custom else home / ".oh-my-zsh/custom")
alias_re = re.compile(
    r"^[ \t]*(?:alias[ \t]+(-g[ \t]+)?slop[ \t]*=.*|alias[ \t]+slop[ \t]+['\"].*|function[ \t]+slop\b.*|slop[ \t]*\(\s*\)\s*\{.*|abbr[ \t]+(?:-a[ \t]+)?slop\b.*)$"
)
for path in candidates:
    files = [path] if path.is_file() else (sorted(path.glob("*.zsh")) + sorted(path.glob("*.sh")) if path.is_dir() else [])
    for file in files:
        original = file.read_text(encoding="utf-8", errors="replace")
        lines = original.splitlines(keepends=True)
        kept = [line for line in lines if not alias_re.match(line.rstrip("\n"))]
        if kept != lines:
            file.write_text("".join(kept), encoding="utf-8")
            print(f"Removed slop alias from {file}")

PIN_BEGIN = "# >>> slop >>>"
PIN_END = "# <<< slop <<<"
PIN = """# >>> slop >>>
unalias slop 2>/dev/null || true
unset -f slop 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
# <<< slop <<<
"""

def pin(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    if PIN_BEGIN in text:
        pre, rest = text.split(PIN_BEGIN, 1)
        rest = rest.split(PIN_END, 1)[1] if PIN_END in rest else ""
        text = pre.rstrip() + "\n"
    path.write_text(text.rstrip() + "\n\n" + PIN, encoding="utf-8")

for rc in (home / ".zshrc", home / ".zprofile", home / ".bashrc", home / ".bash_profile"):
    if rc.is_file() or rc == home / ".zshrc":
        pin(rc)
PY

unalias slop 2>/dev/null || true
unset -f slop 2>/dev/null || true
hash -r 2>/dev/null || true
export PATH="${HOME}/.local/bin:${PATH}"
echo "Installed slop -> $(command -v slop)"
