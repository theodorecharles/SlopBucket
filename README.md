# slop

TUI dashboard for multiple OpenAI Codex / ChatGPT accounts.

Codex only stores one login at `~/.codex/auth.json`. slop keeps named copies, shows remaining usage, and launches Codex as the account you pick.

```
slop
```

Arrow keys move. Enter launches. `a` adds. `d` deletes. `r` refreshes usage.

## Install

Needs `gh auth login` once on the box (the package repo is private). Then:

```bash
curl -fsSL https://raw.githubusercontent.com/theodorecharles/getslop/main/install.sh | bash
```

That installs `slop` and removes any leftover `slop` alias from zsh/bash/fish startup files.

From a checkout: `./install.sh`

Needs Python 3.11+ and the Codex CLI on `PATH`.

## Usage

| Key / command | What it does |
| --- | --- |
| `slop` | open the buckets TUI |
| Enter | switch to the highlighted account and launch Codex |
| `s` | switch without launching |
| `a` | add another account (`codex login`, no logout) |
| `d` | delete a saved account |
| `n` | rename |
| `r` | refresh usage |
| `f` | toggle full-access launch |
| `slop list --quota` | print buckets and live usage |
| `slop launch ted` | switch to `ted` and exec Codex |
| `slop add allie` | log in a second account |
| `slop use ted` | switch only |

Running Codex sessions keep their in-memory login. `/exit` and launch again after a switch.

Do **not** run `codex logout` to change accounts. Logout can revoke a saved refresh token.

## Full access launch

By default slop starts Codex the same way `claude --dangerously-skip-permissions` did:

```
codex --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust
```

Config lives at `~/.config/slop/config.toml`:

```toml
[launch]
full_access = true
bypass_hook_trust = true
bin = "codex"
extra_args = []
```

Turn it off in the TUI with `f`, or:

```bash
slop config --set launch.full_access false
```

`SLOP_FULL_ACCESS=0` overrides for one process.

## How credentials are stored

- Live login: `~/.codex/auth.json` (symlink)
- Saved accounts: `~/.codex/auth.d/<name>.json`
- Codex is pinned to `cli_auth_credentials_store = "file"` so tokens are not hidden in the OS keyring

Token refresh writes through the symlink into the active bucket. Login separately on each machine; sharing one `auth.json` across boxes can invalidate refresh tokens.

## Headless add

```bash
slop add allie --device
```
