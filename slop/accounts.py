from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from slop.store import (
    StoreError,
    auth_path,
    current_name,
    ensure_file_store,
    list_names,
    save_current,
    switch_to,
    unmanaged_auth_exists,
)


class AddError(StoreError):
    pass


def _codex_bin() -> str:
    from slop.config import load
    found = shutil.which(load().launch.bin)
    if not found:
        raise AddError("codex is not on PATH")
    return found


def reauthorize_account(name: str, *, device: bool = True) -> None:
    """Replace only this bucket after successful login, preserving selection.

    Login uses a separate Codex home. Cancellation, failed login, and signing
    into the wrong account leave all existing credentials untouched.
    """
    from slop.refresh import atomic_write, profile_lock, record_status
    from slop.store import codex_home, identity_from_auth, profile_path

    path = profile_path(name)
    if not path.is_file():
        raise AddError(f"no bucket named {name!r}")
    expected = identity_from_auth(path)
    binary = _codex_bin()
    with tempfile.TemporaryDirectory(prefix=".slop-login-", dir=codex_home()) as temp:
        home = Path(temp)
        (home / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        cmd = [binary, "login"]
        if device:
            cmd.append("--device-auth")
        print(f"\nReauthorize {name}" + (f" ({expected.email})" if expected.email else "") +
              ". Sign in using the link and code below.\n", flush=True)
        try:
            completed = subprocess.run(cmd, env=env, check=False)
        except KeyboardInterrupt as exc:
            raise AddError("login cancelled; bucket unchanged") from exc
        except OSError as exc:
            raise AddError("could not start Codex login; bucket unchanged") from exc
        if completed.returncode != 0:
            raise AddError(f"login exited {completed.returncode}; bucket unchanged")
        fresh = home / "auth.json"
        if not fresh.is_file():
            raise AddError("login did not save credentials; bucket unchanged")
        actual = identity_from_auth(fresh)
        import json
        try:
            data = json.loads(fresh.read_text())
            tokens = data.get("tokens") or {}
            if not all(tokens.get(key) for key in ("access_token", "refresh_token", "id_token")):
                raise ValueError("missing tokens")
        except (ValueError, AttributeError) as exc:
            raise AddError("login returned incomplete credentials; bucket unchanged") from exc
        # Email may change or have aliases. A workspace ID alone is not enough:
        # multiple users can belong to the same workspace. Prefer the stable
        # ChatGPT user ID, with email only as a fallback for older credentials.
        same_user = (
            actual.user_id == expected.user_id
            if expected.user_id
            else not expected.email or (actual.email or "").lower() == expected.email.lower()
        )
        if not same_user:
            raise AddError(
                f"signed into a different account ({actual.email or 'unknown'}); "
                f"bucket {name!r} needs its original ChatGPT user"
                + (f" ({expected.email})" if expected.email else "")
                + ". Bucket unchanged."
            )
        if expected.account_id and actual.account_id != expected.account_id:
            raise AddError(
                "signed into a different account or workspace; "
                f"choose the original workspace for bucket {name!r}. Bucket unchanged."
            )
        with profile_lock(name):
            if not path.is_file() or identity_from_auth(path) != expected:
                raise AddError("bucket changed during login; please retry")
            atomic_write(path, fresh.read_bytes())
            record_status(name, "fresh", "Account reauthorized")


def add_account(name: str, *, device: bool = True) -> None:
    """Log into a new Codex account without logging out of saved buckets.

    Temporarily unlinks the active auth.json, runs `codex login --device-auth`
    (or browser login), then stores the new credentials as a named profile.
    """
    from slop.store import validate_name

    validate_name(name)
    if name in list_names():
        raise AddError(f"bucket {name!r} already exists")
    ensure_file_store()
    active = auth_path()
    restore_rel: str | None = None
    if active.exists() or active.is_symlink():
        if unmanaged_auth_exists():
            raise AddError(
                "current auth.json is unmanaged; save it as a bucket before adding another"
            )
        current = current_name()
        if current is None:
            raise AddError("auth.json is a symlink, but not a slop bucket")
        restore_rel = f"auth.d/{current}.json"
        active.unlink()

    cmd = [_codex_bin(), "login"]
    if device:
        cmd.append("--device-auth")
    print(
        "\nSign in to the NEW Codex account"
        + (" with the device code below." if device else " in the browser.")
        + "\nDo not run `codex logout` — that can revoke a saved bucket.\n",
        flush=True,
    )
    try:
        completed = subprocess.run(cmd, check=False)
    except KeyboardInterrupt as exc:
        _restore(restore_rel)
        raise AddError("login cancelled") from exc
    if completed.returncode != 0:
        _restore(restore_rel)
        raise AddError(f"codex login exited {completed.returncode}")
    if not active.is_file() or active.is_symlink():
        _restore(restore_rel)
        raise AddError("codex login did not write a new auth.json")
    try:
        save_current(name, overwrite=False)
    except Exception:
        failed = active.with_name(f"auth.json.add-failed.{os.getpid()}.json")
        try:
            active.replace(failed)
        except Exception:
            pass
        _restore(restore_rel)
        raise
    switch_to(name)


def _restore(rel: str | None) -> None:
    if not rel:
        return
    active = auth_path()
    if active.exists() or active.is_symlink():
        return
    tmp = active.with_name(f".auth.json.restore.{time.time_ns()}.tmp")
    tmp.symlink_to(rel)
    tmp.replace(active)
