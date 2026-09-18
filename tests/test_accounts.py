import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from slop import accounts
from slop.store import current_name
from test_store import _auth


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(accounts, "_codex_bin", lambda: "codex")
    folder = tmp_path / "auth.d"
    folder.mkdir()
    for name in ("ted", "allie"):
        (folder / f"{name}.json").write_text(json.dumps(_auth(f"{name}@example.com")))
    (tmp_path / "auth.json").symlink_to("auth.d/allie.json")
    return folder / "ted.json"


def test_reauth_replaces_only_selected_bucket(saved, monkeypatch):
    other = (saved.parent / "allie.json").read_bytes()
    def login(cmd, *, env, check):
        assert cmd == ["codex", "login", "--device-auth"]
        data = _auth("ted@example.com")
        data["tokens"]["refresh_token"] = "new-refresh"
        (Path(env["CODEX_HOME"]) / "auth.json").write_text(json.dumps(data))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(accounts.subprocess, "run", login)
    accounts.reauthorize_account("ted")
    assert current_name() == "allie"
    assert (saved.parent / "allie.json").read_bytes() == other
    assert json.loads(saved.read_text())["tokens"]["refresh_token"] == "new-refresh"
    assert saved.stat().st_mode & 0o777 == 0o600
    assert not list(saved.parent.parent.glob(".slop-login-*"))


@pytest.mark.parametrize("failure", ["cancel", "exit", "wrong-account", "missing"])
def test_failed_reauth_preserves_existing_login(saved, monkeypatch, failure):
    before = saved.read_bytes()
    def login(cmd, *, env, check):
        if failure == "cancel": raise KeyboardInterrupt
        if failure == "wrong-account":
            (Path(env["CODEX_HOME"]) / "auth.json").write_text(json.dumps(_auth("someone@example.com")))
        return SimpleNamespace(returncode=1 if failure=="exit" else 0)
    monkeypatch.setattr(accounts.subprocess, "run", login)
    with pytest.raises(accounts.AddError):
        accounts.reauthorize_account("ted")
    assert saved.read_bytes() == before
    assert current_name() == "allie"


def test_reauth_accepts_email_alias_for_same_user_and_workspace(saved, monkeypatch):
    from slop.refresh import read_status, record_status
    from slop.store import identity_from_auth
    saved.write_text(json.dumps(_auth("me@tedcharles.net", user_id="ted-user")))
    record_status("ted", "reauth_required", "Old login expired")
    def login(cmd, *, env, check):
        data = _auth("me@tedroddy.net", user_id="ted-user")
        data["tokens"]["refresh_token"] = "new-refresh"
        (Path(env["CODEX_HOME"]) / "auth.json").write_text(json.dumps(data))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(accounts.subprocess, "run", login)
    accounts.reauthorize_account("ted")
    assert identity_from_auth(saved).email == "me@tedroddy.net"
    assert json.loads(saved.read_text())["tokens"]["refresh_token"] == "new-refresh"
    assert read_status("ted")["state"] == "fresh"
    assert current_name() == "allie"


@pytest.mark.parametrize("user_id,account_id,email", [
    ("different-user", "acct", "ted@example.com"),
    ("different-user", "acct", "someone@example.com"),
    (None, "acct", "ted@example.com"),
    ("ted-user", "different-workspace", "ted@example.com"),
])
def test_reauth_rejects_different_user_or_workspace_even_with_matching_email(
    saved, monkeypatch, user_id, account_id, email
):
    saved.write_text(json.dumps(_auth("ted@example.com", user_id="ted-user")))
    before = saved.read_bytes()
    def login(cmd, *, env, check):
        data = _auth(email, user_id=user_id, account_id=account_id)
        (Path(env["CODEX_HOME"]) / "auth.json").write_text(json.dumps(data))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(accounts.subprocess, "run", login)
    with pytest.raises(accounts.AddError):
        accounts.reauthorize_account("ted")
    assert saved.read_bytes() == before
