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
| `l` | reauthorize the selected bucket with a device code |
| `f` | toggle full-access launch |
| `slop list --quota` | print buckets and live usage |
| `slop launch ted` | switch to `ted` and exec Codex |
| `slop add allie` | log in a second account (device code) |
| `slop add allie --browser` | log in with a local browser |
| `slop use ted` | switch only |
| `slop reauth ted` | renew a login in place (device code and link) |
| `slop refresh-tokens --once` | check and renew all due accounts once |

Running Codex sessions keep their in-memory login. `/exit` and launch again after a switch.

Do **not** run `codex logout` to change accounts. Logout can revoke a saved refresh token.

## Keep accounts renewed with PM2

With SlopBucket, Codex, and PM2 installed, run from this checkout:

```bash
bash install-pm2.sh
pm2 logs slop-token-refresh
```

Configure `pm2 startup` once per server if PM2 does not already start at boot.
The service checks all saved buckets every five minutes. It asks Codex to renew
tokens at least daily, or when an access token has less than an hour remaining.
It saves rotated credentials, retries temporary failures, and leaves the active
bucket selected. Usage checks also renew due accounts. No model requests are made.

The service uses Codex's supported
[`account/read` refresh operation](https://developers.openai.com/codex/app-server#auth-endpoints).
Per-bucket locks prevent overlapping SlopBucket refresh, quota, and credential
updates. Logs contain bucket names and status, never token values. Status is saved
under `~/.codex/auth.d/.slop-status/` (or the configured `CODEX_HOME`).

Open `slop` to check your accounts. If a token is expired, revoked, or already
used and cannot be renewed, a prompt offers reauthorization. Accept it to display
the device code and sign-in link. You can also press `l`, or run `slop reauth NAME`.
The bucket keeps its name and the active selection is preserved. Cancelling or
signing into the wrong account leaves the saved login unchanged.

Renewal cannot undo provider revocation or guarantee an indefinite session.
Use independent logins on each server; copying rotating refresh tokens between
machines can invalidate an older copy. SlopBucket locks coordinate SlopBucket
processes on one host, not independent Codex processes or other machines.

Use `slop refresh-tokens --once --force` to explicitly check renewal now. It exits
nonzero if a bucket needs attention. Normal background checks stop retrying a
known invalid login until its credentials change. `--interval SECONDS` changes
the check interval (minimum 30 seconds). The PM2 process name is
`slop-token-refresh`; stop it with `pm2 stop slop-token-refresh`.

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
