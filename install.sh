#!/usr/bin/env bash
# Install SlopBucket. Must be sourced so it can unalias `slop` in THIS shell:
#   source <(curl -fsSL https://raw.githubusercontent.com/theodorecharles/getslop/main/install.sh)

REPO="${SLOP_REPO:-https://github.com/theodorecharles/SlopBucket}"

_slop_sourced=0
if [ -n "${ZSH_VERSION:-}" ]; then
  case "${ZSH_EVAL_CONTEXT:-}" in *:file*|*:source*|*:eval*) _slop_sourced=1 ;; esac
elif [ -n "${BASH_VERSION:-}" ]; then
  if [ "${BASH_SOURCE[0]:-}" != "${0:-}" ]; then
    _slop_sourced=1
  fi
fi

_slop_install() {
  export PATH="${HOME}/.local/bin:${PATH}"

  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh || return 1
    export PATH="${HOME}/.local/bin:${PATH}"
  fi

  if [ -d .git ] && [ -f pyproject.toml ] && [ -d slop ]; then
    uv tool install --force --editable . || return 1
  else
    if ! command -v gh >/dev/null 2>&1; then
      echo "slop: GitHub CLI is required. Install gh, then: gh auth login" >&2
      return 1
    fi
    if ! gh auth status >/dev/null 2>&1; then
      echo "slop: run: gh auth login" >&2
      return 1
    fi
    gh auth setup-git >/dev/null 2>&1 || true
    git_url="$REPO"
    case "$git_url" in
      *.git) ;;
      *) git_url="${git_url}.git" ;;
    esac
    uv tool install --force "git+${git_url}" || return 1
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
    home / ".kshrc",
    home / ".config/fish/config.fish",
]
zsh_custom = os.environ.get("ZSH_CUSTOM")
candidates.append(Path(zsh_custom) if zsh_custom else home / ".oh-my-zsh/custom")

alias_re = re.compile(
    r"^[ \t]*(?:alias[ \t]+(-g[ \t]+)?slop[ \t]*=.*|alias[ \t]+slop[ \t]+['\"].*|function[ \t]+slop\b.*|slop[ \t]*\(\s*\)\s*\{.*|abbr[ \t]+(?:-a[ \t]+)?slop\b.*)$"
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
    print("Removed slop alias/function from:")
    for path in changed:
        print(f"  {path}")

PIN_BEGIN = "# >>> slop >>>"
PIN_END = "# <<< slop <<<"
PIN = """# >>> slop >>>
# SlopBucket CLI. Kill leftover aliases/functions so `slop` is this tool.
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
    print(f"Pinned slop on PATH at end of {path}")

for rc in (home / ".zshrc", home / ".zprofile", home / ".bashrc", home / ".bash_profile"):
    if rc.is_file() or rc == home / ".zshrc":
        pin(rc)
PY

  unalias slop 2>/dev/null || true
  unalias -m slop 2>/dev/null || true
  unset -f slop 2>/dev/null || true
  rehash 2>/dev/null || hash -r 2>/dev/null || true
  export PATH="${HOME}/.local/bin:${PATH}"

  echo
  echo "Installed slop. Run: slop"
  command -v slop >/dev/null 2>&1 && echo "resolves to: $(command -v slop)"
  return 0
}

_slop_install
_slop_status=$?
unset -f _slop_install 2>/dev/null || true

if [ "$_slop_sourced" -eq 1 ]; then
  return "$_slop_status" 2>/dev/null || true
fi
exit "$_slop_status"
