import pytest
from rich.text import Text

from slop.tui import _split_bar, _status_markup
from slop.quota import Quota, Window


def test_s_opens_stats_and_removed_keys_are_unbound(tmp_path, monkeypatch):
    import asyncio
    import json
    from slop import tui
    from datetime import date

    from slop.quota import DailyTokens, Quota, UsageSummary
    from slop.stats import Stats
    from test_store import _auth

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "slop.toml"))
    (tmp_path / "auth.d").mkdir()
    (tmp_path / "auth.d" / "ted.json").write_text(json.dumps(_auth("ted@example.com")))
    bucket_quota = Quota(
        name="ted",
        ok=True,
        daily=[DailyTokens(day=date(2026, 11, 1), tokens=2_000_000), DailyTokens(day=date(2026, 11, 2), tokens=3_000_000)],
        usage_summary=UsageSummary(lifetime_tokens=5_000_000, peak_daily_tokens=3_000_000, current_streak_days=4, longest_streak_days=9),
    )
    monkeypatch.setattr(tui, "iter_quotas", lambda: iter({"ted": bucket_quota}.items()))
    fake = Stats(
        total={"input": 900, "cached": 800, "cache_write": 0, "output": 100, "reasoning": 25, "total": 1000},
        days={"2026-11-01": {"input": 900, "cached": 800, "cache_write": 0, "output": 100, "reasoning": 25, "total": 1000}},
        models={"gpt-m1": {"input": 900, "cached": 800, "cache_write": 0, "output": 100, "reasoning": 25, "total": 1000}},
        requests=3,
        sessions=1,
        files=1,
    )
    monkeypatch.setattr(tui, "collect_stats", lambda progress=None: fake)
    bound = {binding.key: binding.action for binding in tui.SlopApp.BINDINGS}
    assert bound["s"] == "stats"
    assert "r" not in bound
    assert "f" not in bound

    async def scenario():
        app = tui.SlopApp()
        async with app.run_test() as pilot:
            await wait_until(lambda: "ted" in app.quotas)
            await pilot.press("s")
            assert isinstance(app.screen, tui.StatsScreen)
            await pilot.pause()
            body = str(app.screen.query_one("#stats-body", tui.Static).render())
            assert "ALL-TIME TOKEN USAGE" in body
            assert "all machines" in body
            assert "5.0M tokens" in body
            assert "BY BUCKET" in body
            assert "THIS MACHINE — SESSION LOGS" in body
            assert "1,000" in body
            assert "gpt-m1" in body
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, tui.StatsScreen)

    asyncio.run(scenario())


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
    header = next(line for line in plain.splitlines() if "Sun" in line)
    assert [name for name in header.replace("│", " ").split()] == [
        "Sun",
        "Mon",
        "Tue",
        "Wed",
        "Thu",
        "Fri",
        "Sat",
    ]
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
    monkeypatch.setattr(tui, "iter_quotas", lambda: iter({"ted": Quota(name="ted", ok=True)}.items()))

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
