from __future__ import annotations

from dataclasses import dataclass

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static

from slop.config import Config, ensure as ensure_config, save as save_config
from slop.quota import Quota, fetch_all, until_label
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


def _bar(remaining: float, width: int = 18) -> str:
    remaining = max(0.0, min(100.0, remaining))
    filled = int(round((remaining / 100.0) * width))
    filled = min(width, max(0, filled))
    return "█" * filled + "░" * (width - filled)


def _bar_style(remaining: float) -> str:
    if remaining <= 0:
        return "red"
    if remaining < 20:
        return "red"
    if remaining < 40:
        return "yellow"
    return "green"


def _fmt_credits(value: float | None) -> str:
    if value is None:
        return ""
    if value >= 10:
        return f"credits {value:.0f}"
    return f"credits {value:.2f}"


def render_card(name: str, active: bool, ident_email: str | None, ident_plan: str | None, quota: Quota | None, loading: bool) -> str:
    badge = " [bold #c6b26a]ACTIVE[/]" if active else ""
    email = (quota.email if quota and quota.email else ident_email) or "unknown"
    plan = (quota.plan if quota and quota.plan else ident_plan) or ""
    header = f"[bold]{name}[/]{badge}   [dim]{email}[/]  {plan}"
    if loading and quota is None:
        return header + "\n[dim]fetching usage…[/]"
    if quota is None:
        return header + "\n[dim]no usage yet[/]"
    if not quota.ok:
        return header + f"\n[red]{quota.error or 'usage unavailable'}[/]"
    lines = [header]
    if quota.windows:
        for window in quota.windows:
            style = _bar_style(window.remaining_percent)
            resets = until_label(window.resets_at)
            left = f"{window.remaining_percent:.0f}% left"
            extra = f"  resets {resets}" if resets else ""
            lines.append(
                f"[{style}]{_bar(window.remaining_percent)}[/]  {window.label}  {left}{extra}"
            )
    else:
        lines.append("[dim]no rate-limit windows[/]")
    status_bits = []
    if quota.blocked:
        status_bits.append("[red]EMPTY[/]")
    elif any(w.remaining_percent < 20 for w in quota.windows):
        status_bits.append("[yellow]LOW[/]")
    else:
        status_bits.append("[green]OK[/]")
    credits = _fmt_credits(quota.credits)
    if credits:
        status_bits.append(f"[dim]{credits}[/]")
    lines.append(" · ".join(status_bits))
    return "\n".join(lines)


def render_detail(name: str, quota: Quota | None, loading: bool, ident) -> str:
    lines = [f"[bold #c6b26a]{name}[/]"]
    email = ident.email or (quota.email if quota else None)
    plan = ident.plan or (quota.plan if quota else None)
    if ident.name:
        lines.append(ident.name)
    if email:
        lines.append(email)
    if plan:
        lines.append(f"plan {plan}")
    if ident.account_id:
        lines.append(f"[dim]{ident.account_id}[/]")
    lines.append("")
    if loading and quota is None:
        lines.append("[dim]fetching usage…[/]")
        return "\n".join(lines)
    if quota is None:
        lines.append("[dim]press r to refresh usage[/]")
        return "\n".join(lines)
    if not quota.ok:
        lines.append(f"[red]{quota.error}[/]")
        return "\n".join(lines)
    if quota.blocked:
        lines.append("[red]included usage is exhausted[/]")
    if quota.ordinary_allowed is False:
        lines.append("[dim]ordinaryUsageAllowed = false[/]")
    for window in quota.windows:
        style = _bar_style(window.remaining_percent)
        resets = until_label(window.resets_at)
        clock = ""
        from slop.quota import reset_clock

        clock_s = reset_clock(window.resets_at)
        bits = [f"{window.remaining_percent:.0f}% left"]
        if resets:
            bits.append(f"in {resets}")
        if clock_s:
            bits.append(clock_s)
        lines.append(f"{window.label}")
        lines.append(f"[{style}]{_bar(window.remaining_percent, 22)}[/]")
        lines.append("[dim]" + " · ".join(bits) + "[/]")
        lines.append("")
    if quota.credits is not None:
        lines.append(_fmt_credits(quota.credits))
    for label, windows in quota.extra:
        if not windows:
            continue
        lines.append("")
        lines.append(f"[dim]{label}[/]")
        for window in windows:
            lines.append(
                f"  {window.label}  {window.remaining_percent:.0f}% left"
                + (f"  {until_label(window.resets_at)}" if window.resets_at else "")
            )
    lines.append("")
    lines.append("[dim]enter launches Codex with this bucket[/]")
    lines.append("[dim]s switches without launching[/]")
    return "\n".join(lines)


class BucketItem(ListItem):
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
        yield Static(self._markup(), markup=True)

    def _markup(self) -> str:
        return render_card(
            self.bucket_name, self._active, self._email, self._plan, self._quota, self._loading
        )

    def set_state(self, *, active: bool, quota: Quota | None, loading: bool) -> None:
        self._active = active
        self._quota = quota
        self._loading = loading
        body = self.query_one(Static)
        body.update(self._markup())


@dataclass(frozen=True)
class AddSpec:
    name: str
    device: bool


class NameModal(ModalScreen[str | None]):
    DEFAULT_CSS = """
    NameModal { align: center middle; }
    #dialog {
        width: 56;
        height: auto;
        padding: 1 2;
        border: tall #c6b26a;
        background: #1c1914;
    }
    Input { margin: 1 0; }
    Button { width: 1fr; }
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
            with Horizontal():
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _submit(self) -> None:
        value = self.query_one(Input).value.strip()
        self.dismiss(value or None)

    def on_input_submitted(self) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class AddModal(ModalScreen[AddSpec | None]):
    DEFAULT_CSS = """
    AddModal { align: center middle; }
    #dialog {
        width: 62;
        height: auto;
        padding: 1 2;
        border: tall #c6b26a;
        background: #1c1914;
    }
    Input { margin: 1 0; }
    Button { width: 1fr; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Add a Codex bucket")
            yield Label("[dim]Logs in a new account. Saved buckets are not logged out.[/]", markup=True)
            yield Input(placeholder="name, e.g. allie", id="name")
            with Horizontal():
                yield Button("Browser login", variant="primary", id="browser")
                yield Button("Device code", id="device")
            yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _spec(self, device: bool) -> AddSpec | None:
        name = self.query_one(Input).value.strip()
        if not name:
            self.app.notify("Name this bucket first", severity="warning")
            return None
        return AddSpec(name=name, device=device)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        spec = self._spec(device=event.button.id == "device")
        if spec:
            self.dismiss(spec)

    def key_escape(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    #dialog {
        width: 56;
        height: auto;
        padding: 1 2;
        border: tall #d45c4a;
        background: #1c1914;
    }
    Button { width: 1fr; }
    """

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._question)
            with Horizontal():
                yield Button("Delete", variant="error", id="yes")
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


class SlopApp(App[tuple[str, str, list[str]] | None]):
    TITLE = "slop"
    SUB_TITLE = "buckets"
    CSS = """
    Screen {
        background: #14120e;
        color: #e8e0d0;
    }
    Header {
        background: #1c1914;
        color: #c6b26a;
        text-style: bold;
    }
    Footer {
        background: #1c1914;
    }
    #body {
        height: 1fr;
    }
    #buckets {
        width: 3fr;
        background: #14120e;
        scrollbar-color: #c6b26a;
    }
    #detail {
        width: 2fr;
        padding: 1 2;
        border-left: tall #2a261c;
        background: #18150f;
    }
    ListView {
        padding: 1 0;
    }
    ListItem {
        height: auto;
        margin: 0 1 1 1;
        padding: 1 2;
        background: #1a1712;
        border: tall #2a261c;
    }
    ListItem.--highlight {
        background: #2a2418;
        border: tall #c6b26a;
    }
    #empty {
        width: 100%;
        height: 1fr;
        content-align: center middle;
        color: #8a8070;
    }
    """
    BINDINGS = [
        Binding("enter", "launch", "Launch", show=True, priority=True),
        Binding("s", "switch", "Switch", show=True),
        Binding("a", "add", "Add", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("n", "rename", "Rename", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("f", "toggle_full", "Full access", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg: Config = ensure_config()
        self.quotas: dict[str, Quota] = {}
        self.loading: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield ListView(id="buckets")
            yield Static("", id="detail", markup=True)
        yield Footer()
        yield Static(
            "No buckets yet.\nPress [b]a[/] to add a Codex account.",
            id="empty",
            markup=True,
        )

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

    def reload(self) -> None:
        profiles = list_profiles()
        lv = self.query_one("#buckets", ListView)
        empty = self.query_one("#empty", Static)
        current = current_name()
        keep = lv.index
        lv.clear()
        if not profiles:
            empty.display = True
            self.query_one("#detail", Static).update("No buckets.")
            return
        empty.display = False
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
        if keep is not None and keep < len(profiles):
            lv.index = keep
        elif current:
            for i, profile in enumerate(profiles):
                if profile.name == current:
                    lv.index = i
                    break
        self._refresh_detail()
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

    def _refresh_detail(self) -> None:
        name = self._selected_name()
        panel = self.query_one("#detail", Static)
        if not name:
            panel.update("")
            return
        ident = next((p.identity for p in list_profiles() if p.name == name), None)
        from slop.store import Identity

        panel.update(
            render_detail(
                name,
                self.quotas.get(name),
                name in self.loading,
                ident or Identity(),
            )
        )

    def on_list_view_highlighted(self) -> None:
        self._refresh_detail()

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

    def action_switch(self) -> None:
        name = self._selected_name()
        if not name:
            self.notify("No bucket selected", severity="warning")
            return
        try:
            switch_to(name)
        except StoreError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"switched to {name} · restart Codex if it's already running")
        self.reload()

    def action_launch(self) -> None:
        name = self._selected_name()
        if not name:
            self.notify("No bucket selected", severity="warning")
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
