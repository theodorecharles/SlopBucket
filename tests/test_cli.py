from types import SimpleNamespace

import pytest

from slop import accounts, cli, tui


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
