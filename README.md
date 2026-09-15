# SlopBucket

TUI dashboard for multiple OpenAI Codex / ChatGPT accounts.

```bash
curl -fsSL https://raw.githubusercontent.com/theodorecharles/SlopBucket/main/install.sh | bash && slop
```

Arrow keys move. Enter launches. `a` adds an account with device-code login (works on headless boxes). `d` deletes. `r` refreshes usage.

## Usage

| Key / command | What it does |
| --- | --- |
| `slop` | open the buckets TUI |
| Enter | switch to the highlighted account and launch Codex |
| `s` | switch without launching |
| `a` | add another account (device code) |
| `d` | delete a saved account |
| `n` | rename |
| `r` | refresh usage |
| `f` | toggle full-access launch |
| `slop list --quota` | print buckets and live usage |
| `slop launch ted` | switch to `ted` and exec Codex |
| `slop add allie` | log in a second account (device code) |
| `slop add allie --browser` | log in with a local browser |
| `slop use ted` | switch only |

Running Codex sessions keep their in-memory login. `/exit` and launch again after a switch.

Do **not** run `codex logout` to change accounts. Logout can revoke a saved refresh token.

## Full access launch

By default slop starts Codex with:

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

## How credentials are stored

- Live login: `~/.codex/auth.json` (symlink)
- Saved accounts: `~/.codex/auth.d/<name>.json`
- Codex is pinned to `cli_auth_credentials_store = "file"`
