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
    monkeypatch.setattr(tui, "iter_quotas", lambda: iter({"ted": expired}.items()))
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
    monkeypatch.setattr(tui, "iter_quotas", lambda: iter({"ted": Quota(name="ted", ok=False, error="timeout")}.items()))
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
    monkeypatch.setattr(tui, "iter_quotas", lambda: iter({"ted": Quota(name="ted", ok=False, reauth_required=True)}.items()))
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


@pytest.fixture
def refresh_dashboard(tmp_path, monkeypatch):
    import json
    from slop import tui
    from test_store import _auth

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "slop.toml"))
    (tmp_path / "auth.d").mkdir()
    for name in ("alice", "bob"):
        (tmp_path / "auth.d" / f"{name}.json").write_text(
            json.dumps(_auth(f"{name}@example.com"))
        )

    # Exercise real Textual timers, accelerating only the dashboard refresh.
    set_interval = tui.SlopApp.set_interval
    intervals = []

    def fast_interval(self, interval, callback=None, **kwargs):
        if callback == self.action_refresh:
            intervals.append(interval)
            interval = 0.1
        return set_interval(self, interval, callback, **kwargs)

    monkeypatch.setattr(tui.SlopApp, "set_interval", fast_interval)
    return tui, intervals


async def wait_until(predicate):
    import asyncio

    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def test_timer_updates_usage_credits_and_resets_without_disturbing_dialog(
    refresh_dashboard, monkeypatch
):
    import asyncio

    tui, intervals = refresh_dashboard
    calls = []

    def fetch():
        calls.append(None)
        return {
            name: Quota(
                name=name, ok=True,
                windows=[Window("5h", 10 * len(calls), 100 - 10 * len(calls), 300, None)],
                credits=100 + len(calls), banked_resets=len(calls),
            )
            for name in ("alice", "bob")
        }

    monkeypatch.setattr(tui, "iter_quotas", lambda: iter(fetch().items()))

    async def scenario():
        app = tui.SlopApp(selected_name="bob")
        async with app.run_test() as pilot:
            await wait_until(lambda: "bob" in app.quotas)
            await pilot.pause()
            assert intervals == [30]
            assert app._selected_name() == "bob"
            modal = tui.NameModal("Rename bob", default="bob")
            app.push_screen(modal)
            await pilot.pause()
            field = modal.query_one(tui.Input)
            field.value = "still typing"
            before = app.quotas["bob"].banked_resets
            await wait_until(lambda: app.quotas["bob"].banked_resets >= before + 2)
            await pilot.pause()
            assert app.screen is modal
            assert modal.focused is field
            assert field.value == "still typing"
            assert app._selected_name() == "bob"
            item = next(item for item in app.query(tui.BucketItem) if item.bucket_name == "bob")
            count = item._quota.banked_resets
            assert item.query_one(tui.UsageMeter).window.remaining_percent == 100 - 10 * count
            footer = str(item.query_one("#foot", tui.Static).render())
            assert f"{100 + count} credits" in footer
            assert f"banked resets: {count}" in footer
        completed_calls = len(calls)
        await asyncio.sleep(0.2)
        assert len(calls) == completed_calls

    asyncio.run(scenario())


def test_timer_and_manual_refresh_wait_for_slow_fetch(refresh_dashboard, monkeypatch):
    import asyncio
    import threading

    tui, intervals = refresh_dashboard
    release = threading.Event()
    calls = []

    def fetch():
        calls.append(None)
        assert release.wait(timeout=5)
        return {"alice": Quota(name="alice", ok=True, credits=50, banked_resets=2)}

    monkeypatch.setattr(tui, "iter_quotas", lambda: iter(fetch().items()))

    async def scenario():
        app = tui.SlopApp()
        async with app.run_test() as pilot:
            try:
                await wait_until(lambda: bool(calls))
                app.action_refresh()
                await asyncio.sleep(0.35)
                assert intervals == [30]
                assert len(calls) == 1
                assert app.loading == {"alice", "bob"}
            finally:
                release.set()
            await wait_until(lambda: "alice" in app.quotas and not app.loading)
            await wait_until(lambda: len(calls) >= 2)
            await pilot.pause()
            assert app.quotas["alice"].credits == 50

    asyncio.run(scenario())


def test_empty_dashboard_skips_polling_until_bucket_appears(refresh_dashboard, monkeypatch, tmp_path):
    import asyncio

    tui, intervals = refresh_dashboard
    paths = list((tmp_path / "auth.d").glob("*.json"))
    saved = paths[0].read_text()
    for path in paths:
        path.unlink()
    calls = []

    def fetch():
        calls.append(None)
        return {"alice": Quota(name="alice", ok=True, banked_resets=1)}

    monkeypatch.setattr(tui, "iter_quotas", lambda: iter(fetch().items()))

    async def scenario():
        app = tui.SlopApp()
        async with app.run_test():
            await asyncio.sleep(0.25)
            assert intervals == [30]
            assert calls == []
            (tmp_path / "auth.d" / "alice.json").write_text(saved)
            await wait_until(lambda: "alice" in app.quotas)
            assert app.quotas["alice"].banked_resets == 1

    asyncio.run(scenario())


def test_startup_shows_fast_account_and_accepts_input_while_another_is_pending(
    refresh_dashboard, monkeypatch
):
    import asyncio
    import threading
    from slop import quota

    tui, _ = refresh_dashboard
    release = threading.Event()
    slow_started = threading.Event()

    def fetch(name):
        if name == "alice":
            slow_started.set()
            assert release.wait(timeout=5)
            raise RuntimeError("temporary failure")
        return Quota(name=name, ok=True, credits=123, banked_resets=4)

    # Use the real concurrent iterator and GUI worker. Only the network is fake.
    monkeypatch.setattr(quota, "fetch_quota", fetch)

    async def scenario():
        app = tui.SlopApp()
        async with app.run_test() as pilot:
            try:
                await wait_until(lambda: slow_started.is_set() and "bob" in app.quotas)
                await pilot.pause()
                assert app.loading == {"alice"}
                assert "alice" not in app.quotas
                bob = next(item for item in app.query(tui.BucketItem) if item.bucket_name == "bob")
                footer = str(bob.query_one("#foot", tui.Static).render())
                assert "123 credits" in footer
                assert "banked resets: 4" in footer
                await pilot.press("down")
                assert app._selected_name() == "bob"
                await pilot.press("n")
                assert isinstance(app.screen, tui.NameModal)
                await pilot.press("x")
                assert app.screen.query_one(tui.Input).value.endswith("x")
                assert not release.is_set()
            finally:
                release.set()
            await wait_until(lambda: "alice" in app.quotas and not app.loading)
            assert not app.quotas["alice"].ok
            assert app.quotas["bob"].ok
            assert isinstance(app.screen, tui.NameModal)

    asyncio.run(scenario())
