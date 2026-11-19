import json
import time

import pytest

from slop import slop_update as su


def _fake_root(tmp_path, version="0.2.5"):
    root = tmp_path / "uv" / "tools" / "slop"
    package = root / "lib" / "python3.12" / "site-packages" / "slop"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'"""slop."""\n\n__version__ = "{version}"\n')
    return root


def _check(latest):
    check = su.SlopUpdateCheck()
    check._result = su._Result(latest=latest)
    check._done.set()
    return check


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = _fake_root(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(su, "_install_root", lambda: root)
    monkeypatch.setattr(su, "__version__", "0.2.5")
    return root


def test_fetch_latest_parses_version(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'"""slop."""\n\n__version__ = "0.2.6"\n'

    monkeypatch.setattr(su.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert su._fetch_latest() == "0.2.6"


def test_fetch_latest_rejects_non_stable_versions(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'__version__ = "0.2.6rc1"\n'

    monkeypatch.setattr(su.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(su.SlopUpdateError):
        su._fetch_latest()


def test_installed_version_reads_uv_tool_tree(tmp_path):
    assert su._installed_version(_fake_root(tmp_path, "0.2.7")) == "0.2.7"


def test_no_install_when_already_current(installed, monkeypatch):
    monkeypatch.setattr(
        su, "_run_installer", lambda: pytest.fail("installer must not run")
    )
    su.ensure_slop_updated(_check("0.2.5"))
    su.ensure_slop_updated(_check("0.2.4"))


def test_update_installs_and_reports_next_run(installed, monkeypatch, capsys):
    def fake_install():
        init = installed / "lib" / "python3.12" / "site-packages" / "slop" / "__init__.py"
        init.write_text('"""slop."""\n\n__version__ = "0.2.6"\n')

    monkeypatch.setattr(su, "_run_installer", fake_install)
    su.ensure_slop_updated(_check("0.2.6"))
    err = capsys.readouterr().err
    assert "Updating SlopBucket to 0.2.6" in err
    assert "takes effect next time you run slop" in err


def test_failed_install_warns_and_does_not_raise(installed, monkeypatch, capsys):
    def fail():
        raise su.SlopUpdateError("SlopBucket update did not succeed")

    monkeypatch.setattr(su, "_run_installer", fail)
    su.ensure_slop_updated(_check("0.2.6"))
    assert "continuing with SlopBucket 0.2.5" in capsys.readouterr().err


def test_recent_failed_attempt_is_not_retried_immediately(installed, monkeypatch):
    state_dir = installed.parent.parent.parent / "state" / "slop"
    state_dir.mkdir(parents=True)
    (state_dir / "slop-update-check.json").write_text(json.dumps({
        "attempted": "0.2.6",
        "attempted_at": time.time(),
        "checked_at": time.time(),
        "latest": "0.2.6",
    }))
    monkeypatch.setattr(
        su, "_run_installer", lambda: pytest.fail("installer must not run yet")
    )
    su.ensure_slop_updated(_check("0.2.6"))


def test_checkout_install_skips_silently(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(su, "_install_root", lambda: None)
    monkeypatch.setattr(
        su, "_run_installer", lambda: pytest.fail("installer must not run")
    )
    su.ensure_slop_updated(_check("9.9.9"))
    assert capsys.readouterr().err == ""
    check = su.start_slop_check()
    assert check._done.wait(5)
    assert check._result is not None and check._result.silent


def test_check_uses_fresh_cached_latest(installed, monkeypatch):
    state_dir = su._state_dir()
    state_dir.mkdir(parents=True)
    (state_dir / "slop-update-check.json").write_text(json.dumps({
        "checked_at": time.time(),
        "latest": "0.2.6",
    }))
    monkeypatch.setattr(
        su, "_fetch_latest", lambda: pytest.fail("network must not be used")
    )
    check = su.start_slop_check()
    assert check._done.wait(5)
    assert check._result is not None and check._result.latest == "0.2.6"
