from __future__ import annotations

from feishu_bridge.core import commands


def test_parses_plain_command():
    parsed = commands.parse("/new codex 修一下登录")
    assert parsed is not None
    assert parsed.name == "new"
    assert parsed.rest == "codex 修一下登录"


def test_strips_group_mention_suffix():
    parsed = commands.parse("/stop@my_bot")
    assert parsed is not None and parsed.name == "stop"


def test_alias_expansion():
    parsed = commands.parse("/h")
    assert parsed is not None and parsed.name == "help"


def test_non_command_is_none():
    assert commands.parse("帮我看看构建为什么失败") is None


def test_every_documented_command_has_a_handler_name():
    # Guards against documenting a command the bridge cannot route.
    for name in commands.command_names():
        assert name.isidentifier(), name
