from slop.config import Config, LaunchConfig, launch_command, load, save


def test_launch_command_full_access(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "config.toml"))
    cfg = Config(launch=LaunchConfig(full_access=True, bypass_hook_trust=True, bin="codex"))
    save(cfg)
    cmd = launch_command(load())
    assert cmd[0] == "codex"
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--dangerously-bypass-hook-trust" in cmd


def test_launch_command_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "config.toml"))
    cfg = Config(launch=LaunchConfig(full_access=False, bypass_hook_trust=False, extra_args=["--search"]))
    save(cfg)
    cmd = launch_command(load(), extra=["--", "-m", "gpt-5.4"])
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert cmd[-3:] == ["--search", "-m", "gpt-5.4"]


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "config.toml"))
    save(Config())
    monkeypatch.setenv("SLOP_FULL_ACCESS", "0")
    assert load().launch.full_access is False


def test_existing_configs_enable_managed_updates_by_default(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SLOP_CONFIG", str(path))
    path.write_text('[launch]\nbin = "codex"\n')
    cfg = load()
    assert cfg.launch.auto_update
    assert launch_command(cfg)[1:3] == ["-c", "check_for_update_on_startup=false"]


def test_disabling_auto_update_restores_codex_startup_check(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOP_CONFIG", str(tmp_path / "config.toml"))
    cfg = Config(launch=LaunchConfig(auto_update=False))
    save(cfg)
    assert not load().launch.auto_update
    assert "check_for_update_on_startup=false" not in launch_command(load())
