"""All-time token usage from local Codex session logs.

Codex writes one JSONL rollout per thread under ``~/.codex/sessions``.
Each ``token_count`` event carries a cumulative ``total_token_usage`` for
its thread, so per-event consumption is the delta between successive
totals. Summing deltas (instead of ``last_token_usage``) ignores the
duplicate cumulative events Codex emits and never double-counts.

Rollouts can be gigabytes, so results are cached per file with a byte
offset: later scans only read what was appended since the last scan.
The cache holds counts only, never conversation content.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from slop.store import codex_home

CACHE_VERSION = 1
USAGE_KEYS = ("input", "cached", "cache_write", "output", "reasoning", "total")
_RAW_KEYS = {
    "input": "input_tokens",
    "cached": "cached_input_tokens",
    "cache_write": "cache_write_input_tokens",
    "output": "output_tokens",
    "reasoning": "reasoning_output_tokens",
    "total": "total_tokens",
}

Progress = Callable[[int, int], None]


def _zero() -> dict[str, int]:
    return dict.fromkeys(USAGE_KEYS, 0)


def _usage(raw: object) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    out = _zero()
    for key, raw_key in _RAW_KEYS.items():
        value = raw.get(raw_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = int(value)
    if out["total"] == 0 and out["input"] == 0 and out["output"] == 0:
        return None
    return out


def _add(dst: dict[str, int], delta: dict[str, int]) -> None:
    for key in USAGE_KEYS:
        dst[key] += delta[key]


def _delta(total: dict[str, int], prev: dict[str, int] | None, last: dict[str, int] | None) -> dict[str, int]:
    if prev is None or total["total"] < prev["total"]:
        # First event seen, or the cumulative counter reset: the event's
        # own usage is the trustworthy delta. For a genuine thread start
        # last == total, so nothing is lost.
        return dict(last) if last is not None else dict(total)
    return {key: max(0, total[key] - prev[key]) for key in USAGE_KEYS}


def _local_day(timestamp: object) -> str | None:
    if not isinstance(timestamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.date().isoformat()


def fmt_tokens(value: int) -> str:
    value = int(value)
    magnitude = abs(value)
    if magnitude >= 1_000_000_000_000:
        return f"{value / 1e12:.2f}T"
    if magnitude >= 1_000_000_000:
        return f"{value / 1e9:.2f}B"
    if magnitude >= 1_000_000:
        return f"{value / 1e6:.1f}M"
    if magnitude >= 10_000:
        return f"{value / 1e3:.1f}k"
    return f"{value:,}"


@dataclass
class Stats:
    total: dict[str, int] = field(default_factory=_zero)
    days: dict[str, dict[str, int]] = field(default_factory=dict)
    models: dict[str, dict[str, int]] = field(default_factory=dict)
    requests: int = 0
    sessions: int = 0
    files: int = 0

    @property
    def first_day(self) -> str | None:
        return min(self.days) if self.days else None

    @property
    def last_day(self) -> str | None:
        return max(self.days) if self.days else None

    def day_total(self, day: str) -> int:
        usage = self.days.get(day)
        return usage["total"] if usage else 0


def default_session_dirs() -> list[Path]:
    home = codex_home()
    return [home / "sessions", home / "archived_sessions"]


def default_cache_path() -> Path:
    override = os.environ.get("SLOP_STATS_CACHE")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    root = Path(xdg) if xdg else Path.home() / ".cache"
    return root / "slop" / "stats-cache.json"


def _fresh_state() -> dict:
    return {
        "ino": None,
        "size": 0,
        "mtime_ns": 0,
        "head": "",
        "offset": 0,
        "prev": None,
        "model": None,
        "history_start": None,
        "meta_seen": False,
        "total": _zero(),
        "days": {},
        "models": {},
        "requests": 0,
    }


def _head_hash(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return hashlib.sha1(handle.readline()).hexdigest()
    except OSError:
        return ""


def _apply_token_event(state: dict, row: dict) -> None:
    payload = row.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return
    info = payload.get("info")
    if not isinstance(info, dict):
        return
    total = _usage(info.get("total_token_usage"))
    if total is None:
        return
    ordinal = row.get("ordinal")
    history_start = state["history_start"]
    if (
        isinstance(ordinal, (int, float))
        and isinstance(history_start, (int, float))
        and ordinal < history_start
    ):
        # Event copied from the parent thread when this one forked.
        state["prev"] = total
        return
    delta = _delta(total, state["prev"], _usage(info.get("last_token_usage")))
    state["prev"] = total
    if delta["total"] <= 0:
        return
    day = _local_day(row.get("timestamp"))
    if day is None:
        return
    state["requests"] += 1
    _add(state["total"], delta)
    _add(state["days"].setdefault(day, _zero()), delta)
    _add(state["models"].setdefault(state["model"] or "unknown", _zero()), delta)


def _scan_file(path: Path, state: dict) -> dict:
    try:
        handle = path.open("rb")
    except OSError:
        return state
    with handle:
        handle.seek(state["offset"])
        while True:
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                # EOF, or a writer's partial line: leave it for next scan.
                break
            state["offset"] += len(line)
            if b"token_count" in line and b"event_msg" in line:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    _apply_token_event(state, row)
            elif b"turn_context" in line and b'"model"' in line:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("type") == "turn_context":
                    payload = row.get("payload")
                    if isinstance(payload, dict) and isinstance(payload.get("model"), str):
                        state["model"] = payload["model"]
            elif not state["meta_seen"] and b"session_meta" in line:
                state["meta_seen"] = True
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("type") == "session_meta":
                    payload = row.get("payload")
                    if isinstance(payload, dict):
                        start = payload.get("subagent_history_start_ordinal")
                        if isinstance(start, (int, float)):
                            state["history_start"] = start
    return state


def _load_cache(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _save_cache(path: Path, files: dict[str, dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": files}))
        tmp.replace(path)
    except OSError:
        pass


def collect_stats(
    *,
    session_dirs: list[Path] | None = None,
    cache_path: Path | None = None,
    progress: Progress | None = None,
) -> Stats:
    dirs = session_dirs if session_dirs is not None else default_session_dirs()
    cache_file = cache_path if cache_path is not None else default_cache_path()
    paths: list[Path] = []
    for folder in dirs:
        if folder.is_dir():
            paths.extend(sorted(folder.rglob("*.jsonl")))
    cached = _load_cache(cache_file)
    states: dict[str, dict] = {}
    stats = Stats(files=len(paths))
    for index, path in enumerate(paths, start=1):
        if progress is not None:
            progress(index, len(paths))
        key = str(path)
        try:
            info = path.stat()
        except OSError:
            continue
        state = cached.get(key)
        if not isinstance(state, dict) or "offset" not in state:
            state = _fresh_state()
        unchanged = (
            state.get("ino") == info.st_ino
            and state.get("size") == info.st_size
            and state.get("mtime_ns") == info.st_mtime_ns
        )
        if not unchanged:
            reusable = (
                state.get("ino") == info.st_ino
                and isinstance(state.get("offset"), int)
                and 0 <= state["offset"] <= info.st_size
                and state.get("head") == _head_hash(path)
            )
            if not reusable:
                state = _fresh_state()
                state["head"] = _head_hash(path)
            state = _scan_file(path, state)
            state["ino"] = info.st_ino
            # Re-stat: the file may have grown while scanning. Recording
            # the pre-scan size would make the next scan re-read the tail
            # from the saved offset, which stays correct.
            state["size"] = info.st_size
            state["mtime_ns"] = info.st_mtime_ns
        states[key] = state
        if state["requests"]:
            stats.sessions += 1
        stats.requests += state["requests"]
        _add(stats.total, state["total"])
        for day, usage in state["days"].items():
            _add(stats.days.setdefault(day, _zero()), usage)
        for model, usage in state["models"].items():
            _add(stats.models.setdefault(model, _zero()), usage)
    _save_cache(cache_file, states)
    return stats
