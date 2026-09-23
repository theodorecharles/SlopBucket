from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from slop import __version__
from slop.config import config_path, ensure as ensure_config, launch_command, load, save
from slop.quota import banked_resets_label, fetch_all, fetch_quota, until_label
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
    switch_to,
    unmanaged_auth_exists,
)


def _die(message: str, code: int = 1) -> None:
    print(f"slop: {message}", file=sys.stderr)
    raise SystemExit(code)


def _print_profile_line(name: str, active: bool, quota=None) -> None:
    mark = "*" if active else " "
    ident = next((p.identity for p in list_profiles() if p.name == name), None)
    email = (quota.email if quota and quota.email else (ident.email if ident else None)) or ""
    plan = (quota.plan if quota and quota.plan else (ident.plan if ident else None)) or ""
    bits = [f"{mark} {name:<16}", email, plan]
    if quota and quota.ok:
        for window in quota.windows:
            bits.append(f"{window.label} {window.remaining_percent:.0f}% left")
        bits.append(banked_resets_label(quota.banked_resets))
        if quota.blocked:
            bits.append("EMPTY")
    elif quota and not quota.ok:
        bits.append(quota.error or "error")
    print("  ".join(b for b in bits if b))


def cmd_list(args: argparse.Namespace) -> int:
    ensure_file_store()
    profiles = list_profiles()
    if not profiles and unmanaged_auth_exists():
        ident = identity_from_auth(auth_path())
        print(f"unmanaged login: {ident.email or 'unknown'}  (run `slop` to name it)")
        return 0
    if not profiles:
        print("No buckets. Run `slop` and press a to add one.")
        return 0
    quotas = fetch_all(profiles) if args.quota else {}
    if args.json:
        payload = []
        for profile in profiles:
            q = quotas.get(profile.name)
            payload.append(
                {
                    "name": profile.name,
                    "active": profile.active,
                    "email": profile.identity.email,
                    "plan": profile.identity.plan,
                    "quota": None
                    if q is None
                    else {
                        "ok": q.ok,
                        "error": q.error,
                        "reauth_required": q.reauth_required,
                        "blocked": q.blocked,
                        "credits": q.credits,
                        "banked_resets": q.banked_resets,
                        "windows": [
                            {
                                "label": w.label,
                                "remaining_percent": w.remaining_percent,
                                "resets_in": until_label(w.resets_at),
                            }
                            for w in (q.windows if q else [])
                        ],
                    },
                }
            )
        json.dump(payload, sys.stdout, indent=2)
        print()
        return 0
    for profile in profiles:
        _print_profile_line(profile.name, profile.active, quotas.get(profile.name))
    return 0


def cmd_whoami(_args: argparse.Namespace) -> int:
    ensure_file_store()
    name = current_name()
    if name is None:
        if unmanaged_auth_exists():
            ident = identity_from_auth(auth_path())
            print(f"unmanaged  {ident.email or 'unknown'}")
            return 0
        _die("no active bucket")
    ident = next(p.identity for p in list_profiles() if p.name == name)
    print(f"{name}  {ident.email or ''}  {ident.plan or ''}")
    return 0


def cmd_use(args: argparse.Namespace) -> int:
    ensure_file_store()
    try:
        switch_to(args.name)
    except StoreError as exc:
        _die(str(exc))
    print(f"switched to {args.name}")
    print("restart Codex if a session is already running")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    from slop.accounts import AddError, add_account

    ensure_file_store()
    try:
        add_account(args.name, device=not args.browser)
    except (AddError, StoreError) as exc:
        _die(str(exc))
    print(f"added and switched to {args.name}")
    return 0


def cmd_save(args: argparse.Namespace) -> int:
    ensure_file_store()
    try:
        save_current(args.name, overwrite=args.force)
    except StoreError as exc:
        _die(str(exc))
    print(f"saved {args.name}")
    return 0


def cmd_reauth(args: argparse.Namespace) -> int:
    from slop.accounts import reauthorize_account
    from slop.rpc import RpcError
    try:
        reauthorize_account(args.name, device=not args.browser)
    except (StoreError, RpcError) as exc:
        _die(str(exc))
    print(f"reauthorized {args.name}")
    return 0


def cmd_refresh_tokens(args: argparse.Namespace) -> int:
    from slop.refresh import run_service
    return run_service(interval=args.interval, once=args.once, force=args.force)


def _check_interval(value: str) -> float:
    import math
    number = float(value)
    if not math.isfinite(number) or number < 30:
        raise argparse.ArgumentTypeError("interval must be at least 30 seconds")
    return number


def cmd_rm(args: argparse.Namespace) -> int:
    try:
        remove(args.name)
    except StoreError as exc:
        _die(str(exc))
    print(f"removed {args.name}")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    try:
        rename(args.old, args.new)
    except StoreError as exc:
        _die(str(exc))
    print(f"{args.old} → {args.new}")
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    ensure_file_store()
    names = [args.name] if args.name else [p.name for p in list_profiles()]
    if not names:
        _die("no buckets")
    for name in names:
        q = fetch_quota(name)
        _print_profile_line(name, current_name() == name, q)
        if q.ok:
            for window in q.windows:
                print(
                    f"    {window.label:4}  {window.remaining_percent:5.0f}% left"
                    + (f"  resets {until_label(window.resets_at)}" if window.resets_at else "")
                )
            if q.credits is not None:
                print(f"    credits {q.credits:.2f}")
        else:
            print(f"    {q.error}")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    from slop.store import auth_is_managed, config_toml_path

    ensure_file_store()
    print(f"slop        {__version__}")
    print(f"config      {config_path()}")
    cfg = load()
    print(f"full_access {cfg.launch.full_access}")
    print(f"codex bin   {shutil.which(cfg.launch.bin) or 'NOT FOUND'}")
    print(f"auth        {auth_path()}  managed={auth_is_managed()}")
    print(f"codex cfg   {config_toml_path()}")
    print(f"active      {current_name() or 'none'}")
    print(f"buckets     {', '.join(p.name for p in list_profiles()) or 'none'}")
    print("launch      " + " ".join(launch_command(cfg)))
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = ensure_config()
    if args.set is None:
        print(f"path {config_path()}")
        print(f"launch.full_access = {cfg.launch.full_access}")
        print(f"launch.bypass_hook_trust = {cfg.launch.bypass_hook_trust}")
        print(f"launch.bin = {cfg.launch.bin}")
        print(f"launch.extra_args = {cfg.launch.extra_args}")
        return 0
    key, value = args.set
    if key in {"launch.full_access", "full_access"}:
        cfg.launch.full_access = value.lower() in {"1", "true", "yes", "on"}
    elif key in {"launch.bypass_hook_trust", "bypass_hook_trust"}:
        cfg.launch.bypass_hook_trust = value.lower() in {"1", "true", "yes", "on"}
    elif key in {"launch.bin", "bin"}:
        cfg.launch.bin = value
    else:
        _die(f"unknown config key {key}")
    save(cfg)
    print(f"set {key} = {value}")
    return 0


def _exec_codex(extra: list[str] | None = None) -> int:
    cfg = load()
    cmd = launch_command(cfg, extra)
    binary = shutil.which(cmd[0])
    if binary is None:
        _die(f"cannot find {cmd[0]} on PATH")
    cmd[0] = binary
    print("launching: " + " ".join(cmd), file=sys.stderr)
    os.execvp(cmd[0], cmd)
    return 1


def cmd_launch(args: argparse.Namespace) -> int:
    ensure_file_store()
    if args.name:
        try:
            switch_to(args.name)
        except StoreError as exc:
            _die(str(exc))
    elif current_name() is None and unmanaged_auth_exists():
        pass
    elif current_name() is None:
        _die("no active bucket")
    return _exec_codex(args.codex_args)


def cmd_tui(_args: argparse.Namespace) -> int:
    from slop.accounts import add_account, reauthorize_account
    from slop.rpc import RpcError
    from slop.tui import SlopApp

    ensure_file_store()
    prompted: set[str] = set()
    selected_name = None
    while True:
        result = SlopApp(reauth_prompted=prompted, selected_name=selected_name).run()
        if not isinstance(result, tuple) or not result:
            return 0
        action, name, extra = result
        if action == "launch":
            try:
                switch_to(name)
            except StoreError as exc:
                _die(str(exc))
            return _exec_codex(extra)
        if action not in {"reauth", "add"}:
            return 0
        # The previous app and its workers have shut down. Login owns the
        # terminal until it finishes, then we create a fresh dashboard.
        selected_name = name
        prompted.add(name)
        try:
            login = reauthorize_account if action == "reauth" else add_account
            login(name, device="--browser" not in extra)
        except (StoreError, RpcError, OSError) as exc:
            print(f"\nslop: {exc}", file=sys.stderr, flush=True)
            try:
                input("Press Enter to return to your buckets (l retries login). ")
            except (KeyboardInterrupt, EOFError):
                return 0
        except KeyboardInterrupt:
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slop",
        description="Dashboard and launcher for multiple Codex / ChatGPT accounts.",
    )
    parser.add_argument("--version", action="version", version=f"slop {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    tui = sub.add_parser("tui", help="open the TUI (default)")
    tui.set_defaults(func=cmd_tui)

    p = sub.add_parser("list", help="list buckets")
    p.add_argument("--quota", action="store_true", help="also fetch live usage")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("whoami", help="show the active bucket")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("use", help="switch the active bucket without launching")
    p.add_argument("name")
    p.set_defaults(func=cmd_use)

    p = sub.add_parser("add", help="log in a new account as a named bucket")
    p.add_argument("name")
    p.add_argument("--browser", action="store_true", help="use local-browser login instead of device code")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("reauth", help="reauthorize an existing bucket without deleting it")
    p.add_argument("name")
    p.add_argument("--browser", action="store_true")
    p.set_defaults(func=cmd_reauth)

    p = sub.add_parser("refresh-tokens", help="keep saved account tokens renewed (for PM2)")
    p.add_argument("--once", action="store_true", help="check every bucket once and exit")
    p.add_argument("--force", action="store_true", help="request renewal even when tokens are fresh")
    p.add_argument("--interval", type=_check_interval, default=300, help="check interval in seconds (default: 300)")
    p.set_defaults(func=cmd_refresh_tokens)

    p = sub.add_parser("save", help="save the current Codex login as a named bucket")
    p.add_argument("name")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("rm", help="delete a saved bucket")
    p.add_argument("name")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("rename", help="rename a bucket")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("quota", help="print live usage")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("launch", help="switch (optional) and exec Codex")
    p.add_argument("name", nargs="?")
    p.add_argument("codex_args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("config", help="show or set slop config")
    p.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"))
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("doctor", help="print local health")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        return cmd_tui(args)
    return args.func(args)
