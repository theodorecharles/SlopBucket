from pathlib import Path


def test_repeated_shell_setup_preserves_trailing_config_and_multiline_functions(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("ZSH_CUSTOM", str(tmp_path / "custom"))
    rc = tmp_path / ".zshrc"
    rc.write_text(
        "# before\nfunction slop() {\n  echo old wrapper\n}\n"
        "# >>> slop >>>\n# old installer\n# <<< slop <<<\n"
        "export KEEP_THIS_SETTING=yes\n"
    )
    installer = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    setup = installer.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    for _ in range(2):
        exec(compile(setup, "install.sh shell setup", "exec"), {})
    updated = rc.read_text()
    assert "export KEEP_THIS_SETTING=yes" in updated
    assert "function slop() {\n  echo old wrapper\n}" in updated
    assert updated.count("# >>> slop >>>") == 1
    assert "unset -f slop" in updated
