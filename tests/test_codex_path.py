"""The bridge must find the Codex CLI, or say clearly why it cannot.

Regression cover for the WinError 2 crash loop: the resolver used to fall back
to the literal string ``codex.exe``, so a missing config produced an opaque
spawn failure instead of an actionable message.
"""

from __future__ import annotations

import os

import pytest

from feishu_bridge.platform.base import CodexNotFound
from feishu_bridge.platform.windows import WindowsPlatform


def make_platform(tmp_path, monkeypatch, path_hit=None) -> WindowsPlatform:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codexhome"))
    monkeypatch.setattr(
        "feishu_bridge.platform.windows._which",
        lambda name: path_hit if name in ("codex.exe", "codex.cmd", "codex") else None,
    )
    return WindowsPlatform()


def test_an_explicit_config_path_wins(tmp_path, monkeypatch):
    exe = tmp_path / "codex.exe"
    exe.write_text("x")
    platform = make_platform(tmp_path, monkeypatch)
    assert platform.resolve_codex_path(str(exe)) == str(exe)


def test_a_missing_configured_path_is_named_in_the_error(tmp_path, monkeypatch):
    platform = make_platform(tmp_path, monkeypatch)
    with pytest.raises(CodexNotFound) as excinfo:
        platform.resolve_codex_path(str(tmp_path / "gone" / "codex.exe"))
    assert "codex.exe" in str(excinfo.value)


def test_path_is_consulted_first(tmp_path, monkeypatch):
    exe = tmp_path / "on-path" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    platform = make_platform(tmp_path, monkeypatch, path_hit=str(exe))
    assert platform.resolve_codex_path(None) == str(exe)


def test_the_desktop_apps_newest_bin_directory_is_used(tmp_path, monkeypatch):
    bin_root = tmp_path / "AppData" / "OpenAI" / "Codex" / "bin"
    older = bin_root / "aaaa" / "codex.exe"
    newer = bin_root / "bbbb" / "codex.exe"
    for path in (older, newer):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    os.utime(older, (1, 1))
    platform = make_platform(tmp_path, monkeypatch)
    assert platform.resolve_codex_path(None) == str(newer)


def test_config_toml_is_the_last_resort(tmp_path, monkeypatch):
    exe = tmp_path / "cli" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    toml = tmp_path / "codexhome" / "config.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text(f"CODEX_CLI_PATH = '{exe}'\n", encoding="utf-8")
    platform = make_platform(tmp_path, monkeypatch)
    assert platform.resolve_codex_path(None) == str(exe)


def test_nothing_found_reports_every_place_searched(tmp_path, monkeypatch):
    platform = make_platform(tmp_path, monkeypatch)
    with pytest.raises(CodexNotFound) as excinfo:
        platform.resolve_codex_path(None)
    message = str(excinfo.value)
    assert "PATH" in message
    assert "OpenAI" in message
    assert "config.toml" in message
    assert "config.json" in message


def test_config_toml_pointing_at_a_deleted_file_is_not_used(tmp_path, monkeypatch):
    toml = tmp_path / "codexhome" / "config.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text("CODEX_CLI_PATH = 'D:/gone/codex.exe'\n", encoding="utf-8")
    platform = make_platform(tmp_path, monkeypatch)
    with pytest.raises(CodexNotFound):
        platform.resolve_codex_path(None)


def test_base_platform_also_reports_a_configured_but_missing_path(tmp_path):
    from feishu_bridge.platform.base import Platform

    with pytest.raises(CodexNotFound):
        Platform().resolve_codex_path(str(tmp_path / "nope"))
