from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STORE_LINE = 'cli_auth_credentials_store = "file"'


class StoreError(Exception):
    pass


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    return Path(override).expanduser() if override else Path.home() / ".codex"


def auth_path() -> Path:
    return codex_home() / "auth.json"


def auth_dir() -> Path:
    return codex_home() / "auth.d"


def config_toml_path() -> Path:
    return codex_home() / "config.toml"


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise StoreError(
            f"invalid bucket name {name!r} (use letters, numbers, '.', '_', or '-', starting with a letter or number)"
        )
    return name


def profile_path(name: str) -> Path:
    return auth_dir() / f"{validate_name(name)}.json"


def ensure_auth_dir() -> Path:
    path = auth_dir()
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def ensure_file_store() -> None:
    """Force Codex to keep credentials in auth.json so profiles can be swapped."""
    path = config_toml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(STORE_LINE + "\n")
        return
    text = path.read_text()
    updated, n = re.subn(
        r"(?m)^\s*cli_auth_credentials_store\s*=\s*.*$",
        STORE_LINE,
        text,
    )
    if n == 0:
        updated = STORE_LINE + "\n" + text
    if updated != text:
        path.write_text(updated)


@dataclass(frozen=True)
class Identity:
    email: str | None = None
    plan: str | None = None
    name: str | None = None
    account_id: str | None = None


@dataclass(frozen=True)
class Profile:
    name: str
    path: Path
    identity: Identity
    active: bool


def _b64url(data: str) -> dict:
    pad = "=" * ((4 - len(data) % 4) % 4)
    raw = base64.urlsafe_b64decode(data + pad)
    obj = json.loads(raw)
    return obj if isinstance(obj, dict) else {}


def jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        return _b64url(parts[1])
    except Exception:
        return {}


def identity_from_auth(path: Path) -> Identity:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return Identity()
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return Identity()
    email = plan = display = account_id = None
    for key in ("id_token", "access_token"):
        payload = jwt_payload(str(tokens.get(key) or ""))
        if not payload:
            continue
        email = email or payload.get("email")
        display = display or payload.get("name")
        auth = payload.get("https://api.openai.com/auth")
        profile = payload.get("https://api.openai.com/profile")
        if isinstance(profile, dict):
            email = email or profile.get("email")
            display = display or profile.get("name")
        if isinstance(auth, dict):
            plan = plan or auth.get("chatgpt_plan_type")
            account_id = account_id or auth.get("chatgpt_account_id")
    token_account = tokens.get("account_id")
    if isinstance(token_account, str) and not account_id:
        account_id = token_account
    return Identity(
        email=str(email) if email else None,
        plan=str(plan) if plan else None,
        name=str(display) if display else None,
        account_id=str(account_id) if account_id else None,
    )


def suggest_name(ident: Identity) -> str:
    if ident.name:
        slug = re.sub(r"[^a-z0-9]+", "", ident.name.split()[0].lower())
        if slug:
            return slug
    if ident.email:
        local = ident.email.split("@", 1)[0]
        slug = re.sub(r"[^a-z0-9]+", "", local.lower())
        if slug and slug not in {"me", "user", "admin", "mail"}:
            return slug
    return "account"


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve()
    except Exception:
        return None


def current_name() -> str | None:
    active = auth_path()
    if not active.exists() and not active.is_symlink():
        return None
    active_resolved = _resolved(active)
    if active_resolved is None:
        return None
    for profile in list_names():
        if _resolved(profile_path(profile)) == active_resolved:
            return profile
    return None


def list_names() -> list[str]:
    folder = auth_dir()
    if not folder.is_dir():
        return []
    names = []
    for path in sorted(folder.glob("*.json")):
        if path.name.startswith("."):
            continue
        names.append(path.stem)
    return names


def list_profiles() -> list[Profile]:
    active = current_name()
    profiles = []
    for name in list_names():
        path = profile_path(name)
        profiles.append(
            Profile(
                name=name,
                path=path,
                identity=identity_from_auth(path),
                active=name == active,
            )
        )
    return profiles


def auth_is_managed() -> bool:
    path = auth_path()
    return path.is_symlink()


def auth_exists() -> bool:
    path = auth_path()
    return path.exists() or path.is_symlink()


def unmanaged_auth_exists() -> bool:
    path = auth_path()
    return path.is_file() and not path.is_symlink()


def _atomic_symlink(rel_target: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.symlink_to(rel_target)
    tmp.replace(dest)


def switch_to(name: str, *, replace_unmanaged: bool = False) -> None:
    dest = profile_path(name)
    if not dest.is_file():
        raise StoreError(f"no bucket named {name!r}")
    active = auth_path()
    if active.exists() and not active.is_symlink() and not replace_unmanaged:
        raise StoreError(
            f"{active} is an unmanaged regular file; save it as a bucket first so it is not overwritten"
        )
    _atomic_symlink(f"auth.d/{name}.json", active)


def _write_profile_bytes(name: str, data: bytes) -> Path:
    from slop.refresh import atomic_write
    ensure_auth_dir()
    dest = profile_path(name)
    atomic_write(dest, data)
    return dest


def save_current(name: str, *, overwrite: bool = False) -> Profile:
    """Snapshot the live auth.json into a named bucket and point auth.json at it."""
    from slop.refresh import profile_lock
    with profile_lock(name):
        validate_name(name)
        active = auth_path()
        if not active.exists():
            raise StoreError("no current Codex login; run `codex login` first")
        dest = profile_path(name)
        if dest.exists() and not overwrite:
            raise StoreError(f"bucket {name!r} already exists")
        data = active.read_bytes()
        _write_profile_bytes(name, data)
        switch_to(name, replace_unmanaged=True)
        return next(p for p in list_profiles() if p.name == name)


def import_unmanaged(name: str) -> Profile:
    if not unmanaged_auth_exists():
        raise StoreError("current auth.json is already managed or missing")
    return save_current(name, overwrite=False)


def remove(name: str) -> None:
    from slop.refresh import profile_lock
    with profile_lock(name):
        dest = profile_path(name)
        if not dest.is_file():
            raise StoreError(f"no bucket named {name!r}")
        if current_name() == name:
            raise StoreError(f"refusing to remove the active bucket {name!r}; switch first")
        dest.unlink()


def rename(old: str, new: str) -> None:
    from slop.refresh import profile_lock
    first, second = sorted((old, new))
    if old == new:
        raise StoreError("new bucket name must be different")
    with profile_lock(first), profile_lock(second):
        validate_name(new)
        src = profile_path(old)
        dest = profile_path(new)
        if not src.is_file():
            raise StoreError(f"no bucket named {old!r}")
        if dest.exists():
            raise StoreError(f"bucket {new!r} already exists")
        was_current = current_name() == old
        src.rename(dest)
        dest.chmod(0o600)
        if was_current:
            switch_to(new)
