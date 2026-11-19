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
    monkeypatch.setattr(tui, "fetch_each", lambda: iter([expired]))
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
    monkeypatch.setattr(tui, "fetch_each", lambda: iter([Quota(name="ted", ok=False, error="timeout")]))
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
    monkeypatch.setattr(tui, "fetch_each", lambda: iter([Quota(name="ted", ok=False, reauth_required=True)]))
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


def test_first_bucket_shows_before_slower_bucket_finishes(tmp_path, monkeypatch):
    import asyncio
    import json
    import threading

    from slop import tui
    from test_store import _auth

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "slop.toml"))
    (tmp_path / "auth.d").mkdir()
    (tmp_path / "auth.d" / "ted.json").write_text(json.dumps(_auth("ted@example.com")))
    (tmp_path / "auth.d" / "allie.json").write_text(json.dumps(_auth("allie@example.com")))
    gate = threading.Event()

    def fake_fetch():
        yield Quota(name="ted", ok=True, banked_resets=1)
        gate.wait(timeout=10)
        yield Quota(name="allie", ok=True, banked_resets=2)

    monkeypatch.setattr(tui, "fetch_each", fake_fetch)

    async def wait_for(predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("condition never became true")

    async def scenario():
        app = tui.SlopApp()
        async with app.run_test() as pilot:
            await wait_for(lambda: "ted" in app.quotas)
            assert "allie" not in app.quotas
            assert app.loading == {"allie"}
            gate.set()
            await wait_for(lambda: "allie" in app.quotas and not app.loading)
            await pilot.pause()

    asyncio.run(scenario())


def test_collect_resets_sorts_and_skips_past_and_failed():
    from datetime import datetime, timedelta, timezone

    from slop.tui import collect_resets, next_resets

    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    soon = int((now + timedelta(hours=5)).timestamp())
    later = int((now + timedelta(days=3)).timestamp())
    past = int((now - timedelta(hours=1)).timestamp())
    quotas = {
        "ted": Quota(name="ted", ok=True, windows=[
            Window("7d", 50, 50, 10080, later),
            Window("5h", 10, 90, 300, soon),
            Window("5h", 10, 90, 300, past),
            Window("5h", 10, 90, 300, None),
        ]),
        "allie": Quota(name="allie", ok=False, windows=[Window("7d", 1, 99, 10080, soon)]),
    }
    events = collect_resets(quotas, now=now)
    assert [(e.name, e.label) for e in events] == [("ted", "5h"), ("ted", "7d")]
    assert next_resets(events) == {events[0]}


def test_fmt_tokens_scales():
    from slop.tui import fmt_tokens

    assert fmt_tokens(0) == "0"
    assert fmt_tokens(947_592) == "948k"
    assert fmt_tokens(402_196_208) == "402M"
    assert fmt_tokens(1_879_077_803) == "1.88B"


def test_calendar_highlights_today_resets_and_daily_tokens():
    from datetime import date, datetime, timedelta, timezone

    from slop.quota import DailyTokens, UsageSummary
    from slop.theme import GREEN, MAGENTA
    from slop.tui import collect_resets, daily_totals, render_calendar, render_calendar_detail

    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    reset = datetime(2026, 11, 20, 14, 32, tzinfo=timezone.utc)
    quota = Quota(
        name="ted",
        ok=True,
        windows=[Window("7d", 50, 50, 10080, int(reset.timestamp()))],
        daily=[DailyTokens(day=date(2026, 11, 17), tokens=402_196_208)],
        usage_summary=UsageSummary(lifetime_tokens=1_900_000_000, peak_daily_tokens=402_196_208),
    )
    quotas = {"ted": quota}
    events = collect_resets(quotas, now=now)
    rendered = render_calendar(
        2026, 11, today=date(2026, 11, 18), events=events, totals=daily_totals(quotas)
    )
    plain = rendered.plain
    assert "November 2026" in plain
    assert "┌" in plain and "┬" in plain and "│" in plain and "┼" in plain and "└" in plain
    assert all(
        line.startswith("│") and line.endswith("│") and line.count("│") == 8
        for line in plain.splitlines()
        if line.startswith("│")
    )
    expected_time = reset.astimezone().strftime("%H:%M")
    assert expected_time in plain
    assert "ted" in plain
    assert "402M tok" in plain
    styles = [str(span.style) for span in rendered.spans]
    assert any(MAGENTA in style for style in styles)
    assert any(GREEN in style for style in styles)
    detail = render_calendar_detail(
        2026, 11, quotas=quotas, events=events, now=now + timedelta(0), loading=False
    ).plain
    assert "next for ted" in detail
    assert "402M" in detail
    assert "lifetime 1.90B" in detail


def test_calendar_view_toggles_and_pages_months_with_arrows(tmp_path, monkeypatch):
    import asyncio
    import json
    from datetime import date

    from slop import tui
    from test_store import _auth

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "slop.toml"))
    (tmp_path / "auth.d").mkdir()
    (tmp_path / "auth.d" / "ted.json").write_text(json.dumps(_auth("ted@example.com")))
    monkeypatch.setattr(tui, "fetch_each", lambda: iter([Quota(name="ted", ok=True)]))

    async def scenario():
        app = tui.SlopApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            start = (date.today().year, date.today().month)
            assert app._cal == start
            await pilot.press("c")
            await pilot.pause()
            assert app.view == "calendar"
            assert app.query_one("#calendar-view").display is True
            assert app.query_one("#buckets").display is False
            await pilot.press("down")
            await pilot.pause()
            assert app._cal == tui.shift_month(*start, 1)
            await pilot.press("up")
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert app._cal == tui.shift_month(*start, -1)
            await pilot.press("enter")
            await pilot.pause()
            assert app.is_running
            await pilot.press("c")
            await pilot.pause()
            assert app.view == "buckets"
            assert app.query_one("#buckets").display is True

    asyncio.run(scenario())
