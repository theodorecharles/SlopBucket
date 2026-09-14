from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def config_path() -> Path:
    override = os.environ.get("SLOP_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else Path.home() / ".config"
    return root / "slop" / "config.toml"


@dataclass
class LaunchConfig:
    """How slop starts Codex after you pick a bucket."""

    full_access: bool = True
    bypass_hook_trust: bool = True
    bin: str = "codex"
    extra_args: list[str] = field(default_factory=list)


@dataclass
class Config:
    launch: LaunchConfig = field(default_factory=LaunchConfig)


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def default_text() -> str:
    return """# slop — Codex account buckets
#
# Launch flags map to the official Codex CLI:
#   full_access        -> --dangerously-bypass-approvals-and-sandbox
#   bypass_hook_trust  -> --dangerously-bypass-hook-trust
# Set either to false if you want Codex prompts/sandbox back.

[launch]
full_access = true
bypass_hook_trust = true
bin = "codex"
extra_args = []
"""


def load() -> Config:
    path = config_path()
    env_full = os.environ.get("SLOP_FULL_ACCESS")
    if not path.exists():
        cfg = Config()
        if env_full is not None:
            cfg.launch.full_access = _as_bool(env_full, True)
        return cfg
    data = tomllib.loads(path.read_text())
    raw = data.get("launch") or {}
    launch = LaunchConfig(
        full_access=_as_bool(raw.get("full_access"), True),
        bypass_hook_trust=_as_bool(raw.get("bypass_hook_trust"), True),
        bin=str(raw.get("bin") or "codex"),
        extra_args=[str(a) for a in (raw.get("extra_args") or [])],
    )
    if env_full is not None:
        launch.full_access = _as_bool(env_full, launch.full_access)
    return Config(launch=launch)


def save(cfg: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = ", ".join(repr(a) for a in cfg.launch.extra_args)
    path.write_text(
        "# slop — Codex account buckets\n"
        "#\n"
        "# Launch flags map to the official Codex CLI:\n"
        "#   full_access        -> --dangerously-bypass-approvals-and-sandbox\n"
        "#   bypass_hook_trust  -> --dangerously-bypass-hook-trust\n"
        "# Set either to false if you want Codex prompts/sandbox back.\n"
        "\n"
        "[launch]\n"
        f"full_access = {str(cfg.launch.full_access).lower()}\n"
        f"bypass_hook_trust = {str(cfg.launch.bypass_hook_trust).lower()}\n"
        f"bin = {cfg.launch.bin!r}\n"
        f"extra_args = [{extra}]\n"
    )
    path.chmod(0o600)
    return path


def ensure() -> Config:
    path = config_path()
    if not path.exists():
        save(Config())
    return load()


def launch_command(cfg: Config | None = None, extra: list[str] | None = None) -> list[str]:
    cfg = cfg or load()
    cmd = [cfg.launch.bin]
    if cfg.launch.full_access:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    if cfg.launch.bypass_hook_trust:
        cmd.append("--dangerously-bypass-hook-trust")
    cmd.extend(cfg.launch.extra_args)
    if extra:
        if extra[:1] == ["--"]:
            extra = extra[1:]
        cmd.extend(extra)
    return cmd
