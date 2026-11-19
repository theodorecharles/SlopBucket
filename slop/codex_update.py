"""Check for Codex updates in the background and install them before launch."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


VERSION_TIMEOUT = 5
NETWORK_TIMEOUT = 4
CHECK_WAIT_TIMEOUT = 10
CHECK_MAX_AGE = 300
UPDATE_TIMEOUT = 180
LOCK_WAIT_TIMEOUT = UPDATE_TIMEOUT + 10
MAX_METADATA_BYTES = 1024 * 1024
SOURCES = {
    "brew": ("https://formulae.brew.sh/api/cask/codex.json", "version"),
    "npm": ("https://registry.npmjs.org/@openai/codex/latest", "version"),
    "standalone": ("https://api.github.com/repos/openai/codex/releases/latest", "tag_name"),
}


class UpdateError(Exception):
    """A fixed, safe message that may be shown without exposing diagnostics."""


@dataclass(frozen=True)
class _Installation:
    method: str
    root: Path
    manifest: Path
    version_hint: str | None = None


@dataclass(frozen=True)
class _Result:
    install: _Installation | None = None
    current: str | None = None
    latest: str | None = None
    warning: str | None = None


@dataclass
class UpdateCheck:
    binary: str
    _started_at: float = field(default_factory=time.monotonic)
    _done: threading.Event = field(default_factory=threading.Event)
    _result: _Result | None = None


def _stable_version(value: str | None) -> tuple[int, int, int] | None:
    # Custom builds and prereleases belong to their chosen channel. Never replace
    # one with the stable channel, even when its numeric component looks older.
    if value is None or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        return None
    version = tuple(int(part) for part in value.split("."))
    return version if version != (0, 0, 0) else None


def _detect_install(binary: str) -> _Installation:
    resolved = Path(binary).resolve()
    parts = resolved.parts
    for i in range(len(parts) - 2):
        if parts[i:i + 2] == ("Caskroom", "codex"):
            root = Path(*parts[:i + 2])
            return _Installation("brew", root, root / parts[i + 2] / "codex-package.json", parts[i + 2])
        if parts[i:i + 3] == ("node_modules", "@openai", "codex"):
            root = Path(*parts[:i + 3])
            return _Installation("npm", root, root / "package.json")
        if parts[i:i + 2] == ("packages", "standalone") and parts[i + 2] in {"releases", "current"}:
            root = Path(*parts[:i + 2])
            # Read the release reached by this executable, not a different
            # installation's current link.
            manifest = resolved.parent.parent / "codex-package.json"
            return _Installation("standalone", root, manifest)
    raise UpdateError("this Codex installation does not support automatic updates")


def _stop_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # The parent may exit before a child does. Kill the group even after wait()
    # succeeds so npm, brew, or an installer cannot continue after a timeout.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=1)


def _installed_version(binary: str, install: _Installation) -> str:
    try:
        with install.manifest.open("rb") as source:
            raw = source.read(MAX_METADATA_BYTES + 1)
        if len(raw) <= MAX_METADATA_BYTES:
            manifest = json.loads(raw)
            version = manifest.get("version") if isinstance(manifest, dict) else None
            if isinstance(version, str) and version:
                return version
    except (OSError, ValueError):
        pass
    if install.version_hint:
        return install.version_hint
    # Most installations have version metadata, including Homebrew builds whose
    # executable can be slow to start on macOS. Probe only as a bounded fallback.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            [binary, "--version"], stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            status = proc.wait(timeout=VERSION_TIMEOUT)
        except BaseException:
            _stop_process_group(proc)
            raise
        output.seek(0)
        version_output = output.read(4097)
    if status == 0 and len(version_output) <= 4096:
        match = re.fullmatch(r"codex(?:-cli)? ([^\s]+)\s*", version_output.decode(errors="replace"))
        if match:
            return match.group(1)
    raise UpdateError("could not determine the installed Codex version")


def _latest_version(install: _Installation) -> str:
    url, key = SOURCES[install.method]
    request = urllib.request.Request(url, headers={"User-Agent": "SlopBucket", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
        raw = response.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise UpdateError("Codex update metadata exceeded the size limit")
    metadata = json.loads(raw)
    if not isinstance(metadata, dict) or metadata.get("prerelease") or metadata.get("draft"):
        raise UpdateError("Codex update metadata was not a stable release")
    version = metadata.get(key)
    if isinstance(version, str) and install.method == "standalone":
        version = version.removeprefix("rust-v")
    if not isinstance(version, str) or _stable_version(version) is None:
        raise UpdateError("Codex update metadata did not contain a stable version")
    return version


def start_check(binary: str) -> UpdateCheck:
    """Immediately start a read-only check without delaying the account picker."""
    check = UpdateCheck(os.path.abspath(binary))

    def work() -> None:
        try:
            install = _detect_install(check.binary)
            current = _installed_version(check.binary, install)
            if _stable_version(current) is None:
                check._result = _Result(warning="automatic updates are skipped for custom or prerelease Codex builds")
            else:
                check._result = _Result(install, current, _latest_version(install))
        except UpdateError as exc:
            check._result = _Result(warning=str(exc))
        except Exception:
            check._result = _Result(warning="could not check for a Codex update")
        finally:
            check._done.set()

    threading.Thread(target=work, name="slop-codex-update-check", daemon=True).start()
    return check


@contextmanager
def _update_lock(install: _Installation):
    state = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not state.is_absolute():
        state = Path.home() / ".local" / "state"
    directory = state / "slop"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(str(install.root).encode()).hexdigest()[:20]
    fd = os.open(directory / f"codex-update-{key}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        deadline = time.monotonic() + LOCK_WAIT_TIMEOUT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise UpdateError("another Codex update is still running")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _run_updater(binary: str) -> None:
    env = os.environ.copy()
    env["CODEX_NON_INTERACTIVE"] = "1"
    # Avoid project-local package manager configuration and leave launch cwd alone.
    with tempfile.TemporaryDirectory(prefix="slop-codex-update-") as directory:
        proc = subprocess.Popen(
            [binary, "update"], cwd=directory, env=env,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            status = proc.wait(timeout=UPDATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            _stop_process_group(proc)
            raise UpdateError("Codex update timed out") from None
        except BaseException:
            _stop_process_group(proc)
            raise
        if status:
            raise UpdateError("Codex update did not succeed")


def _warn(message: str) -> None:
    print(f"slop: {message}; continuing with installed Codex.", file=sys.stderr)


def ensure_updated(binary: str, check: UpdateCheck | None = None) -> None:
    """Apply an available stable update; failures must not prevent Codex launch."""
    binary = os.path.abspath(binary)
    if (check is None or check.binary != binary
            or time.monotonic() - check._started_at > CHECK_MAX_AGE):
        check = start_check(binary)
    if not check._done.wait(CHECK_WAIT_TIMEOUT):
        _warn("Codex update check timed out")
        return
    result = check._result
    if result is None:
        _warn("could not check for a Codex update")
        return
    if result.warning:
        _warn(result.warning)
        return
    latest = _stable_version(result.latest)
    current = _stable_version(result.current)
    if result.install is None or latest is None or current is None:
        return
    try:
        with _update_lock(result.install):
            install = _detect_install(binary)
            if install.root != result.install.root or install.method != result.install.method:
                raise UpdateError("Codex installation changed during the update check")
            # A concurrent Slop process may already have installed the release.
            current = _stable_version(_installed_version(binary, install))
            if current is None or current >= latest:
                return
            print(f"slop: Updating Codex to {result.latest}…", file=sys.stderr)
            _run_updater(binary)
            installed = _stable_version(_installed_version(binary, _detect_install(binary)))
            if installed is None or installed < latest:
                raise UpdateError("Codex update finished without installing the requested version")
    except UpdateError as exc:
        _warn(str(exc))
    except Exception:
        _warn("could not complete the Codex update")
