import pytest
from rich.text import Text

from slop.tui import _split_bar, _status_markup
from slop.quota import Quota, Window


def test_empty_bar_is_all_track():
    filled, empty = _split_bar(0, 20)
    assert filled == 0
    assert empty == 20


def test_full_bar_is_all_fill():
    filled, empty = _split_bar(100, 20)
    assert filled == 20
    assert empty == 0


def test_status_hides_internals():
    q = Quota(
        name="ted",
        ok=True,
        ordinary_allowed=False,
        rate_limit_reached=True,
        windows=[Window("7d", 100, 0, 10080, 1)],
        credits=173.48,
    )
    markup = _status_markup(q, False)
    assert "ordinaryUsageAllowed" not in markup
    assert "empty" in markup
    assert "173 credits" in markup


def test_expired_bucket_prompts_once_and_can_be_reauthorized(tmp_path, monkeypatch):
    import asyncio
    import json
    from slop import tui
    from test_store import _auth

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "slop.toml"))
    (tmp_path / "auth.d").mkdir()
    (tmp_path / "auth.d" / "ted.json").write_text(json.dumps(_auth("ted@example.com")))
    expired = Quota(name="ted", ok=False, error="Sign in again", reauth_required=True)
    monkeypatch.setattr(tui, "fetch_all", lambda: {"ted": expired})
    calls = []
    monkeypatch.setattr(tui.SlopApp, "_reauthorize", lambda self, name: calls.append(name))

    async def scenario():
        app = tui.SlopApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, tui.ConfirmModal)
            assert app.screen._verb == "reauthorize"
            app._apply_quotas({"ted": expired})
            await pilot.pause()
            assert isinstance(app.screen, tui.ConfirmModal)
            await pilot.press("enter")
            await pilot.pause()
            assert calls == ["ted"]
            app._apply_quotas({"ted": expired})
            await pilot.pause()
            assert not isinstance(app.screen, tui.ConfirmModal)
            await pilot.press("l")
            assert calls == ["ted", "ted"]
    asyncio.run(scenario())


def test_transient_error_does_not_prompt_login(tmp_path, monkeypatch):
    import asyncio
    import json
    from slop import tui
    from test_store import _auth
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "slop.toml"))
    (tmp_path / "auth.d").mkdir()
    (tmp_path / "auth.d" / "ted.json").write_text(json.dumps(_auth("ted@example.com")))
    monkeypatch.setattr(tui, "fetch_all", lambda: {"ted": Quota(name="ted", ok=False, error="timeout")})
    async def scenario():
        app = tui.SlopApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, tui.ConfirmModal)
    asyncio.run(scenario())


def test_login_prompt_exits_tui_before_starting_interactive_login(tmp_path, monkeypatch):
    import asyncio
    import json
    from slop import accounts, tui
    from test_store import _auth
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "slop.toml"))
    (tmp_path / "auth.d").mkdir()
    (tmp_path / "auth.d" / "ted.json").write_text(json.dumps(_auth("ted@example.com")))
    monkeypatch.setattr(tui, "fetch_all", lambda: {"ted": Quota(name="ted", ok=False, reauth_required=True)})
    def unexpected_login(*args, **kwargs):
        raise AssertionError("Login must not run inside Textual")
    monkeypatch.setattr(accounts, "reauthorize_account", unexpected_login)
    async def scenario():
        prompted = set()
        app = tui.SlopApp(reauth_prompted=prompted)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, tui.ConfirmModal)
            await pilot.press("enter")
        assert app.return_value == ("reauth", "ted", [])
        assert prompted == {"ted"}
    asyncio.run(scenario())


@pytest.mark.parametrize("count, label", [(3, "3"), (0, "0"), (None, "unavailable")])
def test_status_shows_banked_resets_even_when_empty(count, label):
    quota = Quota(name="account", ok=True, ordinary_allowed=False, banked_resets=count)
    status = Text.from_markup(_status_markup(quota, False)).plain
    assert "empty" in status
    assert f"banked resets: {label}" in status


def test_status_keeps_banked_resets_while_refreshing():
    quota = Quota(name="account", ok=True, banked_resets=2)
    assert "banked resets: 2" in _status_markup(quota, True)
