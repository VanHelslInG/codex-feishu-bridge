"""Static checks on the macOS launcher assets.

No Mac is available, so the launcher scripts cannot be executed here. These
tests cover what can be checked from the repository alone: the LaunchAgent
template must parse as a plist, must stay in sync with the substitutions that
install-agent.sh performs, and the shell scripts must remain LF-only — a CRLF
shebang is the classic way a shell script arrives on a Mac already broken.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

MACOS_DIR = Path(__file__).resolve().parent.parent / "macos"
PLIST_TEMPLATE = MACOS_DIR / "com.chen.codex-feishu-bridge.plist.template"
INSTALL_AGENT = MACOS_DIR / "install-agent.sh"

PLACEHOLDER = re.compile(r"__[A-Z_]+__")
SED_PLACEHOLDER = re.compile(r"s\|\s*(__[A-Z_]+__)\s*\|")

VALUES = {
    "__ROOT__": "/Users/dev/codex-feishu-bridge",
    "__VENV__": "/Users/dev/codex-feishu-bridge/.venv",
    "__APP_DIR__": "/Users/dev/Library/Application Support/CodexFeishuBridge",
    "__HOME__": "/Users/dev",
}


def shell_scripts() -> list[Path]:
    return sorted(MACOS_DIR.rglob("*.sh"))


def test_there_are_shell_scripts_to_check():
    assert shell_scripts()


def test_every_shell_script_starts_with_a_bash_shebang():
    for script in shell_scripts():
        first_line = script.read_bytes().split(b"\n", 1)[0]
        assert first_line == b"#!/usr/bin/env bash", script.name


def test_no_shell_script_carries_crlf():
    """A CRLF shebang makes ./install.sh fail on macOS with a literal ^M."""
    for script in shell_scripts():
        assert b"\r\n" not in script.read_bytes(), script.name


def test_install_agent_fills_every_placeholder_in_the_template():
    """Adding a __TOKEN__ to the template without a sed rule would ship it raw."""
    in_template = set(PLACEHOLDER.findall(PLIST_TEMPLATE.read_text(encoding="utf-8")))
    filled_by_script = set(SED_PLACEHOLDER.findall(INSTALL_AGENT.read_text(encoding="utf-8")))
    assert in_template == filled_by_script
    assert in_template, "the template is expected to carry placeholders"
    assert in_template <= set(VALUES)


def test_the_rendered_agent_is_a_plist_that_polls_instead_of_autostarting():
    text = PLIST_TEMPLATE.read_text(encoding="utf-8")
    for token, value in VALUES.items():
        text = text.replace(token, value)
    assert not PLACEHOLDER.search(text)

    agent = plistlib.loads(text.encode("utf-8"))

    assert agent["Label"] == "com.chen.codex-feishu-bridge"
    # The bridge must not run at login or come back on its own: it is wanted
    # only while the Codex desktop app is open, and bridge-agent.sh decides.
    assert "RunAtLoad" not in agent
    assert "KeepAlive" not in agent
    assert agent["StartInterval"] == 120

    arguments = agent["ProgramArguments"]
    assert arguments[0] == "/bin/bash"
    assert arguments[1] == f"{VALUES['__ROOT__']}/macos/bridge-agent.sh"
    assert arguments[2] == f"{VALUES['__VENV__']}/bin/python"
    assert arguments[3] == VALUES["__ROOT__"]


def test_the_rendered_agent_gives_the_bridge_its_environment():
    text = PLIST_TEMPLATE.read_text(encoding="utf-8")
    for token, value in VALUES.items():
        text = text.replace(token, value)
    agent = plistlib.loads(text.encode("utf-8"))

    environment = agent["EnvironmentVariables"]
    assert environment["PYTHONPATH"] == f"{VALUES['__ROOT__']}/src"
    assert environment["CODEX_FEISHU_BRIDGE_HOME"] == VALUES["__APP_DIR__"]
    assert environment["HOME"] == VALUES["__HOME__"]
    # launchd's own default PATH is too narrow to find a Homebrew codex.
    assert "/opt/homebrew/bin" in environment["PATH"]
    assert "/usr/sbin" in environment["PATH"]


def test_the_agent_starts_nothing_when_codex_is_closed():
    """The whole point of the platform's autostart story."""
    script = (MACOS_DIR / "bridge-agent.sh").read_text(encoding="utf-8")
    assert "codex_app_running" in script
    assert "Contents/MacOS/" in script


def test_the_pid_file_and_health_port_come_from_one_place():
    """start/stop/bridge-agent must not each invent their own location."""
    lib = (MACOS_DIR / "lib" / "appdir.sh").read_text(encoding="utf-8")
    assert "bridge.pid" not in lib, "the pid file name belongs to the callers"
    assert "bridge_health_port" in lib
    for name in ("start.sh", "stop.sh", "status.sh", "bridge-agent.sh"):
        assert "bridge.pid" in (MACOS_DIR / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("name", ["install.sh", "start.sh", "stop.sh", "status.sh", "pair-code.sh"])
def test_scripts_source_the_shared_library(name):
    script = (MACOS_DIR / name).read_text(encoding="utf-8")
    assert 'macos/lib/appdir.sh' in script
