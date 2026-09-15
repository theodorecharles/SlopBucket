from __future__ import annotations

import os
import shutil
import subprocess
import time

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
    found = shutil.which("codex")
    if not found:
        raise AddError("codex is not on PATH")
    return found


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
