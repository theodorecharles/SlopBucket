#!/usr/bin/env bash
set -euo pipefail

REPO="${SLOP_REPO:-https://github.com/theodorecharles/SlopBucket}"

strip_slop_aliases() {
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
    home / ".kshrc",
    home / ".config/fish/config.fish",
]
zsh_custom = os.environ.get("ZSH_CUSTOM")
if zsh_custom:
    candidates.append(Path(zsh_custom))
else:
    candidates.append(home / ".oh-my-zsh/custom")

alias_re = re.compile(
    r"^[ \t]*(?:alias[ \t]+slop[ \t]*=.*|alias[ \t]+slop[ \t]+['\"].*|abbr[ \t]+(?:-a[ \t]+)?slop\b.*)$"
)

changed = []
for path in candidates:
    files = []
    if path.is_dir():
        files.extend(sorted(path.glob("*.zsh")))
        files.extend(sorted(path.glob("*.sh")))
    elif path.is_file():
        files.append(path)
    for file in files:
        original = file.read_text(encoding="utf-8", errors="replace")
        lines = original.splitlines(keepends=True)
        kept = [line for line in lines if not alias_re.match(line.rstrip("\n"))]
        if kept == lines:
            continue
        file.write_text("".join(kept), encoding="utf-8")
        changed.append(str(file))

if changed:
    print("Removed slop alias from:")
    for path in changed:
        print(f"  {path}")
else:
    print("No slop alias found in shell startup files.")

PIN_BEGIN = "# >>> slop >>>"
PIN_END = "# <<< slop <<<"
PIN = """# >>> slop >>>
# SlopBucket CLI. Kill leftover aliases/functions so `slop` is this tool.
unalias slop 2>/dev/null || true
unset -f slop 2>/dev/null || true
# <<< slop <<<
"""

def pin(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    if PIN_BEGIN in text:
        pre, rest = text.split(PIN_BEGIN, 1)
        if PIN_END in rest:
            rest = rest.split(PIN_END, 1)[1]
        else:
            rest = ""
        text = pre.rstrip() + "\n"
    text = text.rstrip() + "\n\n" + PIN
    path.write_text(text, encoding="utf-8")
    print(f"Pinned unalias slop at end of {path}")

for rc in (home / ".zshrc", home / ".zprofile", home / ".bashrc", home / ".bash_profile"):
    if rc.is_file() or rc == home / ".zshrc":
        pin(rc)
PY
}

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

export PATH="${HOME}/.local/bin:${PATH}"

if [[ -d .git && -f pyproject.toml && -d slop ]]; then
  uv tool install --force --editable .
else
  if ! command -v gh >/dev/null 2>&1; then
    echo "slop: GitHub CLI is required. Install it, then: gh auth login" >&2
    exit 1
  fi
  if ! gh auth status >/dev/null 2>&1; then
    echo "slop: run: gh auth login" >&2
    exit 1
  fi
  gh auth setup-git >/dev/null 2>&1 || true
  git_url="$REPO"
  [[ "$git_url" == *.git ]] || git_url="${git_url}.git"
  uv tool install --force "git+${git_url}"
fi

echo
strip_slop_aliases
unalias slop 2>/dev/null || true
hash -r 2>/dev/null || true

echo
echo "Installed slop. Open a new terminal (or run: unalias slop) then: slop"
if type slop >/dev/null 2>&1; then
  echo "Current shell resolves slop as: $(type slop | head -1)"
fi
