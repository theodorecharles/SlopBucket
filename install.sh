#!/usr/bin/env bash
set -euo pipefail

REPO="${SLOP_REPO:-https://github.com/theodorecharles/SlopBucket.git}"
export PATH="${HOME}/.local/bin:${HOME}/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"
with_pm2=false
for arg in "$@"; do
  case "$arg" in
    --pm2) with_pm2=true ;;
    --help|-h)
      echo "Usage: bash install.sh [--pm2]"
      echo "Install/update SlopBucket; --pm2 also installs/updates its background service."
      echo "The service requires Codex and Node.js/npm on PATH."
      exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if "$with_pm2"; then
  command -v codex >/dev/null || { echo "Install Codex before installing the refresh service." >&2; exit 1; }
  command -v node >/dev/null || { echo "Install Node.js/npm before installing the refresh service." >&2; exit 1; }
  if ! command -v pm2 >/dev/null; then
    command -v npm >/dev/null || { echo "Install npm before installing PM2." >&2; exit 1; }
  fi
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

# File invocation installs that checkout; piping this script always downloads
# current main, even if the caller happens to be in an older SlopBucket checkout.
source_dir="${SLOP_SOURCE_DIR:-}"
if [[ -z "$source_dir" && -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "${script_dir}/pyproject.toml" && -d "${script_dir}/slop" ]]; then
    source_dir="$script_dir"
  fi
fi
if [[ -z "$source_dir" ]]; then
  install_tmp="$(mktemp -d "${TMPDIR:-/tmp}/slop-install.XXXXXX")"
  trap 'rm -rf -- "$install_tmp"' EXIT
  archive_url="${SLOP_ARCHIVE_URL:-${REPO%.git}/archive/refs/heads/main.tar.gz}"
  curl -fLsS --retry 3 "$archive_url" -o "${install_tmp}/source.tar.gz"
  mkdir "${install_tmp}/source"
  tar -xzf "${install_tmp}/source.tar.gz" --strip-components=1 -C "${install_tmp}/source"
  source_dir="${install_tmp}/source"
fi
uv tool install --force --python '>=3.11' "$source_dir"
tool_dir="$(uv tool dir)"
tool_bin="$(uv tool dir --bin)"
export PATH="${tool_bin}:${PATH}"

"${tool_dir}/slop/bin/python" - <<'PY'
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
    r"^[ \t]*(?:alias[ \t]+(-g[ \t]+)?slop[ \t]*=.*|alias[ \t]+slop[ \t]+['\"].*|abbr[ \t]+(?:-a[ \t]+)?slop\b.*)$"
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
        text = re.sub(r"(?ms)^" + re.escape(PIN_BEGIN) + r"\n.*?^" + re.escape(PIN_END) + r"(?:\n|$)", "", text)
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
slop --version
if "$with_pm2"; then
  bash "${source_dir}/install-pm2.sh" --skip-install
fi
