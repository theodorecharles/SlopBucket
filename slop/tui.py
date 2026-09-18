from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

from slop.config import Config, ensure as ensure_config, save as save_config
from slop.quota import Quota, Window, fetch_all, until_label
from slop.theme import GREEN, GROK_NIGHT, GUTTER, MUTED, RED, YELLOW
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
    extras: list[str] = [status]
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
            meters.mount(Static(f"[{MUTED}]press r to load usage[/]", markup=True))
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
    """
    BINDINGS = [
        Binding("enter", "launch", "Launch", show=True),
        Binding("s", "switch", "Switch", show=True),
        Binding("a", "add", "Add", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("n", "rename", "Rename", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("l", "reauthorize", "Reauthorize", show=True),
        Binding("f", "toggle_full", "Full access", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def action_help_quit(self) -> None:
        self.exit()

    def __init__(self) -> None:
        super().__init__()
        self.register_theme(GROK_NIGHT)
        self.theme = "groknight"
        self.cfg: Config = ensure_config()
        self.quotas: dict[str, Quota] = {}
        self.loading: set[str] = set()
        self._reauth_prompted: set[str] = set()
        self._reauth_running = False

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
        yield Footer()

    def on_mount(self) -> None:
        ensure_file_store()
        self._sync_subtitle()
        self.run_worker(self._boot(), exclusive=True)

    def _sync_subtitle(self) -> None:
        mode = "full access" if self.cfg.launch.full_access else "sandbox on"
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
        mode = "full access" if self.cfg.launch.full_access else "sandbox on"
        bits.append(mode)
        return "   ·   ".join(bits)

    def _update_summary(self) -> None:
        self.query_one("#summary", Static).update(self._summary_text())

    def reload(self) -> None:
        profiles = list_profiles()
        lv = self.query_one("#buckets", ListView)
        empty = self.query_one("#empty", Static)
        current = current_name()
        keep = lv.index
        existing = {
            item.bucket_name: item
            for item in lv.children
            if isinstance(item, BucketItem)
        }
        names = [p.name for p in profiles]
        if set(existing) == set(names) and names:
            empty.display = False
            lv.display = True
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
                lv.focus()
            except Exception:
                pass
            return
        lv.clear()
        if not profiles:
            empty.display = True
            lv.display = False
            self._update_summary()
            return
        empty.display = False
        lv.display = True
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
        self._update_summary()
        try:
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
        names = [p.name for p in list_profiles()]
        self.loading = set(names)
        self.reload()
        self.fetch_quotas()

    @work(thread=True, group="quota", exclusive=True)
    def fetch_quotas(self) -> None:
        results = fetch_all()
        self.call_from_thread(self._apply_quotas, results)

    def _apply_quotas(self, results: dict[str, Quota]) -> None:
        self.quotas.update(results)
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
        finally:
            self._reauth_running = False

    def _reauthorize(self, name: str) -> None:
        from slop.accounts import reauthorize_account
        from slop.rpc import RpcError
        try:
            with self.suspend():
                reauthorize_account(name)
        except (StoreError, RpcError) as exc:
            self.notify(str(exc), severity="error", timeout=10)
            return
        self.quotas.pop(name, None)
        self.notify(f"reauthorized {name}")
        self.action_refresh()

    def action_reauthorize(self) -> None:
        if self._modal_open() or self._reauth_running:
            return
        name = self._selected_name()
        if name:
            self._reauth_prompted.add(name)
            self._reauthorize(name)

    def action_switch(self) -> None:
        if self._modal_open():
            return
        name = self._selected_name()
        if not name:
            return
        try:
            switch_to(name)
        except StoreError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"switched to {name} · restart Codex if it's already running")
        self.reload()

    def _modal_open(self) -> bool:
        return len(self.screen_stack) > 1

    def action_launch(self) -> None:
        if self._modal_open():
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

    def action_toggle_full(self) -> None:
        self.cfg.launch.full_access = not self.cfg.launch.full_access
        save_config(self.cfg)
        self._sync_subtitle()
        state = "on" if self.cfg.launch.full_access else "off"
        self.notify(f"full access {state}")

    @work
    async def action_add(self) -> None:
        spec = await self.push_screen_wait(AddModal())
        if spec is None:
            return
        from slop.accounts import AddError, add_account

        with self.suspend():
            try:
                add_account(spec.name, device=spec.device)
            except AddError as exc:
                self.notify(str(exc), severity="error")
                return
            except StoreError as exc:
                self.notify(str(exc), severity="error")
                return
        self.notify(f"added {spec.name}")
        self.reload()
        self.action_refresh()

    @work
    async def action_delete(self) -> None:
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
