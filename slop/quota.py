from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from slop.store import Profile, identity_from_auth, list_profiles, profile_path


class QuotaError(Exception):
    pass


@dataclass(frozen=True)
class Window:
    label: str
    used_percent: float
    remaining_percent: float
    duration_mins: int
    resets_at: int | None

    @property
    def empty(self) -> bool:
        return self.remaining_percent <= 0


@dataclass
class Quota:
    name: str
    ok: bool
    error: str | None = None
    email: str | None = None
    plan: str | None = None
    ordinary_allowed: bool | None = None
    rate_limit_reached: bool = False
    windows: list[Window] = field(default_factory=list)
    extra: list[tuple[str, list[Window]]] = field(default_factory=list)
    credits: float | None = None
    fetched_at: float = 0.0

    @property
    def blocked(self) -> bool:
        if self.ordinary_allowed is False:
            return True
        return self.rate_limit_reached


def window_label(mins: int) -> str:
    if mins == 300:
        return "5h"
    if mins == 10080:
        return "7d"
    if mins % 1440 == 0:
        days = mins // 1440
        return f"{days}d"
    if mins % 60 == 0:
        hours = mins // 60
        return f"{hours}h"
    return f"{mins}m"


def until_label(resets_at: int | None, *, now: float | None = None) -> str:
    if not resets_at:
        return ""
    now = time.time() if now is None else now
    secs = int(resets_at - now)
    if secs <= 0:
        return "now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 48:
        rem = mins % 60
        return f"{hours}h" if rem == 0 else f"{hours}h {rem}m"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d" if rem_h == 0 else f"{days}d {rem_h}h"


def reset_clock(resets_at: int | None) -> str:
    if not resets_at:
        return ""
    dt = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone()
    return dt.strftime("%a %H:%M")


def _parse_window(raw: object) -> Window | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("usedPercent")
    mins = raw.get("windowDurationMins")
    if not isinstance(used, (int, float)) or not isinstance(mins, (int, float)):
        return None
    used_f = float(used)
    remaining = max(0.0, min(100.0, 100.0 - used_f))
    resets = raw.get("resetsAt")
    resets_i = int(resets) if isinstance(resets, (int, float)) else None
    return Window(
        label=window_label(int(mins)),
        used_percent=used_f,
        remaining_percent=remaining,
        duration_mins=int(mins),
        resets_at=resets_i,
    )


def _windows_from_bucket(bucket: dict) -> list[Window]:
    out: list[Window] = []
    primary = _parse_window(bucket.get("primary"))
    secondary = _parse_window(bucket.get("secondary"))
    if primary:
        out.append(primary)
    if secondary:
        out.append(secondary)
    return out


def _credits(bucket: dict) -> float | None:
    credits = bucket.get("credits")
    if not isinstance(credits, dict):
        return None
    if credits.get("hasCredits") is not True:
        return None
    balance = credits.get("balance")
    try:
        return float(balance)
    except (TypeError, ValueError):
        return None


def parse_rate_limits(name: str, account: dict | None, result: dict) -> Quota:
    email = plan = None
    if isinstance(account, dict) and account.get("type") == "chatgpt":
        email = account.get("email")
        plan = account.get("planType")

    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        fallback = result.get("rateLimits")
        buckets = {"codex": fallback} if isinstance(fallback, dict) else {}

    main = buckets.get("codex") if isinstance(buckets.get("codex"), dict) else None
    if main is None:
        for value in buckets.values():
            if isinstance(value, dict):
                main = value
                break
    if not isinstance(main, dict):
        main = result.get("rateLimits") if isinstance(result.get("rateLimits"), dict) else {}

    extra: list[tuple[str, list[Window]]] = []
    for key, bucket in buckets.items():
        if key == "codex" or not isinstance(bucket, dict):
            continue
        label = bucket.get("limitName") or key
        extra.append((str(label), _windows_from_bucket(bucket)))

    reached = main.get("rateLimitReachedType") if isinstance(main, dict) else None
    return Quota(
        name=name,
        ok=True,
        email=email,
        plan=str(plan) if plan else None,
        ordinary_allowed=result.get("ordinaryUsageAllowed")
        if isinstance(result.get("ordinaryUsageAllowed"), bool)
        else None,
        rate_limit_reached=bool(reached),
        windows=_windows_from_bucket(main) if isinstance(main, dict) else [],
        extra=extra,
        credits=_credits(main) if isinstance(main, dict) else None,
        fetched_at=time.time(),
    )


def _codex_bin() -> str:
    found = shutil.which("codex")
    if not found:
        raise QuotaError("codex is not on PATH")
    return found


def _send_rpc(home: Path, timeout: float = 25.0) -> tuple[dict | None, dict | None]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    proc = subprocess.Popen(
        [_codex_bin(), "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    messages = [
        {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 0,
            "params": {"clientInfo": {"name": "slop", "version": "0.1.0"}},
        },
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "method": "account/read",
            "id": 1,
            "params": {"refreshToken": False},
        },
        {"jsonrpc": "2.0", "method": "account/rateLimits/read", "id": 2},
    ]
    replies: dict[int, dict] = {}
    deadline = time.time() + timeout
    try:
        for msg in messages:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        while time.time() < deadline and 2 not in replies:
            if proc.poll() is not None:
                break
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("id"), int):
                replies[obj["id"]] = obj
        account = (replies.get(1) or {}).get("result")
        limits = replies.get(2) or {}
        if "error" in limits:
            err = limits["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise QuotaError(msg or "rate limit read failed")
        result = limits.get("result")
        if not isinstance(result, dict):
            raise QuotaError("no rate-limit payload from Codex")
        return account if isinstance(account, dict) else None, result
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()


def fetch_quota(name: str) -> Quota:
    path = profile_path(name)
    if not path.is_file():
        return Quota(name=name, ok=False, error=f"no bucket named {name!r}")
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "slop" / "probe"
    cache.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=str(cache)))
    try:
        (home / "auth.json").symlink_to(path.resolve())
        (home / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
        account, result = _send_rpc(home)
        assert result is not None
        quota = parse_rate_limits(name, account, result)
        ident = identity_from_auth(path)
        if not quota.email:
            quota.email = ident.email
        if not quota.plan:
            quota.plan = ident.plan
        return quota
    except QuotaError as exc:
        return Quota(name=name, ok=False, error=str(exc), fetched_at=time.time())
    except Exception as exc:
        return Quota(name=name, ok=False, error=str(exc), fetched_at=time.time())
    finally:
        shutil.rmtree(home, ignore_errors=True)


def fetch_all(profiles: list[Profile] | None = None) -> dict[str, Quota]:
    profiles = profiles if profiles is not None else list_profiles()
    out: dict[str, Quota] = {}
    if not profiles:
        return out
    workers = min(4, len(profiles))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_quota, p.name): p.name for p in profiles}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                out[name] = fut.result()
            except Exception as exc:
                out[name] = Quota(name=name, ok=False, error=str(exc), fetched_at=time.time())
    return out
