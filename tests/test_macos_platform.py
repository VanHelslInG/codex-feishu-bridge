"""The macOS adapter, exercised from any host.

Nothing here needs a Mac: the Keychain helper is a subprocess and the CLI
lookup is pure path probing, so both can be pinned on Windows. What cannot be
pinned this way — launchd, the real `security` binary, the app-bundle layout —
stays explicitly unverified and is called out in the handoff.
"""

from __future__ import annotations

import subprocess

import pytest

from feishu_bridge.platform.base import CodexNotFound
from feishu_bridge.platform.macos import MacOSPlatform


def make_platform(tmp_path, monkeypatch, path_hit=None) -> MacOSPlatform:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codexhome"))
    monkeypatch.setattr(
        "feishu_bridge.platform.macos.shutil.which",
        lambda name: path_hit if name == "codex" else None,
    )
    monkeypatch.setattr(
        MacOSPlatform, "_app_bundle_candidates", staticmethod(lambda: [])
    )
    return MacOSPlatform()


# --- Keychain ---------------------------------------------------------------


def test_a_missing_security_binary_reads_as_an_absent_secret(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("feishu_bridge.platform.macos.subprocess.run", boom)
    assert MacOSPlatform().secret_get("svc", "acct") is None


def test_a_stored_secret_is_returned_without_its_trailing_newline(monkeypatch):
    monkeypatch.setattr(
        "feishu_bridge.platform.macos.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "s3cret\n", ""),
    )
    assert MacOSPlatform().secret_get("svc", "acct") == "s3cret"


def test_a_keychain_miss_is_not_an_error(monkeypatch):
    monkeypatch.setattr(
        "feishu_bridge.platform.macos.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 44, "", "not found"),
    )
    assert MacOSPlatform().secret_get("svc", "acct") is None


def test_an_empty_keychain_entry_reads_as_absent(monkeypatch):
    monkeypatch.setattr(
        "feishu_bridge.platform.macos.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "\n", ""),
    )
    assert MacOSPlatform().secret_get("svc", "acct") is None


def test_storing_a_secret_without_a_keychain_says_so(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("feishu_bridge.platform.macos.subprocess.run", boom)
    with pytest.raises(OSError) as excinfo:
        MacOSPlatform().secret_set("svc", "acct", "value")
    assert "security" in str(excinfo.value)


def test_deleting_an_absent_secret_is_a_no_op(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("feishu_bridge.platform.macos.subprocess.run", boom)
    MacOSPlatform().secret_delete("svc", "acct")


# --- Codex CLI lookup -------------------------------------------------------


def test_an_explicit_config_path_wins(tmp_path, monkeypatch):
    exe = tmp_path / "codex"
    exe.write_text("x")
    platform = make_platform(tmp_path, monkeypatch)
    assert platform.resolve_codex_path(str(exe)) == str(exe)


def test_a_missing_configured_path_is_named_in_the_error(tmp_path, monkeypatch):
    platform = make_platform(tmp_path, monkeypatch)
    with pytest.raises(CodexNotFound) as excinfo:
        platform.resolve_codex_path(str(tmp_path / "gone" / "codex"))
    assert "codex" in str(excinfo.value)


def test_path_is_consulted_first(tmp_path, monkeypatch):
    exe = tmp_path / "on-path" / "codex"
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    platform = make_platform(tmp_path, monkeypatch, path_hit=str(exe))
    assert platform.resolve_codex_path(None) == str(exe)


def test_the_desktop_app_bundle_is_used_when_path_misses(tmp_path, monkeypatch):
    exe = tmp_path / "ChatGPT.app" / "Contents" / "Resources" / "codex-cli" / "codex"
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    platform = make_platform(tmp_path, monkeypatch)
    monkeypatch.setattr(
        MacOSPlatform, "_app_bundle_candidates", staticmethod(lambda: [tmp_path / "nope", exe])
    )
    assert platform.resolve_codex_path(None) == str(exe)


def test_the_nested_cli_bundle_layout_is_among_the_candidates():
    """The layout the Telegram bridge runs on: the CLI is a nested helper app."""
    # as_posix() so the assertion reads the same wherever the suite runs.
    candidates = [path.as_posix() for path in MacOSPlatform._app_bundle_candidates()]
    assert any(
        path.startswith("/Applications/ChatGPT.app/Contents/Resources/codex-cli/")
        and path.endswith("/Contents/MacOS/codex")
        for path in candidates
    )


def test_config_toml_is_the_last_resort(tmp_path, monkeypatch):
    exe = tmp_path / "cli" / "codex"
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    toml = tmp_path / "codexhome" / "config.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text(f"CODEX_CLI_PATH = '{exe}'\n", encoding="utf-8")
    platform = make_platform(tmp_path, monkeypatch)
    assert platform.resolve_codex_path(None) == str(exe)


def test_a_tilde_in_the_recorded_path_is_expanded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    exe = home / "bin" / "codex"
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    # expanduser() reads USERPROFILE on Windows and HOME on POSIX, so pin both
    # and the test means the same thing on either host.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    toml = tmp_path / "codexhome" / "config.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text("CODEX_CLI_PATH = '~/bin/codex'\n", encoding="utf-8")
    platform = make_platform(tmp_path, monkeypatch)
    assert platform.resolve_codex_path(None) == str(exe)


def test_config_toml_pointing_at_a_deleted_file_is_not_used(tmp_path, monkeypatch):
    toml = tmp_path / "codexhome" / "config.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text("CODEX_CLI_PATH = '/gone/codex'\n", encoding="utf-8")
    platform = make_platform(tmp_path, monkeypatch)
    with pytest.raises(CodexNotFound):
        platform.resolve_codex_path(None)


def test_nothing_found_reports_every_place_searched(tmp_path, monkeypatch):
    platform = make_platform(tmp_path, monkeypatch)
    with pytest.raises(CodexNotFound) as excinfo:
        platform.resolve_codex_path(None)
    message = str(excinfo.value)
    assert "PATH" in message
    assert "config.toml" in message
    assert "config.json" in message


# --- FD counting ------------------------------------------------------------


def test_fd_count_parses_lsof_field_output(monkeypatch):
    output = "p123\nfcwd\nf0\nf1\nf2\nf3\n"
    monkeypatch.setattr(
        "feishu_bridge.platform.macos.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, output, ""),
    )
    assert MacOSPlatform().fd_count(123) == 4


def test_fd_count_is_unknown_when_lsof_fails(monkeypatch):
    monkeypatch.setattr(
        "feishu_bridge.platform.macos.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "lsof: no pids"),
    )
    assert MacOSPlatform().fd_count(123) is None


def test_fd_count_is_unknown_when_lsof_is_missing(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("feishu_bridge.platform.macos.subprocess.run", boom)
    assert MacOSPlatform().fd_count(123) is None
