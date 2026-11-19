from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.worker import Worker
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

from slop.config import Config, ensure as ensure_config
from slop.quota import Quota, Window, banked_resets_label, iter_quotas, until_label
from slop.stats import Stats, collect_stats
from slop.stats import fmt_tokens as _fmt_tokens_big
from slop.theme import BLUE, FG, GREEN, GROK_NIGHT, GUTTER, MAGENTA, MUTED, ORANGE, RED, YELLOW
from slop.store import (
    StoreError,
    current_name,
    ensure_file_store,
    identity_from_auth,
    auth_path,
    list_profiles,
    remove,
    rename,
    save_current,
    suggest_name,
    switch_to,
    unmanaged_auth_exists,
)

QUOTA_REFRESH_INTERVAL = 30


def _bar_style(remaining: float) -> str:
    if remaining < 20:
        return RED
    if remaining < 40:
        return YELLOW
    return GREEN


def _fmt_credits(value: float | None) -> str:
    if value is None:
        return ""
    if value >= 10:
        return f"{value:.0f} credits"
    return f"{value:.2f} credits"


def _split_bar(remaining: float, width: int) -> tuple[int, int]:
    remaining = max(0.0, min(100.0, remaining))
    width = max(4, width)
    filled = int(round((remaining / 100.0) * width))
    filled = min(width, max(0, filled))
    return filled, width - filled


class UsageMeter(Static):
    DEFAULT_CSS = """
    UsageMeter {
        width: 1fr;
        height: 1;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, window: Window) -> None:
        super().__init__()
        self.window = window

    def on_resize(self) -> None:
        self.refresh()

    def render(self) -> Text:
        remaining = self.window.remaining_percent
        color = _bar_style(remaining)
        resets = until_label(self.window.resets_at)
        meta = f"{remaining:3.0f}%"
        if resets:
            meta += f" · {resets}"
        prefix = f"{self.window.label:<3} "
        # prefix + space + bar + two spaces + meta
        bar_width = max(8, self.size.width - len(prefix) - len(meta) - 2)
        filled, empty = _split_bar(remaining, bar_width)
        text = Text()
        text.append(prefix, style=MUTED)
        if filled:
            text.append("█" * filled, style=color)
        if empty:
            text.append("░" * empty, style=GUTTER)
        text.append("  ")
        text.append(meta, style=color if remaining < 40 else MUTED)
        return text


def _status_markup(quota: Quota | None, loading: bool) -> str:
    if loading and quota is None:
        return f"[{MUTED}]fetching usage…[/]"
    if quota is None:
        return f"[{MUTED}]waiting for usage[/]"
    if not quota.ok:
        return f"[{RED}]{quota.error or 'usage unavailable'}[/]"
    if quota.blocked:
        status = f"[{RED}]empty[/]"
    elif any(w.remaining_percent < 20 for w in quota.windows):
        status = f"[{YELLOW}]low[/]"
    else:
        status = f"[{GREEN}]ok[/]"
    extras: list[str] = [status, f"[{MUTED}]{banked_resets_label(quota.banked_resets)}[/]"]
    credits = _fmt_credits(quota.credits)
    if credits:
        extras.append(f"[{MUTED}]{credits}[/]")
    bits = []
    for label, windows in quota.extra:
        if not windows:
            continue
        parts = " · ".join(f"{w.label} {w.remaining_percent:.0f}%" for w in windows)
        bits.append(f"{label} {parts}")
    if bits:
        extras.append(f"[{MUTED}]{'  ·  '.join(bits)}[/]")
    return "   ".join(extras)


@dataclass(frozen=True)
class ResetEvent:
    name: str
    label: str
    when: datetime


def collect_resets(
    quotas: dict[str, Quota], *, now: datetime | None = None
) -> list[ResetEvent]:
    """Upcoming window resets across all loaded buckets, soonest first."""
    now = now or datetime.now().astimezone()
    out: list[ResetEvent] = []
    for quota in quotas.values():
        if quota is None or not quota.ok:
            continue
        for window in quota.windows:
            if not window.resets_at:
                continue
            when = datetime.fromtimestamp(window.resets_at, tz=timezone.utc).astimezone()
            if when <= now:
                continue
            out.append(ResetEvent(name=quota.name, label=window.label, when=when))
    out.sort(key=lambda event: (event.when, event.name, event.label))
    return out


def next_resets(events: list[ResetEvent]) -> set[ResetEvent]:
    """The soonest upcoming reset for each bucket."""
    seen: set[str] = set()
    out: set[ResetEvent] = set()
    for event in events:
        if event.name not in seen:
            seen.add(event.name)
            out.add(event)
    return out


def daily_totals(quotas: dict[str, Quota]) -> dict[date, int]:
    totals: dict[date, int] = {}
    for quota in quotas.values():
        if quota is None or not quota.ok:
            continue
        for entry in quota.daily:
            totals[entry.day] = totals.get(entry.day, 0) + entry.tokens
    return totals


def fmt_tokens(tokens: int) -> str:
    if tokens >= 1_000_000_000:
        return f"{tokens / 1e9:.2f}B"
    if tokens >= 1_000_000:
        value = tokens / 1e6
        return f"{value:.0f}M" if value >= 100 else f"{value:.1f}M"
    if tokens >= 10_000:
        return f"{tokens / 1e3:.0f}k"
    if tokens >= 1_000:
        return f"{tokens / 1e3:.1f}k"
    return str(tokens)


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + month - 1) + delta
    return index // 12, index % 12 + 1


def _token_style(tokens: int, peak: int) -> str:
    if peak <= 0 or tokens <= 0:
        return MUTED
    ratio = tokens / peak
    if ratio >= 0.85:
        return f"bold {ORANGE}"
    if ratio >= 0.5:
        return YELLOW
    if ratio >= 0.2:
        return FG
    return MUTED


def render_calendar(
    year: int,
    month: int,
    *,
    today: date,
    events: list[ResetEvent],
    totals: dict[date, int],
    width: int = 112,
) -> Text:
    cell_w = max(11, min(18, (width - 8) // 7))
    by_day: dict[date, list[ResetEvent]] = {}
    for event in events:
        by_day.setdefault(event.when.date(), []).append(event)
    upcoming = next_resets(events)
    month_peak = max(
        (tokens for day, tokens in totals.items() if (day.year, day.month) == (year, month)),
        default=0,
    )
    earliest = min(totals) if totals else None
    text = Text()
    text.append(f"{_calendar.month_name[month]} {year}", style=f"bold {MAGENTA}")
    text.append("    cells: tokens used that day · reset times below\n", style=MUTED)

    def hline(left: str, mid: str, right: str) -> None:
        text.append(left + mid.join("─" * cell_w for _ in range(7)) + right + "\n", style=GUTTER)

    def append_row(cells: list[tuple[str, str]]) -> None:
        text.append("│", style=GUTTER)
        for segment, style in cells:
            text.append(segment, style=style)
            text.append("│", style=GUTTER)
        text.append("\n")

    def cell(content: str) -> str:
        return (" " + content)[:cell_w].ljust(cell_w)

    hline("┌", "┬", "┐")
    append_row([(cell(weekday), f"bold {MUTED}") for weekday in ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")])
    weeks = _calendar.Calendar(firstweekday=_calendar.SUNDAY).monthdatescalendar(year, month)
    for week in weeks:
        hline("├", "┼", "┤")
        lines: list[list[tuple[str, str]]] = [[] for _ in range(5)]
        for day in week:
            day_events = by_day.get(day, [])
            if day == today:
                day_style = f"bold #141414 on {MAGENTA}"
            elif day_events:
                day_style = f"bold #141414 on {GREEN}"
            elif day.month == month:
                day_style = FG
            else:
                day_style = MUTED
            lines[0].append((cell(f"{day.day:>2}"), day_style))
            tokens = totals.get(day)
            if tokens is None and earliest is not None and earliest <= day <= today:
                tokens = 0
            token_text = f"{fmt_tokens(tokens)} tok" if tokens is not None and day <= today else ""
            lines[1].append((cell(token_text), _token_style(tokens or 0, month_peak)))
            shown = day_events[:3] if len(day_events) <= 3 else day_events[:2]
            for slot in range(3):
                if slot < len(shown):
                    event = shown[slot]
                    entry = f"{event.when:%H:%M} {event.name} {event.label}"
                    style = f"bold {GREEN}" if event in upcoming else BLUE
                elif slot == 2 and len(day_events) > 3:
                    entry = f"+{len(day_events) - 2} more"
                    style = MUTED
                else:
                    entry, style = "", MUTED
                lines[2 + slot].append((cell(entry), style))
        for line in lines:
            append_row(line)
    hline("└", "┴", "┘")
    text.append("today", style=f"bold #141414 on {MAGENTA}")
    text.append("  ", style=MUTED)
    text.append("reset day", style=f"bold #141414 on {GREEN}")
    text.append("  bold green entry = that bucket's next reset", style=MUTED)
    text.append("\n↑ previous month   ↓ next month   c buckets", style=MUTED)
    return text


def render_calendar_detail(
    year: int,
    month: int,
    *,
    quotas: dict[str, Quota],
    events: list[ResetEvent],
    now: datetime,
    loading: bool,
) -> Text:
    text = Text()
    text.append("Upcoming resets\n", style=f"bold {FG}")
    upcoming = next_resets(events)
    if events:
        for event in events[:12]:
            stamp = int(event.when.timestamp())
            line = (
                f"  {event.when:%a %b %d %H:%M}  {event.name:<14} "
                f"{event.label:<3} window  (in {until_label(stamp, now=now.timestamp())})"
            )
            text.append(line, style=FG)
            if event in upcoming:
                text.append(f"   ← next for {event.name}", style=f"bold {GREEN}")
            text.append("\n")
    elif loading:
        text.append("  fetching usage…\n", style=MUTED)
    else:
        text.append("  no upcoming resets loaded\n", style=MUTED)
    text.append(f"\nTokens · {_calendar.month_name[month]} {year}\n", style=f"bold {FG}")
    rows: list[tuple[str, int, Quota]] = []
    for name, quota in quotas.items():
        if quota is None or not quota.ok or not quota.daily:
            continue
        month_total = sum(
            entry.tokens for entry in quota.daily if (entry.day.year, entry.day.month) == (year, month)
        )
        rows.append((name, month_total, quota))
    if rows:
        for name, month_total, quota in sorted(rows, key=lambda row: -row[1]):
            text.append(f"  {name:<14} {fmt_tokens(month_total):>7} this month", style=FG)
            summary = quota.usage_summary
            if summary is not None:
                bits = []
                if summary.peak_daily_tokens is not None:
                    bits.append(f"peak day {fmt_tokens(summary.peak_daily_tokens)}")
                if summary.lifetime_tokens is not None:
                    bits.append(f"lifetime {fmt_tokens(summary.lifetime_tokens)}")
                if summary.current_streak_days is not None:
                    bits.append(f"streak {summary.current_streak_days}d")
                if bits:
                    text.append("   ·   " + " · ".join(bits), style=MUTED)
            text.append("\n")
        text.append(
            f"  {'all buckets':<14} {fmt_tokens(sum(row[1] for row in rows)):>7} this month\n",
            style=f"bold {FG}",
        )
    elif loading:
        text.append("  fetching usage history…\n", style=MUTED)
    else:
        text.append("  daily token history unavailable\n", style=MUTED)
    return text


class CalendarPane(Static):
    can_focus = True
    BINDINGS = [
        Binding("up,left,pageup", "prev_month", "Previous month", show=False),
        Binding("down,right,pagedown", "next_month", "Next month", show=False),
    ]
    DEFAULT_CSS = """
    CalendarPane {
        height: auto;
        padding: 1 2 0 2;
    }
    CalendarPane:focus {
        background: #181818;
    }
    """

    def action_prev_month(self) -> None:
        app = self.app
        if isinstance(app, SlopApp):
            app.action_prev_month()

    def action_next_month(self) -> None:
        app = self.app
        if isinstance(app, SlopApp):
            app.action_next_month()


class BucketItem(ListItem):
    DEFAULT_CSS = """
    BucketItem {
        layout: vertical;
        height: auto;
        padding: 1 2 1 2;
        margin: 0 2 1 2;
        background: #1c1c1c;
        border: tall #333333;
        color: #e1e1e1;
    }
    ListView > BucketItem.-highlight {
        background: #242424;
        border: tall #bb9af7;
        color: #e1e1e1;
        text-style: none;
    }
    ListView:focus > BucketItem.-highlight {
        background: #242424;
        border: tall #bb9af7;
        color: #e1e1e1;
        text-style: none;
    }
    BucketItem .head {
        height: 1;
        margin-bottom: 1;
    }
    BucketItem .name {
        width: auto;
        text-style: bold;
        color: #e1e1e1;
        padding-right: 1;
    }
    BucketItem .badge {
        width: auto;
        color: #141414;
        background: #bb9af7;
        text-style: bold;
        padding: 0 1;
        margin-right: 1;
    }
    BucketItem .email {
        width: 1fr;
        color: #6c6c6c;
        padding-left: 1;
    }
    BucketItem .plan {
        width: auto;
        color: #6c6c6c;
        text-style: italic;
    }
    BucketItem #meters {
        height: auto;
        width: 1fr;
    }
    BucketItem #foot {
        height: auto;
        color: #6c6c6c;
    }
    """

    def __init__(
        self,
        name: str,
        active: bool,
        email: str | None,
        plan: str | None,
        quota: Quota | None,
        loading: bool,
    ) -> None:
        super().__init__()
        self.bucket_name = name
        self._active = active
        self._email = email
        self._plan = plan
        self._quota = quota
        self._loading = loading

    def compose(self) -> ComposeResult:
        with Horizontal(classes="head"):
            yield Label(self.bucket_name, classes="name")
            yield Label("ACTIVE", classes="badge", id="badge")
            yield Label(self._email or "", classes="email")
            yield Label((self._plan or "").upper(), classes="plan")
        yield Vertical(id="meters")
        yield Static(_status_markup(self._quota, self._loading), id="foot", markup=True)

    def on_mount(self) -> None:
        self.populate()

    def set_state(self, *, active: bool, quota: Quota | None, loading: bool, email: str | None = None, plan: str | None = None) -> None:
        self._active = active
        self._quota = quota
        self._loading = loading
        if email is not None:
            self._email = email
        if plan is not None:
            self._plan = plan
        if self.is_mounted:
            self.populate()

    def populate(self) -> None:
        badge = self.query_one("#badge", Label)
        badge.display = self._active
        email_w = self.query_one(".email", Label)
        plan_w = self.query_one(".plan", Label)
        email_w.update(self._email or "")
        plan_w.update((self._plan or "").upper())
        meters = self.query_one("#meters", Vertical)
        meters.remove_children()
        quota = self._quota
        if self._loading and quota is None:
            meters.mount(Static("fetching usage…", markup=True))
        elif quota is None:
            meters.mount(Static(f"[{MUTED}]waiting for usage[/]", markup=True))
        elif not quota.ok:
            meters.mount(Static(f"[{RED}]{quota.error or 'usage unavailable'}[/]", markup=True))
        elif quota.windows:
            for window in quota.windows:
                meters.mount(UsageMeter(window))
        else:
            meters.mount(Static(f"[{MUTED}]no usage windows[/]", markup=True))
        if quota and quota.ok:
            if quota.email:
                self._email = quota.email
                email_w.update(quota.email)
            if quota.plan:
                self._plan = quota.plan
                plan_w.update(quota.plan.upper())
        self.query_one("#foot", Static).update(_status_markup(quota, self._loading))


@dataclass(frozen=True)
class AddSpec:
    name: str
    device: bool


class NameModal(ModalScreen[str | None]):
    BINDINGS = [
        Binding("enter", "submit", "ok", show=True, priority=True),
        Binding("escape", "cancel", "cancel", show=True, priority=True),
    ]
    DEFAULT_CSS = """
    NameModal { align: center middle; }
    #dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        border: tall #bb9af7;
        background: #1c1c1c;
    }
    Input { margin: 1 0; }
    .hint { color: #6c6c6c; }
    """

    def __init__(self, title: str, default: str = "", placeholder: str = "bucket name") -> None:
        super().__init__()
        self._title = title
        self._default = default
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title)
            yield Input(value=self._default, placeholder=self._placeholder, id="name")
            yield Label("enter  ok      esc  cancel", classes="hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def action_submit(self) -> None:
        value = self.query_one(Input).value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self) -> None:
        self.action_submit()


class AddModal(ModalScreen[AddSpec | None]):
    BINDINGS = [
        Binding("enter", "device", "device login", show=True, priority=True),
        Binding("ctrl+b", "browser", "browser login", show=True, priority=True),
        Binding("escape", "cancel", "cancel", show=True, priority=True),
    ]
    DEFAULT_CSS = """
    AddModal { align: center middle; }
    #dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        border: tall #bb9af7;
        background: #1c1c1c;
    }
    Input { margin: 1 0; }
    .hint { color: #6c6c6c; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Add a Codex bucket")
            yield Label("Logs in a new account. Saved buckets are not logged out.")
            yield Input(placeholder="name, e.g. allie", id="name")
            yield Label("enter  device code      ctrl+b  browser      esc  cancel", classes="hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _spec(self, device: bool) -> AddSpec | None:
        name = self.query_one(Input).value.strip()
        if not name:
            return None
        return AddSpec(name=name, device=device)

    def action_browser(self) -> None:
        spec = self._spec(False)
        if spec:
            self.dismiss(spec)

    def action_device(self) -> None:
        spec = self._spec(True)
        if spec:
            self.dismiss(spec)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self) -> None:
        self.action_device()


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("enter", "yes", "confirm", show=True, priority=True),
        Binding("y", "yes", "confirm", show=False, priority=True),
        Binding("n", "no", "cancel", show=False, priority=True),
        Binding("escape", "no", "cancel", show=True, priority=True),
    ]
    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    #dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        border: tall #f7768e;
        background: #1c1c1c;
    }
    .hint { color: #6c6c6c; margin-top: 1; }
    """

    def __init__(self, question: str, verb: str = "delete") -> None:
        super().__init__()
        self._question = question
        self._verb = verb

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._question)
            yield Label(f"enter/y  {self._verb}      esc/n  cancel", classes="hint")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


_PARTIAL_BLOCKS = " ▁▂▃▄▅▆▇█"


def _pct(part: int, whole: int) -> str:
    if whole <= 0:
        return "0%"
    return f"{part / whole * 100:.0f}%"


def _column_chart(values: list[int], height: int = 8) -> list[str]:
    """One column per value, scaled to the peak, using eighth blocks."""
    if not values:
        return []
    peak = max(values) or 1
    eighths = [int(round(value / peak * height * 8)) for value in values]
    rows: list[str] = []
    for row in range(height, 0, -1):
        chars: list[str] = []
        for filled in eighths:
            if filled >= row * 8:
                chars.append("█")
            elif filled <= (row - 1) * 8:
                chars.append(" ")
            else:
                chars.append(_PARTIAL_BLOCKS[filled - (row - 1) * 8])
        rows.append("".join(chars).rstrip())
    return rows


def _recent_day_values(stats: Stats, days: int, today: date) -> list[tuple[date, int]]:
    return [
        (day, stats.day_total(day.isoformat()))
        for day in (today - timedelta(days=offset) for offset in range(days - 1, -1, -1))
    ]


def _server_view(quotas: dict[str, Quota] | None):
    """Aggregate server-reported usage across buckets.

    Returns (total, daily totals, per-bucket lifetime rows) or None when
    no bucket has reported any usage history yet.
    """
    if not quotas:
        return None
    lifetimes: list[tuple[str, object]] = []
    total = 0
    contributed = False
    for name, quota in quotas.items():
        if quota is None or not quota.ok:
            continue
        summary = quota.usage_summary
        daily_sum = sum(entry.tokens for entry in quota.daily)
        if summary is not None and summary.lifetime_tokens is not None:
            lifetimes.append((name, summary))
            total += summary.lifetime_tokens
            contributed = True
        elif daily_sum:
            total += daily_sum
            contributed = True
    daily = {day: tokens for day, tokens in daily_totals(quotas).items() if tokens > 0}
    if not contributed and not daily:
        return None
    if not total:
        total = sum(daily.values())
    lifetimes.sort(key=lambda item: -(item[1].lifetime_tokens or 0))
    return total, daily, lifetimes


def render_stats(
    stats: Stats,
    width: int,
    today: date | None = None,
    quotas: dict[str, Quota] | None = None,
) -> Text:
    today = today or date.today()
    width = max(40, width)
    text = Text()
    total = stats.total
    server = _server_view(quotas)
    if server is None and total["total"] <= 0:
        text.append("ALL-TIME TOKEN USAGE\n\n", style=f"bold {MAGENTA}")
        text.append("No token usage available yet. Server history has not loaded and no local sessions were found.", style=MUTED)
        return text

    def heading(label: str) -> None:
        text.append(f"{label}\n", style=f"bold {MAGENTA}")

    def row(label: str, value: str, note: str = "") -> None:
        text.append(f"  {label:<11}", style=MUTED)
        text.append(f"{value:>10}", style=FG)
        if note:
            text.append(f"   {note}", style=MUTED)
        text.append("\n")

    bar_width = max(10, min(28, width - 46))

    def daily_section(daily: dict[date, int]) -> None:
        best_day, best_value = max(daily.items(), key=lambda item: item[1])

        def window(days: int) -> int:
            return sum(daily.get(today - timedelta(days=offset), 0) for offset in range(days))

        row("active days", f"{len(daily):,}", f"since {min(daily).isoformat()}")
        row("avg / day", _fmt_tokens_big(sum(daily.values()) // max(1, len(daily))), "per active day")
        today_note = f"last 7d {_fmt_tokens_big(window(7))} · last 30d {_fmt_tokens_big(window(30))}"
        row("today", _fmt_tokens_big(daily.get(today, 0)), today_note)
        row("best day", _fmt_tokens_big(best_value), best_day.isoformat())
        text.append("\n")
        chart_days = max(14, min(90, width - 4))
        recent = [(today - timedelta(days=offset)) for offset in range(chart_days - 1, -1, -1)]
        values = [daily.get(day, 0) for day in recent]
        peak_day = max(recent, key=lambda day: daily.get(day, 0))
        heading(f"DAILY TOKENS — LAST {chart_days} DAYS")
        text.append(f"  peak {_fmt_tokens_big(daily.get(peak_day, 0))} on {peak_day.isoformat()}\n", style=MUTED)
        for line in _column_chart(values):
            text.append(f"  {line}\n", style=BLUE)
        start_label = recent[0].isoformat()
        end_label = "today"
        gap = max(1, len(recent) - len(start_label) - len(end_label))
        text.append(f"  {start_label}{' ' * gap}{end_label}\n\n", style=MUTED)
        heading("BUSIEST DAYS")
        for day, value in sorted(daily.items(), key=lambda item: item[1], reverse=True)[:5]:
            filled, empty = _split_bar(value / best_value * 100 if best_value else 0, bar_width)
            text.append(f"  {day.isoformat()}  ", style=FG)
            text.append("█" * filled, style=GREEN)
            text.append("░" * empty, style=GUTTER)
            text.append(f"  {_fmt_tokens_big(value)}\n", style=MUTED)
        text.append("\n")

    if server is not None:
        server_total, daily, lifetimes = server
        heading("ALL-TIME TOKEN USAGE")
        text.append("  all buckets · all machines · server-reported\n\n", style=MUTED)
        text.append(f"  {_fmt_tokens_big(server_total)} tokens", style=f"bold {FG}")
        text.append(f"   ({server_total:,})\n\n", style=MUTED)
        if daily:
            daily_section(daily)
        if lifetimes:
            heading("BY BUCKET")
            peak_lifetime = lifetimes[0][1].lifetime_tokens or 1
            lifetime_sum = sum(summary.lifetime_tokens or 0 for _, summary in lifetimes)
            for name, summary in lifetimes:
                lifetime = summary.lifetime_tokens or 0
                filled, empty = _split_bar(lifetime / peak_lifetime * 100 if peak_lifetime else 0, bar_width)
                text.append(f"  {name:<20}", style=FG)
                text.append("█" * filled, style=ORANGE)
                text.append("░" * empty, style=GUTTER)
                bits = [f"{_fmt_tokens_big(lifetime):>8}", _pct(lifetime, lifetime_sum)]
                if summary.peak_daily_tokens is not None:
                    bits.append(f"peak day {_fmt_tokens_big(summary.peak_daily_tokens)}")
                if summary.current_streak_days is not None:
                    bits.append(f"streak {summary.current_streak_days}d")
                if summary.longest_streak_days is not None:
                    bits.append(f"longest {summary.longest_streak_days}d")
                text.append("  " + "  ·  ".join(bits) + "\n", style=MUTED)
            text.append("\n")

    if total["total"] > 0:
        heading("THIS MACHINE — SESSION LOGS")
        note = "  Codex session logs on this machine · all buckets combined"
        if server is not None:
            note += f" · {_pct(total['total'], server[0])} of all-time"
        else:
            note += " · server history unavailable, showing this machine only"
        text.append(note + "\n\n", style=MUTED)
        text.append(f"  {_fmt_tokens_big(total['total'])} tokens", style=f"bold {FG}")
        text.append(f"   ({total['total']:,})\n\n", style=MUTED)
        row("input", _fmt_tokens_big(total["input"]), f"cached {_fmt_tokens_big(total['cached'])} · {_pct(total['cached'], total['input'])} of input")
        row("output", _fmt_tokens_big(total["output"]), f"reasoning {_fmt_tokens_big(total['reasoning'])} · {_pct(total['reasoning'], total['output'])} of output")
        if total["cache_write"]:
            row("cache write", _fmt_tokens_big(total["cache_write"]))
        row("sessions", f"{stats.sessions:,}", f"model calls {stats.requests:,}")
        row("active days", f"{len(stats.days):,}", f"since {stats.first_day}")
        text.append("\n")
        if server is None:
            local_daily = {
                date.fromisoformat(day): usage["total"] for day, usage in stats.days.items()
            }
            daily_section(local_daily)
        heading("BY MODEL — THIS MACHINE")
        models = sorted(stats.models.items(), key=lambda item: item[1]["total"], reverse=True)
        peak_model = models[0][1]["total"] if models else 1
        for name, usage in models[:8]:
            filled, empty = _split_bar(usage["total"] / peak_model * 100 if peak_model else 0, bar_width)
            text.append(f"  {name:<20}", style=FG)
            text.append("█" * filled, style=BLUE)
            text.append("░" * empty, style=GUTTER)
            text.append(f"  {_fmt_tokens_big(usage['total']):>8}  {_pct(usage['total'], total['total'])}\n", style=MUTED)
        if len(models) > 8:
            rest = sum(usage["total"] for _, usage in models[8:])
            text.append(f"  {'+ ' + str(len(models) - 8) + ' more':<20}", style=MUTED)
            text.append(" " * bar_width)
            text.append(f"  {_fmt_tokens_big(rest):>8}  {_pct(rest, total['total'])}\n", style=MUTED)
        text.append("\n  Local counts exclude deleted sessions. Server daily totals can lag a day.", style=MUTED)
    return text


class StatsScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back", show=True, priority=True),
        Binding("q", "back", "Back", show=False, priority=True),
        Binding("s", "back", "Back", show=False, priority=True),
    ]
    DEFAULT_CSS = """
    StatsScreen {
        background: #141414;
        color: #e1e1e1;
    }
    #stats-scroll {
        height: 1fr;
        scrollbar-color: #bb9af7;
    }
    #stats-body {
        padding: 1 3;
        width: 1fr;
    }
    """

    def __init__(self, quotas: dict[str, Quota] | None = None) -> None:
        super().__init__()
        self._quotas = quotas or {}
        self._stats: Stats | None = None
        self._quota_fingerprint: tuple | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="◆")
        with VerticalScroll(id="stats-scroll"):
            yield Static("scanning Codex sessions…", id="stats-body")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "slop"
        self.sub_title = "stats"
        self._load()
        # Server history may still be loading when the page opens; pick
        # it up (and later refreshes) without reopening the page.
        self.set_interval(5, self._render_body)

    def action_back(self) -> None:
        self.app.pop_screen()

    @work(thread=True, group="stats", exclusive=True)
    def _load(self) -> None:
        def progress(done: int, total: int) -> None:
            self.app.call_from_thread(self._on_progress, done, total)

        stats = collect_stats(progress=progress)
        self.app.call_from_thread(self._show, stats)

    def _on_progress(self, done: int, total: int) -> None:
        if self._stats is not None or not self.is_mounted:
            return
        self.query_one("#stats-body", Static).update(
            f"scanning Codex sessions… {done}/{total}\n\nFirst scan reads every saved session once; later visits only read new activity."
        )

    def _show(self, stats: Stats) -> None:
        self._stats = stats
        self._render_body()

    def _live_quotas(self) -> dict[str, Quota]:
        app = self.app
        if isinstance(app, SlopApp) and app.quotas:
            return dict(app.quotas)
        return self._quotas

    def _render_body(self) -> None:
        if self._stats is None or not self.is_mounted:
            return
        quotas = self._live_quotas()
        fingerprint = tuple(
            sorted(
                (name, quota.fetched_at, len(quota.daily))
                for name, quota in quotas.items()
                if quota is not None
            )
        ) + (self.size.width,)
        if fingerprint == self._quota_fingerprint:
            return
        self._quota_fingerprint = fingerprint
        self.query_one("#stats-body", Static).update(
            render_stats(self._stats, self.size.width - 6, quotas=quotas)
        )

    def on_resize(self) -> None:
        self._render_body()


class SlopApp(App[tuple[str, str, list[str]] | None]):
    TITLE = "slop"
    SUB_TITLE = "buckets"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen {
        background: #141414;
        color: #e1e1e1;
    }
    Header {
        background: #0c0c0c;
        color: #bb9af7;
        text-style: bold;
    }
    Footer {
        background: #0c0c0c;
        color: #c8c8c8;
    }
    #summary {
        height: 1;
        padding: 0 3;
        color: #6c6c6c;
        background: #0c0c0c;
        border-bottom: solid #333333;
    }
    #body {
        height: 1fr;
        layout: vertical;
    }
    #buckets {
        height: 1fr;
        background: #141414;
        scrollbar-color: #bb9af7;
        padding: 1 0 0 0;
    }
    ListView {
        background: #141414;
    }
    ListView:focus {
        background-tint: 0%;
    }
    ListView > ListItem.-highlight {
        background: #242424;
        color: #e1e1e1;
        text-style: none;
    }
    ListView:focus > ListItem.-highlight {
        background: #242424;
        color: #e1e1e1;
        text-style: none;
    }
    #empty {
        width: 100%;
        height: 1fr;
        content-align: center middle;
        color: #6c6c6c;
        display: none;
    }
    #calendar-view {
        height: 1fr;
        display: none;
        background: #141414;
        scrollbar-color: #bb9af7;
    }
    #calendar-detail {
        height: auto;
        padding: 1 2;
    }
    """
    BINDINGS = [
        Binding("enter", "launch", "Launch", show=True),
        Binding("s", "stats", "Stats", show=True),
        Binding("a", "add", "Add", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("n", "rename", "Rename", show=True),
        Binding("c", "calendar", "Calendar", show=True),
        Binding("l", "reauthorize", "Reauthorize", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def action_help_quit(self) -> None:
        self.exit()

    def __init__(self, *, reauth_prompted: set[str] | None = None, selected_name: str | None = None) -> None:
        super().__init__()
        self.register_theme(GROK_NIGHT)
        self.theme = "groknight"
        self.cfg: Config = ensure_config()
        self.quotas: dict[str, Quota] = {}
        self.loading: set[str] = set()
        self.view: str = "buckets"
        today = date.today()
        self._cal: tuple[int, int] = (today.year, today.month)
        self._quota_worker: Worker[None] | None = None
        self._reauth_prompted = reauth_prompted if reauth_prompted is not None else set()
        self._reauth_running = False
        self._initial_selection = selected_name

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, icon="◆")
        yield Static("", id="summary")
        with Vertical(id="body"):
            yield ListView(id="buckets")
            yield Static(
                "No buckets yet.\n\nPress [b]a[/] to add a Codex account.",
                id="empty",
                markup=True,
            )
            with VerticalScroll(id="calendar-view"):
                yield CalendarPane("", id="calendar-grid")
                yield Static("", id="calendar-detail")
        yield Footer()

    def on_mount(self) -> None:
        ensure_file_store()
        self._sync_subtitle()
        self.run_worker(self._boot(), exclusive=True)

    def _sync_subtitle(self) -> None:
        mode = "yolo"
        if self.view == "calendar":
            year, month = self._cal
            self.sub_title = f"calendar · {_calendar.month_name[month]} {year} · {mode}"
        else:
            self.sub_title = f"buckets · {mode}"

    async def _boot(self) -> None:
        if unmanaged_auth_exists():
            ident = identity_from_auth(auth_path())
            suggested = suggest_name(ident)
            title = "Name this existing Codex login"
            if ident.email:
                title = f"Found {ident.email}. Name this bucket"
            name = await self.push_screen_wait(NameModal(title, default=suggested))
            if name:
                try:
                    save_current(name)
                    self.notify(f"saved {name}")
                except StoreError as exc:
                    self.notify(str(exc), severity="error")
        self.reload()
        self.action_refresh()
        self.set_interval(QUOTA_REFRESH_INTERVAL, self.action_refresh)

    def _summary_text(self) -> str:
        profiles = list_profiles()
        if not profiles:
            return "no buckets"
        n = len(profiles)
        noun = "bucket" if n == 1 else "buckets"
        bits = [f"{n} {noun}"]
        empty = []
        ok = []
        for profile in profiles:
            q = self.quotas.get(profile.name)
            if q and q.ok and q.blocked:
                empty.append(profile.name)
            elif q and q.ok:
                ok.append(profile.name)
        if empty:
            bits.append("empty: " + ", ".join(empty))
        elif ok and len(ok) == n:
            bits.append("all ok")
        if self.loading:
            bits.append(f"fetching {len(self.loading)}…")
        bits.append("yolo")
        return "   ·   ".join(bits)

    def _update_summary(self) -> None:
        self.query_one("#summary", Static).update(self._summary_text())

    def _sync_view(self, *, has_profiles: bool) -> None:
        in_calendar = self.view == "calendar"
        self.query_one("#calendar-view", VerticalScroll).display = in_calendar
        self.query_one("#buckets", ListView).display = not in_calendar and has_profiles
        self.query_one("#empty", Static).display = not in_calendar and not has_profiles
        if in_calendar:
            self._render_calendar()
            try:
                if not self._modal_open():
                    self.query_one("#calendar-grid", CalendarPane).focus()
            except Exception:
                pass

    def _render_calendar(self) -> None:
        now = datetime.now().astimezone()
        year, month = self._cal
        events = collect_resets(self.quotas, now=now)
        self.query_one("#calendar-grid", CalendarPane).update(
            render_calendar(
                year,
                month,
                today=now.date(),
                events=events,
                totals=daily_totals(self.quotas),
                width=max(77, self.size.width - 4),
            )
        )
        self.query_one("#calendar-detail", Static).update(
            render_calendar_detail(
                year,
                month,
                quotas=self.quotas,
                events=events,
                now=now,
                loading=bool(self.loading),
            )
        )

    def action_calendar(self) -> None:
        if self._modal_open():
            return
        self.view = "buckets" if self.view == "calendar" else "calendar"
        self._sync_subtitle()
        self.reload()

    def action_prev_month(self) -> None:
        if self.view != "calendar" or self._modal_open():
            return
        self._cal = shift_month(*self._cal, -1)
        self._sync_subtitle()
        self._render_calendar()

    def action_next_month(self) -> None:
        if self.view != "calendar" or self._modal_open():
            return
        self._cal = shift_month(*self._cal, 1)
        self._sync_subtitle()
        self._render_calendar()

    def reload(self) -> None:
        profiles = list_profiles()
        lv = self.query_one("#buckets", ListView)
        current = self._initial_selection or current_name()
        keep = lv.index
        existing = {
            item.bucket_name: item
            for item in lv.children
            if isinstance(item, BucketItem)
        }
        names = [p.name for p in profiles]
        if set(existing) == set(names) and names:
            self._sync_view(has_profiles=True)
            for profile in profiles:
                existing[profile.name].set_state(
                    active=profile.active,
                    quota=self.quotas.get(profile.name),
                    loading=profile.name in self.loading,
                    email=profile.identity.email,
                    plan=profile.identity.plan,
                )
            self._update_summary()
            try:
                if self.view != "calendar" and not self._modal_open():
                    lv.focus()
            except Exception:
                pass
            return
        lv.clear()
        if not profiles:
            self._sync_view(has_profiles=False)
            self._update_summary()
            return
        self._sync_view(has_profiles=True)
        for profile in profiles:
            lv.append(
                BucketItem(
                    profile.name,
                    profile.active,
                    profile.identity.email,
                    profile.identity.plan,
                    self.quotas.get(profile.name),
                    profile.name in self.loading,
                )
            )
        lv.index = 0
        if keep is not None and keep < len(profiles):
            lv.index = keep
        elif current:
            for i, profile in enumerate(profiles):
                if profile.name == current:
                    lv.index = i
                    break
        self._initial_selection = None
        self._update_summary()
        try:
            if self.view != "calendar" and not self._modal_open():
                lv.focus()
        except Exception:
            pass

    def _selected_name(self) -> str | None:
        lv = self.query_one("#buckets", ListView)
        item = lv.highlighted_child
        if isinstance(item, BucketItem):
            return item.bucket_name
        return None

    def on_list_view_selected(self) -> None:
        self.action_launch()

    def action_refresh(self) -> None:
        # Cancelling a Textual thread worker does not stop its network requests.
        # Let a slow refresh finish before accepting another timer tick or `r`.
        if self._quota_worker is not None and not self._quota_worker.is_finished:
            return
        names = [p.name for p in list_profiles()]
        self.loading = set(names)
        self.reload()
        if names:
            self._quota_worker = self.fetch_quotas()

    @work(thread=True, group="quota", exclusive=True)
    def fetch_quotas(self) -> None:
        for name, quota in iter_quotas():
            self.call_from_thread(self._apply_quotas, {name: quota})
        self.call_from_thread(self._apply_quotas, {}, finished=True)

    def _apply_quotas(self, results: dict[str, Quota], *, finished: bool = False) -> None:
        self.quotas.update(results)
        self.loading.difference_update(results)
        if finished:
            self.loading.clear()
        self.reload()
        self.prompt_reauthorization()

    @work(group="reauth")
    async def prompt_reauthorization(self) -> None:
        if self._reauth_running:
            return
        self._reauth_running = True
        try:
            for name, quota in list(self.quotas.items()):
                if not quota.reauth_required or name in self._reauth_prompted:
                    continue
                # Do not replace a modal the user is already interacting with.
                import asyncio
                while self._modal_open():
                    await asyncio.sleep(0.2)
                if name not in {p.name for p in list_profiles()}:
                    continue
                self._reauth_prompted.add(name)
                ok = await self.push_screen_wait(ConfirmModal(
                    f"{name} needs a new login. Reauthorize with a device code?", "reauthorize"
                ))
                if ok:
                    self._reauthorize(name)
                    return
        finally:
            self._reauth_running = False

    def _reauthorize(self, name: str) -> None:
        # Finish Textual's event loop before a blocking, interactive login.
        # App.suspend() can leave the driver suspended when validation raises.
        self.exit(("reauth", name, []))

    def action_reauthorize(self) -> None:
        if self.view != "buckets" or self._modal_open() or self._reauth_running:
            return
        name = self._selected_name()
        if name:
            self._reauth_prompted.add(name)
            self._reauthorize(name)

    def action_stats(self) -> None:
        if self._modal_open():
            return
        self.push_screen(StatsScreen(dict(self.quotas)))

    def _modal_open(self) -> bool:
        return len(self.screen_stack) > 1

    def action_launch(self) -> None:
        if self.view != "buckets" or self._modal_open():
            return
        name = self._selected_name()
        if not name:
            return
        try:
            switch_to(name)
        except StoreError as exc:
            self.notify(str(exc), severity="error")
            return
        self.exit(("launch", name, []))

    @work
    async def action_add(self) -> None:
        if self.view != "buckets":
            return
        spec = await self.push_screen_wait(AddModal())
        if spec is None:
            return
        self.exit(("add", spec.name, [] if spec.device else ["--browser"]))

    @work
    async def action_delete(self) -> None:
        if self.view != "buckets":
            return
        name = self._selected_name()
        if not name:
            return
        if current_name() == name:
            self.notify("Switch away from this bucket before deleting it", severity="warning")
            return
        ok = await self.push_screen_wait(ConfirmModal(f"Delete bucket {name}?"))
        if not ok:
            return
        try:
            remove(name)
        except StoreError as exc:
            self.notify(str(exc), severity="error")
            return
        self.quotas.pop(name, None)
        self.notify(f"removed {name}")
        self.reload()

    @work
    async def action_rename(self) -> None:
        if self.view != "buckets":
            return
        name = self._selected_name()
        if not name:
            return
        new = await self.push_screen_wait(NameModal(f"Rename {name}", default=name))
        if not new or new == name:
            return
        try:
            rename(name, new)
        except StoreError as exc:
            self.notify(str(exc), severity="error")
            return
        if name in self.quotas:
            q = self.quotas.pop(name)
            q.name = new
            self.quotas[new] = q
        self.notify(f"{name} → {new}")
        self.reload()
