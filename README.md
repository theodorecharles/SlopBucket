# SlopBucket

TUI dashboard for multiple OpenAI Codex / ChatGPT accounts.

```bash
curl -fsSL https://raw.githubusercontent.com/theodorecharles/SlopBucket/main/install.sh | bash && slop
```

Arrow keys move. Enter launches. `a` adds an account with device-code login (works on headless boxes). `d` deletes. `r` refreshes usage.

Each account shows usage left and its banked reset count. `slop list --quota` and
`slop quota` also show banked resets; `slop list --quota --json` includes
`quota.banked_resets`. If Codex does not return a count, it displays as
`unavailable` (`null` in JSON), rather than zero.

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

Install or update SlopBucket **and** its PM2 service on Debian/Linux or macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/theodorecharles/SlopBucket/main/install.sh | bash -s -- --pm2
```

Requires Codex, Node.js/npm, curl, and tar. The installer sets up uv, a compatible
Python, and PM2 if needed. It updates only `slop-token-refresh` and preserves
other PM2 apps. Re-running the command fetches current main, including when run
from an older checkout. The service configuration is saved to
`~/.config/slop/ecosystem.config.js` (honoring `XDG_CONFIG_HOME`).

To install a local checkout instead:

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
Reauthorization matches the saved ChatGPT user and workspace IDs, allowing
email aliases or email changes on the same account. Older credentials without
a user ID fall back to matching the email address.
The dashboard closes while device login runs, then reopens after the new
credentials are saved. If login validation fails, the reason stays visible
until you press Enter; press `l` to retry. Ctrl-C cancels the login.

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
