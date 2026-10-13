import argparse
import json
from types import SimpleNamespace

import pytest

from slop import accounts, cli, tui
from slop.quota import Quota, Window
from slop.store import Identity, Profile


@pytest.fixture(autouse=True)
def isolated_config_and_update_check(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "slop.toml"))
    monkeypatch.setattr(cli, "start_check", lambda binary: None)


@pytest.mark.parametrize("action", ["reauth", "add"])
def test_login_runs_after_dashboard_closes_then_dashboard_reopens(monkeypatch, action):
    events = []
    opened = []
    class App:
        def __init__(self, **kwargs): opened.append(kwargs)
        def run(self):
            events.extend(["open", "close"])
            return (action, "ted", []) if len(opened) == 1 else None
    def login(name, *, device):
        assert events[-1] == "close"
        assert name == "ted" and device
        events.append("login")
    monkeypatch.setattr(tui, "SlopApp", App)
    monkeypatch.setattr(cli, "ensure_file_store", lambda: None)
    monkeypatch.setattr(accounts, "reauthorize_account", login)
    monkeypatch.setattr(accounts, "add_account", login)
    assert cli.cmd_tui(SimpleNamespace()) == 0
    assert events == ["open", "close", "login", "open", "close"]
    assert opened[1]["selected_name"] == "ted"
    assert opened[1]["reauth_prompted"] == {"ted"}


def test_login_validation_error_stays_visible_before_dashboard_reopens(monkeypatch, capsys):
    opened = []
    events = []
    class App:
        def __init__(self, **kwargs): opened.append(kwargs)
        def run(self):
            events.append("dashboard")
            return ("reauth", "ted", []) if len(opened) == 1 else None
    def login(*args, **kwargs):
        print("Successfully logged in")
        raise accounts.AddError("signed into a different account; bucket needs ted@example.com")
    def acknowledge(prompt):
        captured = capsys.readouterr()
        assert "Successfully logged in" in captured.out
        assert "ted@example.com" in captured.err
        events.append("error acknowledged")
        return ""
    monkeypatch.setattr(tui, "SlopApp", App)
    monkeypatch.setattr(cli, "ensure_file_store", lambda: None)
    monkeypatch.setattr(accounts, "reauthorize_account", login)
    monkeypatch.setattr("builtins.input", acknowledge)
    assert cli.cmd_tui(SimpleNamespace()) == 0
    assert events == ["dashboard", "error acknowledged", "dashboard"]


@pytest.mark.parametrize("during_login", [True, False])
def test_ctrl_c_during_login_or_error_exits_cleanly(monkeypatch, during_login):
    class App:
        def __init__(self, **kwargs): pass
        def run(self): return ("reauth", "ted", [])
    def login(*args, **kwargs):
        if during_login:
            raise KeyboardInterrupt
        raise accounts.AddError("login cancelled; bucket unchanged")
    def interrupt(_): raise KeyboardInterrupt
    monkeypatch.setattr(tui, "SlopApp", App)
    monkeypatch.setattr(cli, "ensure_file_store", lambda: None)
    monkeypatch.setattr(accounts, "reauthorize_account", login)
    monkeypatch.setattr("builtins.input", interrupt)
    assert cli.cmd_tui(SimpleNamespace()) == 0


@pytest.fixture
def quota_accounts(tmp_path, monkeypatch):
    profiles = [
        Profile(name, tmp_path / f"{name}.json", Identity(), False)
        for name in ("alice", "bob", "charlie")
    ]
    quotas = {
        profile.name: Quota(
            name=profile.name,
            ok=True,
            windows=[Window("5h", 25, 75, 300, None)],
            banked_resets=count,
        )
        for profile, count in zip(profiles, [2, 0, None])
    }
    monkeypatch.setattr(cli, "ensure_file_store", lambda: None)
    monkeypatch.setattr(cli, "list_profiles", lambda: profiles)
    monkeypatch.setattr(cli, "current_name", lambda: None)
    monkeypatch.setattr(cli, "fetch_all", lambda profiles: quotas)
    monkeypatch.setattr(cli, "fetch_quota", quotas.__getitem__)
    return quotas


@pytest.mark.parametrize("command", ["list", "quota"])
def test_text_usage_shows_each_accounts_banked_resets(quota_accounts, capsys, command):
    if command == "list":
        assert cli.cmd_list(argparse.Namespace(quota=True, json=False)) == 0
    else:
        assert cli.cmd_quota(argparse.Namespace(name=None)) == 0
    lines = capsys.readouterr().out.splitlines()
    for name, label in [("alice", "2"), ("bob", "0"), ("charlie", "unavailable")]:
        line = next(line for line in lines if name in line)
        assert "75% left" in line
        assert f"banked resets: {label}" in line


def test_json_usage_includes_banked_resets(quota_accounts, capsys):
    assert cli.cmd_list(argparse.Namespace(quota=True, json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["name"]: row["quota"]["banked_resets"] for row in payload} == {
        "alice": 2,
        "bob": 0,
        "charlie": None,
    }


def test_list_without_quota_does_not_fetch_usage(quota_accounts, monkeypatch, capsys):
    def unexpected_fetch(profiles):
        pytest.fail("plain list should not fetch usage")

    monkeypatch.setattr(cli, "fetch_all", unexpected_fetch)
    assert cli.cmd_list(argparse.Namespace(quota=False, json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert all(row["quota"] is None for row in payload)


@pytest.mark.parametrize("from_dashboard", [True, False])
def test_update_then_launch_preserves_selected_account_and_arguments(
    tmp_path, monkeypatch, from_dashboard
):
    from slop.store import current_name
    from test_store import _auth

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.d").mkdir()
    for name in ("alice", "bob"):
        (tmp_path / "auth.d" / f"{name}.json").write_text(json.dumps(_auth(f"{name}@example.com")))
    (tmp_path / "auth.json").symlink_to("auth.d/alice.json")
    events = []
    check = object()
    binary = str(tmp_path / "codex")
    extra = ["resume", "--last"]
    monkeypatch.setattr(cli.shutil, "which", lambda _: binary)
    monkeypatch.setattr(cli, "start_check", lambda _: events.append("background check") or check)

    def update(path, pending):
        assert path == binary
        assert pending is (check if from_dashboard else None)
        assert current_name() == "bob"
        events.append("update")
        # A second Slop launch can switch accounts while the updater runs.
        cli.switch_to("alice")

    def launch(path, args):
        assert path == binary
        assert current_name() == "bob"
        assert args[1:3] == ["-c", "check_for_update_on_startup=false"]
        assert args[-2:] == extra
        events.append("launch")

    class App:
        def __init__(self, **kwargs): pass
        def run(self):
            assert events == ["background check"]
            events.append("dashboard closed")
            return "launch", "bob", extra

    monkeypatch.setattr(tui, "SlopApp", App)
    monkeypatch.setattr(cli, "ensure_updated", update)
    monkeypatch.setattr(cli.os, "execvp", launch)
    if from_dashboard:
        cli.cmd_tui(SimpleNamespace())
        assert events == ["background check", "dashboard closed", "update", "launch"]
    else:
        cli.cmd_launch(SimpleNamespace(name="bob", codex_args=extra))
        assert events == ["update", "launch"]


def test_auto_update_opt_out_skips_check_and_updater(monkeypatch):
    from slop.config import Config, LaunchConfig, save

    save(Config(launch=LaunchConfig(auto_update=False)))
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/test/codex")
    monkeypatch.setattr(cli, "ensure_file_store", lambda: None)
    monkeypatch.setattr(cli, "switch_to", lambda _: None)
    monkeypatch.setattr(cli, "start_check", lambda _: pytest.fail("update check disabled"))
    monkeypatch.setattr(cli, "ensure_updated", lambda *args: pytest.fail("updater disabled"))
    launched = []
    monkeypatch.setattr(cli.os, "execvp", lambda path, args: launched.append(args))

    class App:
        def __init__(self, **kwargs): pass
        def run(self): return "launch", "bob", []

    monkeypatch.setattr(tui, "SlopApp", App)
    cli.cmd_tui(SimpleNamespace())
    assert len(launched) == 1
    assert "check_for_update_on_startup=false" not in launched[0]


def test_auto_update_config_can_be_disabled_and_enabled(monkeypatch):
    from slop.config import load

    cli.cmd_config(SimpleNamespace(set=("launch.auto_update", "false")))
    assert not load().launch.auto_update
    cli.cmd_config(SimpleNamespace(set=("launch.auto_update", "true")))
    assert load().launch.auto_update
