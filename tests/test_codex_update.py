import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from slop import codex_update as update


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def npm_codex(tmp_path, monkeypatch):
    root = tmp_path / "npm" / "lib" / "node_modules" / "@openai" / "codex"
    (root / "bin").mkdir(parents=True)
    manifest = root / "package.json"
    manifest.write_text(json.dumps({"name": "@openai/codex", "version": "0.162.1"}))
    log = tmp_path / "updates.jsonl"
    executable = root / "bin" / "codex.js"
    executable.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
manifest = Path({str(manifest)!r})
if sys.argv[1] == '--version':
    print('codex-cli ' + json.loads(manifest.read_text())['version'])
elif sys.argv[1] == 'update':
    with open({str(log)!r}, 'a') as log:
        log.write(json.dumps({{'cwd': os.getcwd(), 'stdin': sys.stdin.read(),
                              'noninteractive': os.environ.get('CODEX_NON_INTERACTIVE')}}) + '\\n')
    manifest.write_text(json.dumps({{'name': '@openai/codex', 'version': '0.163.0'}}))
else:
    sys.exit(9)
''')
    executable.chmod(0o700)
    binary = tmp_path / "codex"
    binary.symlink_to(executable)
    monkeypatch.setattr(update, "_latest_version", lambda _: "0.163.0")
    return binary, manifest, executable, log


@pytest.mark.parametrize("installed", ["0.163.0", "0.164.0", "1.0.0", "0.162.1-alpha.2", "0.162.1+custom", "0.0.0"])
def test_current_newer_and_custom_builds_are_never_updated(npm_codex, installed, monkeypatch):
    binary, manifest, _, log = npm_codex
    manifest.write_text(json.dumps({"version": installed}))
    update.ensure_updated(str(binary))
    assert not log.exists()
    assert json.loads(manifest.read_text())["version"] == installed


def test_update_uses_native_command_then_verifies(npm_codex, monkeypatch):
    binary, manifest, _, log = npm_codex
    original_cwd = Path.cwd()
    update.ensure_updated(str(binary))
    assert json.loads(manifest.read_text())["version"] == "0.163.0"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 1
    assert Path(calls[0]["cwd"]) != original_cwd
    assert not Path(calls[0]["cwd"]).exists()
    assert calls[0]["stdin"] == ""
    assert calls[0]["noninteractive"] == "1"
    assert Path.cwd() == original_cwd


def test_successful_command_that_did_not_update_warns(npm_codex, monkeypatch, capsys):
    binary, _, _, _ = npm_codex
    monkeypatch.setattr(update, "_run_updater", lambda _: None)
    assert update.ensure_updated(str(binary)) is None
    assert "without installing the requested version" in capsys.readouterr().err


def test_network_failure_is_nonfatal_and_hides_diagnostics(npm_codex, monkeypatch, capsys):
    binary, _, _, log = npm_codex
    def fail(_):
        raise OSError("SECRET_TOKEN upstream diagnostic")
    monkeypatch.setattr(update, "_latest_version", fail)
    assert update.ensure_updated(str(binary)) is None
    output = capsys.readouterr().err
    assert "could not check" in output
    assert "SECRET_TOKEN" not in output
    assert not log.exists()


def test_failed_native_update_is_nonfatal(npm_codex, capsys):
    binary, manifest, executable, _ = npm_codex
    executable.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(7)\n")
    assert update.ensure_updated(str(binary)) is None
    assert "did not succeed" in capsys.readouterr().err
    assert json.loads(manifest.read_text())["version"] == "0.162.1"


def test_start_check_does_not_wait_for_network(npm_codex, monkeypatch):
    binary, _, _, log = npm_codex
    entered = threading.Event()
    release = threading.Event()
    def slow(_):
        entered.set()
        release.wait(3)
        return "0.163.0"
    monkeypatch.setattr(update, "_latest_version", slow)
    try:
        started = time.monotonic()
        check = update.start_check(str(binary))
        assert time.monotonic() - started < 0.5
        assert entered.wait(1)
        assert not check._done.is_set()
        assert not log.exists()
    finally:
        release.set()
    assert check._done.wait(1)
    assert not log.exists()


def test_slow_background_check_cannot_block_launch(npm_codex, monkeypatch, capsys):
    binary, _, _, log = npm_codex
    check = update.UpdateCheck(str(binary))
    monkeypatch.setattr(update, "CHECK_WAIT_TIMEOUT", 0.01)
    started = time.monotonic()
    update.ensure_updated(str(binary), check)
    assert time.monotonic() - started < 1
    assert "check timed out" in capsys.readouterr().err
    assert not log.exists()


def test_stale_dashboard_check_is_refreshed(npm_codex, monkeypatch):
    binary, _, _, log = npm_codex
    monkeypatch.setattr(update, "_latest_version", lambda _: "0.162.1")
    check = update.start_check(str(binary))
    assert check._done.wait(1)
    check._started_at -= update.CHECK_MAX_AGE + 1
    monkeypatch.setattr(update, "_latest_version", lambda _: "0.163.0")
    update.ensure_updated(str(binary), check)
    assert log.exists()


def test_check_for_different_binary_is_not_reused(npm_codex):
    binary, _, _, log = npm_codex
    check = update.UpdateCheck("/another/codex")
    update.ensure_updated(str(binary), check)
    assert log.exists()


def test_concurrent_launches_only_install_once(npm_codex):
    binary, _, executable, log = npm_codex
    # Hold the first process long enough for the second to contend on the lock.
    executable.write_text(executable.read_text().replace(
        "elif sys.argv[1] == 'update':", "elif sys.argv[1] == 'update':\n    import time; time.sleep(0.2)"))
    checks = [update.start_check(str(binary)) for _ in range(2)]
    assert all(check._done.wait(1) for check in checks)
    threads = [threading.Thread(target=update.ensure_updated, args=(str(binary), check)) for check in checks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert len(log.read_text().splitlines()) == 1


def test_lock_wait_is_bounded(npm_codex, monkeypatch, capsys):
    binary, _, _, log = npm_codex
    monkeypatch.setattr(update, "LOCK_WAIT_TIMEOUT", 0.01)
    with update._update_lock(update._detect_install(str(binary))):
        update.ensure_updated(str(binary))
    assert "another Codex update is still running" in capsys.readouterr().err
    assert not log.exists()


def test_current_version_waits_for_in_progress_install(npm_codex):
    binary, manifest, _, log = npm_codex
    manifest.write_text(json.dumps({"version": "0.163.0"}))
    check = update.start_check(str(binary))
    assert check._done.wait(1)
    with update._update_lock(update._detect_install(str(binary))):
        launch = threading.Thread(target=update.ensure_updated, args=(str(binary), check))
        launch.start()
        launch.join(0.15)
        assert launch.is_alive()
    launch.join(2)
    assert not launch.is_alive()
    assert not log.exists()


def test_update_timeout_terminates_process_group(npm_codex, monkeypatch, tmp_path, capsys):
    binary, _, executable, _ = npm_codex
    pid_file = tmp_path / "child.pid"
    executable.write_text(f'''#!{sys.executable}
import signal, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path({str(pid_file)!r}).write_text(str(child.pid))
def reap(signum, frame):
    child.wait(timeout=1)
    sys.exit(0)
signal.signal(signal.SIGTERM, reap)
time.sleep(60)
''')
    monkeypatch.setattr(update, "UPDATE_TIMEOUT", 0.2)
    started = time.monotonic()
    update.ensure_updated(str(binary))
    assert time.monotonic() - started < 3
    assert "update timed out" in capsys.readouterr().err
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_version_probe_timeout_is_nonfatal(npm_codex, monkeypatch, capsys):
    binary, manifest, executable, _ = npm_codex
    manifest.unlink()
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    monkeypatch.setattr(update, "VERSION_TIMEOUT", 0.05)
    started = time.monotonic()
    update.ensure_updated(str(binary))
    assert time.monotonic() - started < 3
    assert "could not check" in capsys.readouterr().err


def test_homebrew_version_uses_metadata_without_starting_binary(tmp_path, monkeypatch):
    executable = tmp_path / "opt" / "homebrew" / "Caskroom" / "codex" / "0.162.1" / "codex"
    executable.parent.mkdir(parents=True)
    executable.touch()
    binary = tmp_path / "codex"
    binary.symlink_to(executable)
    def unexpected(*args, **kwargs):
        pytest.fail("Homebrew version must not start the binary")
    monkeypatch.setattr(update.subprocess, "Popen", unexpected)
    install = update._detect_install(str(binary))
    assert install.method == "brew"
    assert install.root == executable.parent.parent
    assert update._installed_version(str(binary), install) == "0.162.1"


def test_standalone_detection_follows_current_symlink(tmp_path):
    root = tmp_path / ".codex" / "packages" / "standalone"
    release = root / "releases" / "0.162.1-x86_64-unknown-linux-musl"
    (release / "bin").mkdir(parents=True)
    (release / "bin" / "codex").touch()
    (release / "codex-package.json").write_text(json.dumps({"layoutVersion": 1, "version": "0.162.1"}))
    (root / "current").symlink_to(release)
    binary = tmp_path / "codex"
    binary.symlink_to(root / "current" / "bin" / "codex")
    install = update._detect_install(str(binary))
    assert install.method == "standalone"
    assert install.root == root
    assert update._installed_version(str(binary), install) == "0.162.1"


def test_unknown_install_does_not_execute(tmp_path, monkeypatch, capsys):
    binary = tmp_path / "custom-codex"
    binary.write_text("do not run")
    def unexpected(*args, **kwargs):
        pytest.fail("Unknown installation must not execute")
    monkeypatch.setattr(update.subprocess, "Popen", unexpected)
    update.ensure_updated(str(binary))
    assert "does not support automatic updates" in capsys.readouterr().err


@pytest.mark.parametrize("method,payload,url", [
    ("brew", {"version": "0.163.0"}, "https://formulae.brew.sh/api/cask/codex.json"),
    ("npm", {"version": "0.163.0"}, "https://registry.npmjs.org/@openai/codex/latest"),
    ("standalone", {"tag_name": "rust-v0.163.0"}, "https://api.github.com/repos/openai/codex/releases/latest"),
])
def test_update_metadata_uses_install_channel(method, payload, url, tmp_path, monkeypatch):
    def response(request, timeout):
        assert request.full_url == url
        assert timeout == update.NETWORK_TIMEOUT
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(update.urllib.request, "urlopen", response)
    assert update._latest_version(update._Installation(method, tmp_path, tmp_path / "manifest")) == "0.163.0"


@pytest.mark.parametrize("payload", [
    b'{"version":"0.164.0-alpha"}', b'{"version":"SECRET_TOKEN"}', b'[]', b'x' * (1024 * 1024 + 1),
])
def test_invalid_or_oversized_release_metadata_is_rejected(payload, tmp_path, monkeypatch):
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(payload))
    with pytest.raises(update.UpdateError) as error:
        update._latest_version(update._Installation("npm", tmp_path, tmp_path / "manifest"))
    assert "SECRET_TOKEN" not in str(error.value)
