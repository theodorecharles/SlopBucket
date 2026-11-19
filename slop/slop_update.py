"""Check GitHub for SlopBucket updates and install them before launch.

Mirrors the Codex updater in codex_update.py: the check runs in the
background while the dashboard is open, and any available update is
installed at the Codex launch boundary. The running process keeps using
the code it started with; the update takes effect next time slop runs.
Failures warn and never block a launch.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from slop import __version__

NETWORK_TIMEOUT = 4
CHECK_WAIT_TIMEOUT = 10
CHECK_MAX_AGE = 300
CHECK_INTERVAL = 6 * 3600
UPDATE_TIMEOUT = 180
LOCK_WAIT_TIMEOUT = UPDATE_TIMEOUT + 10
MAX_METADATA_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
REPO = os.environ.get("SLOP_REPO") or "theodorecharles/SlopBucket"
INIT_URL = f"https://raw.githubusercontent.com/{REPO}/main/slop/__init__.py"
ARCHIVE_URL = f"https://github.com/{REPO}/archive/refs/heads/main.tar.gz"


class SlopUpdateError(Exception):
    """A fixed, safe message that may be shown without exposing diagnostics."""


@dataclass(frozen=True)
class _Result:
    latest: str | None = None
    warning: str | None = None
    silent: bool = False


@dataclass
class SlopUpdateCheck:
    _started_at: float = field(default_factory=time.monotonic)
    _done: threading.Event = field(default_factory=threading.Event)
    _result: _Result | None = None


def _version_tuple(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        return None
    version = tuple(int(part) for part in value.split("."))
    return version if version != (0, 0, 0) else None


def _state_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not state.is_absolute():
        state = Path.home() / ".local" / "state"
    return state / "slop"


def _install_root() -> Path | None:
    """The uv-tool install containing this package, or None for checkouts."""
    parts = Path(__file__).resolve().parts
    for i in range(len(parts) - 2):
        if parts[i:i + 3] == ("uv", "tools", "slop"):
            return Path(*parts[:i + 3])
    return None


def _installed_version(root: Path) -> str | None:
    """Version of the slop package currently on disk under a uv-tool root."""
    for init in sorted(root.glob("lib/python*/site-packages/slop/__init__.py")):
        try:
            match = re.search(
                r'^__version__ = "([^"]+)"', init.read_text()[:MAX_METADATA_BYTES], re.MULTILINE
            )
        except OSError:
            continue
        if match:
            return match.group(1)
    return None


def _fetch_latest() -> str:
    request = urllib.request.Request(INIT_URL, headers={"User-Agent": "SlopBucket"})
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
        raw = response.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise SlopUpdateError("SlopBucket update metadata exceeded the size limit")
    match = re.search(rb'^__version__ = "([^"]+)"', raw, re.MULTILINE)
    version = match.group(1).decode() if match else None
    if _version_tuple(version) is None:
        raise SlopUpdateError("SlopBucket update metadata did not contain a stable version")
    return version


def _read_state() -> dict:
    try:
        state = json.loads((_state_dir() / "slop-update-check.json").read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(state: dict) -> None:
    try:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "slop-update-check.json"
        path.write_text(json.dumps(state))
        path.chmod(0o600)
    except OSError:
        pass


def _fresh(state: dict, key: str) -> bool:
    when = state.get(key)
    return isinstance(when, (int, float)) and time.time() - when < CHECK_INTERVAL


def start_slop_check() -> SlopUpdateCheck:
    """Immediately start a read-only check without delaying the account picker."""
    check = SlopUpdateCheck()

    def work() -> None:
        try:
            if _install_root() is None:
                # Development checkouts and other installs manage themselves.
                check._result = _Result(silent=True)
                return
            state = _read_state()
            if _fresh(state, "checked_at") and _version_tuple(state.get("latest")) is not None:
                check._result = _Result(latest=state["latest"])
                return
            latest = _fetch_latest()
            state.update(checked_at=time.time(), latest=latest)
            _write_state(state)
            check._result = _Result(latest=latest)
        except SlopUpdateError as exc:
            check._result = _Result(warning=str(exc))
        except Exception:
            check._result = _Result(warning="could not check for a SlopBucket update")
        finally:
            check._done.set()

    threading.Thread(target=work, name="slop-self-update-check", daemon=True).start()
    return check


@contextmanager
def _update_lock():
    directory = _state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "slop-update.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        deadline = time.monotonic() + LOCK_WAIT_TIMEOUT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SlopUpdateError("another SlopBucket update is still running")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _stop_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=1)


def _download_source(directory: Path) -> Path:
    request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "SlopBucket"})
    archive = directory / "source.tar.gz"
    total = 0
    with urllib.request.urlopen(request, timeout=30) as response, archive.open("wb") as out:
        while chunk := response.read(65536):
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise SlopUpdateError("SlopBucket update download exceeded the size limit")
            out.write(chunk)
    source = directory / "source"
    source.mkdir()
    with tarfile.open(archive) as tar:
        try:
            tar.extractall(source, filter="data")
        except TypeError:
            tar.extractall(source)
    extracted = [entry for entry in source.iterdir() if entry.is_dir()]
    if len(extracted) != 1 or not (extracted[0] / "pyproject.toml").is_file():
        raise SlopUpdateError("SlopBucket update download had an unexpected layout")
    return extracted[0]


def _run_installer() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise SlopUpdateError("uv is required for automatic SlopBucket updates")
    with tempfile.TemporaryDirectory(prefix="slop-self-update-") as raw:
        source = _download_source(Path(raw))
        proc = subprocess.Popen(
            [uv, "tool", "install", "--force", "--python", ">=3.11", str(source)],
            cwd=raw, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            status = proc.wait(timeout=UPDATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            _stop_process_group(proc)
            raise SlopUpdateError("SlopBucket update timed out") from None
        except BaseException:
            _stop_process_group(proc)
            raise
        if status:
            raise SlopUpdateError("SlopBucket update did not succeed")


def _warn(message: str) -> None:
    print(f"slop: {message}; continuing with SlopBucket {__version__}.", file=sys.stderr)


def ensure_slop_updated(check: SlopUpdateCheck | None = None) -> None:
    """Install an available SlopBucket update; failures must not prevent launch.

    The new version takes effect next time slop runs; this process keeps
    the code it started with.
    """
    root = _install_root()
    if root is None:
        return
    current = _version_tuple(__version__)
    if current is None:
        return
    if check is None or time.monotonic() - check._started_at > CHECK_MAX_AGE:
        check = start_slop_check()
    if not check._done.wait(CHECK_WAIT_TIMEOUT):
        _warn("SlopBucket update check timed out")
        return
    result = check._result
    if result is None or result.silent:
        return
    if result.warning:
        _warn(result.warning)
        return
    latest = _version_tuple(result.latest)
    if latest is None or latest <= current:
        return
    state = _read_state()
    if state.get("attempted") == result.latest and _fresh(state, "attempted_at"):
        return
    try:
        with _update_lock():
            # A concurrent Slop process may already have installed the release.
            installed = _version_tuple(_installed_version(root))
            if installed is not None and installed >= latest:
                return
            state.update(attempted=result.latest, attempted_at=time.time())
            _write_state(state)
            print(f"slop: Updating SlopBucket to {result.latest}…", file=sys.stderr)
            _run_installer()
            installed = _version_tuple(_installed_version(root))
            if installed is None or installed < latest:
                raise SlopUpdateError(
                    "SlopBucket update finished without installing the requested version"
                )
            print(
                f"slop: SlopBucket {result.latest} installed; it takes effect next time you run slop.",
                file=sys.stderr,
            )
    except SlopUpdateError as exc:
        _warn(str(exc))
    except Exception:
        _warn("could not complete the SlopBucket update")
