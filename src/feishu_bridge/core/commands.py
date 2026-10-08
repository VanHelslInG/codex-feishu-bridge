"""Slash-command parsing and the help text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

COMMANDS: Dict[str, str] = {
    "bind": "把当前会话与 Bridge 配对",
    "projects": "查看可用项目别名",
    "new": "新建任务并绑定当前话题",
    "tasks": "搜索最近的任务",
    "use": "把当前话题切到某个任务",
    "resume": "把当前话题绑定到已有任务",
    "status": "查看当前话题的任务与队列",
    "progress": "立即查看执行进度（不占用队列）",
    "where": "查看当前话题的路由与模型设置",
    "models": "查看可用模型",
    "model": "设置模型与推理强度",
    "compact": "压缩当前任务的上下文",
    "renew": "压缩后在继承历史的新任务里继续",
    "upload_revoke": "撤销当前任务的持续上传授权",
    "stop": "中断当前正在执行的处理",
    "fork": "分叉当前任务",
    "clear": "在当前话题里开一个空白任务",
    "rename": "修改当前话题标题",
    "cancel": "取消进行中的交互选择",
    "help": "查看帮助",
}

# Commands that need the mention prefix stripped in group chats.
ALIASES = {"h": "help", "?": "help", "start": "help", "progress_details": "progress"}


@dataclass
class Command:
    name: str
    rest: str = ""


def parse(text: str) -> Optional[Command]:
    """Parse ``/name rest`` from a message, ignoring an ``@bot`` suffix."""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped[1:].partition(" ")
    name = head.split("@", 1)[0].lower()
    if not name:
        return None
    return Command(name=ALIASES.get(name, name), rest=rest.strip())


def is_known(name: str) -> bool:
    return name in COMMANDS


def help_text() -> str:
    lines = [
        "**可用指令**",
        "",
        "在话题里直接发消息即可交给 Codex；下面这些指令用于管理任务。",
        "",
    ]
    for name, description in COMMANDS.items():
        lines.append(f"- `/{name}` — {description}")
    lines += [
        "",
        "**推荐流程**",
        "",
        "1. 在群里 `@机器人 项目 任务内容`：自动建话题、建任务并开始执行。",
        "2. 之后直接在该话题里继续发消息，按先后顺序排队执行。",
        "3. `/stop` 中断，`/progress` 随时看进度，`/model` 换模型。",
    ]
    return "\n".join(lines)


def command_names() -> List[str]:
    return list(COMMANDS.keys())
