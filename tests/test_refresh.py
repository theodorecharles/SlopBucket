import base64
import json
import os
import time
from datetime import datetime, timezone

import pytest

from slop import refresh
from slop.rpc import RpcError


def auth(now, *, age=0, expires=86400):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": now + expires}).encode()).decode().rstrip("=")
    return {"auth_mode": "chatgpt", "last_refresh": datetime.fromtimestamp(now-age, timezone.utc).isoformat(),
            "tokens": {"access_token": f"h.{payload}.s", "refresh_token": "old", "id_token": "id"}}


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    folder = tmp_path / "auth.d"
    folder.mkdir()
    path = folder / "test.json"
    path.write_text(json.dumps(auth(time.time(), age=86401)))
    (tmp_path / "auth.json").symlink_to("auth.d/test.json")
    return path


def test_schedule_renews_daily_or_before_access_expiry():
    now = int(time.time())
    assert not refresh.refresh_due(auth(now), now)
    assert refresh.refresh_due(auth(now, age=86400), now)
    assert refresh.refresh_due(auth(now, expires=3599), now)
    assert refresh.refresh_due({"tokens": {}}, now)


@pytest.mark.parametrize("replace_link", [True, False])
def test_rotated_credentials_survive_probe_cleanup(bucket, monkeypatch, replace_link):
    class RPC:
        def __init__(self, home): self.home = home
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def call(self, method, params):
            assert method == "account/read" and params == {"refreshToken": True}
            target = self.home / "auth.json"
            data = json.loads(target.read_text())
            data["tokens"]["refresh_token"] = "rotated"
            if replace_link:
                target.unlink()
            target.write_text(json.dumps(data))
            return {"account": {"type": "chatgpt"}}
    monkeypatch.setattr(refresh, "CodexRPC", RPC)
    assert refresh.refresh_profile("test")["state"] == "renewed"
    assert json.loads(bucket.read_text())["tokens"]["refresh_token"] == "rotated"
    assert (bucket.parent.parent / "auth.json").is_symlink()
    assert bucket.stat().st_mode & 0o777 == 0o600
    assert list((bucket.parent.parent / ".slop-probes").iterdir()) == []


def test_terminal_error_is_remembered_until_credentials_change(bucket, monkeypatch):
    class RPC:
        calls = 0
        def __init__(self, home): pass
        def __enter__(self):
            RPC.calls += 1
            raise RpcError("Sign in again", reauth=True)
        def __exit__(self, *_): pass
    monkeypatch.setattr(refresh, "CodexRPC", RPC)
    assert refresh.refresh_profile("test")["state"] == "reauth_required"
    assert refresh.refresh_profile("test")["state"] == "reauth_required"
    assert RPC.calls == 1
    bucket.write_text(json.dumps(auth(time.time())))
    assert refresh.read_status("test") == {}
    assert refresh.refresh_profile("test")["state"] == "fresh"


def test_network_errors_retry_without_reauth(bucket, monkeypatch):
    class RPC:
        def __init__(self, home): pass
        def __enter__(self): raise RpcError("timeout")
        def __exit__(self, *_): pass
    monkeypatch.setattr(refresh, "CodexRPC", RPC)
    assert refresh.refresh_profile("test")["state"] == "retry"
    assert refresh.read_status("test")["state"] == "retry"


def test_concurrent_update_is_not_overwritten(bucket):
    with pytest.raises(RpcError, match="concurrently"):
        with refresh.account_home(bucket) as home:
            (home / "auth.json").unlink()
            (home / "auth.json").write_text('{"new": "rotation"}')
            bucket.write_text('{"new": "concurrent"}')
    assert json.loads(bucket.read_text()) == {"new": "concurrent"}
    assert len(list(bucket.parent.glob(".test.refresh-conflict.*"))) == 1


def test_probe_persists_rotation_even_if_later_request_fails(bucket):
    with pytest.raises(RpcError, match="usage failed"):
        with refresh.account_home(bucket) as home:
            (home / "auth.json").unlink()
            (home / "auth.json").write_text('{"rotated": true}')
            raise RpcError("usage failed")
    assert json.loads(bucket.read_text()) == {"rotated": True}


def test_profile_lock_times_out(bucket):
    with refresh.profile_lock("test"):
        with pytest.raises(RpcError, match="busy"):
            with refresh.profile_lock("test", timeout=0.01):
                pytest.fail("second writer entered")


def test_one_bad_bucket_does_not_stop_service(bucket, monkeypatch, capsys):
    monkeypatch.setattr(refresh, "list_names", lambda: ["bad", "good"])
    monkeypatch.setattr(refresh, "refresh_profile", lambda n, **kw: dict(name=n, state="retry" if n=="bad" else "fresh"))
    assert refresh.run_service(once=True) == 1
    assert '"name": "good"' in capsys.readouterr().out


def test_api_keys_are_not_refreshed(bucket, monkeypatch):
    bucket.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "fake"}))
    assert refresh.refresh_profile("test")["state"] == "skipped"
