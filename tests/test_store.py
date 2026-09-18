import json

from slop.store import (
    current_name,
    ensure_file_store,
    identity_from_auth,
    list_names,
    save_current,
    suggest_name,
    switch_to,
    Identity,
)


def _auth(email: str, plan: str = "pro", *, user_id: str | None = None, account_id: str = "acct") -> dict:
    # Minimal unsigned JWT-shaped payload (header.payload.sig) for identity parsing.
    import base64

    def enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = enc({"alg": "none", "typ": "JWT"})
    payload = enc(
        {
            "email": email,
            "name": "Ted Roddy",
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": plan,
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": user_id,
            },
        }
    )
    token = f"{header}.{payload}.sig"
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": token,
            "id_token": token,
            "refresh_token": "refresh",
            "account_id": account_id,
        },
    }


def test_save_and_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps(_auth("me@tedcharles.net")))
    ensure_file_store()
    save_current("ted")
    assert (tmp_path / "auth.d" / "ted.json").is_file()
    assert (tmp_path / "auth.json").is_symlink()
    assert current_name() == "ted"

    (tmp_path / "auth.d" / "allie.json").write_text(json.dumps(_auth("allie@example.com")))
    switch_to("allie")
    assert current_name() == "allie"
    assert list_names() == ["allie", "ted"]
    ident = identity_from_auth(tmp_path / "auth.d" / "ted.json")
    assert ident.email == "me@tedcharles.net"
    assert ident.plan == "pro"
    assert suggest_name(ident) == "ted"


def test_suggest_name_fallback():
    assert suggest_name(Identity(email="me@x.com")) == "account"
    assert suggest_name(Identity(email="allie@x.com")) == "allie"


def test_identity_includes_stable_user_and_workspace_ids(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(_auth("old@example.com", user_id="user-123", account_id="workspace-456")))
    ident = identity_from_auth(path)
    assert ident.user_id == "user-123"
    assert ident.account_id == "workspace-456"


def test_file_store_written(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    ensure_file_store()
    text = (tmp_path / "config.toml").read_text()
    assert 'cli_auth_credentials_store = "file"' in text
