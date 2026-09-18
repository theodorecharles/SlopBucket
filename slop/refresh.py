"""Renew saved accounts without selecting them or starting model sessions."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from slop.rpc import CodexRPC, RpcError
from slop.store import StoreError, codex_home, ensure_auth_dir, jwt_payload, list_names, profile_path

CHECK_INTERVAL = 300
MAX_AGE = 24 * 3600
EXPIRY_MARGIN = 3600


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def profile_lock(name: str, timeout: float = 60):
    # A separate inode remains stable when Codex atomically replaces credentials.
    path = profile_path(name)
    folder = ensure_auth_dir() / ".slop-locks"
    folder.mkdir(mode=0o700, exist_ok=True)
    with (folder / (path.stem + ".lock")).open("a") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RpcError("Bucket is busy; will retry")
                time.sleep(0.05)
        try:
            yield path
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def account_home(path: Path):
    # Codex writes through the link. Also handle versions that replace the link
    # itself: commit their new credentials before removing the temporary home.
    original = path.read_bytes()
    cache = codex_home() / ".slop-probes"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="account-", dir=cache) as temp:
        home = Path(temp)
        auth = home / "auth.json"
        auth.symlink_to(path.resolve())
        (home / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
        try:
            yield home
        finally:
            if auth.is_file() and not auth.is_symlink():
                updated = auth.read_bytes()
                if updated != original:
                    if not path.exists() or path.read_bytes() != original:
                        # Keep credentials recoverable if another writer won the race.
                        recovery = path.parent / f".{path.stem}.refresh-conflict.{time.time_ns()}"
                        atomic_write(recovery, updated)
                        raise RpcError("Credentials changed concurrently; retry this bucket")
                    atomic_write(path, updated)
            if path.exists():
                path.chmod(0o600)


def atomic_write(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def status_path(name: str) -> Path:
    profile_path(name)  # Validate before constructing paths.
    folder = ensure_auth_dir() / ".slop-status"
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder / f"{name}.json"


def read_status(name: str) -> dict:
    try:
        status = json.loads(status_path(name).read_text())
        if status.get("fingerprint") == fingerprint(profile_path(name)):
            return status
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def record_status(name: str, state: str, message: str) -> None:
    data = dict(name=name, state=state, message=message, checked_at=time.time(),
                fingerprint=fingerprint(profile_path(name)))
    atomic_write(status_path(name), json.dumps(data).encode())


def refresh_due(data: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    tokens = data.get("tokens") or {}
    claims = jwt_payload(str(tokens.get("access_token") or ""))
    expiry = claims.get("exp")
    if isinstance(expiry, (int, float)) and expiry <= now + EXPIRY_MARGIN:
        return True
    try:
        last = datetime.fromisoformat(data["last_refresh"].replace("Z", "+00:00"))
        if last.tzinfo is None:
            return True
        age = now - last.timestamp()
        return age < -300 or age >= MAX_AGE
    except (KeyError, TypeError, ValueError, AttributeError):
        return True


def renew_locked(name: str, path: Path, *, force: bool = False) -> str:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RpcError("Invalid saved credentials")
    tokens = data.get("tokens")
    if data.get("auth_mode") not in (None, "chatgpt") or not isinstance(tokens, dict):
        return "skipped"
    if not tokens.get("refresh_token"):
        record_status(name, "reauth_required", "No refresh token; sign in again")
        raise RpcError("No refresh token; sign in again", reauth=True)
    status = read_status(name)
    if status.get("state") == "reauth_required" and not force:
        raise RpcError(status["message"], reauth=True)
    if not force and not refresh_due(data):
        return "fresh"
    before = fingerprint(path)
    try:
        with account_home(path) as home:
            with CodexRPC(home) as rpc:
                result = rpc.call("account/read", {"refreshToken": True})
                if not result.get("account"):
                    raise rpc.renewal_error()
        if fingerprint(path) == before:
            raise RpcError("Codex did not persist renewed credentials; will retry")
        record_status(name, "fresh", "Tokens renewed")
        return "renewed"
    except RpcError as exc:
        record_status(name, "reauth_required" if exc.reauth else "retry", str(exc))
        raise


def refresh_profile(name: str, *, force: bool = False) -> dict:
    try:
        with profile_lock(name) as path:
            state = renew_locked(name, path, force=force)
        return dict(name=name, state=state)
    except RpcError as exc:
        return dict(name=name, state="reauth_required" if exc.reauth else "retry", message=str(exc))
    except (OSError, ValueError, StoreError):
        return dict(name=name, state="retry", message="Cannot read or update saved credentials")


def run_service(*, interval: float = CHECK_INTERVAL, once: bool = False, force: bool = False) -> int:
    stop = threading.Event()
    def shutdown(*_):
        stop.set()
    previous = {}
    if not once:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, shutdown)
    try:
        while not stop.is_set():
            failed = False
            for name in list_names():
                if stop.is_set():
                    break
                result = refresh_profile(name, force=force)
                failed |= result["state"] in {"retry", "reauth_required"}
                print(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **result}), flush=True)
            if once:
                return int(failed)
            stop.wait(interval)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0
