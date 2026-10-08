"""Card builders for the bridge's interactive surfaces."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .render import button, button_row, card, markdown_elements

STATUS_TEMPLATE = {
    "completed": "green",
    "failed": "red",
    "interrupted": "grey",
}


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...（内容过长，已截断）"


def approval_card(request_type: str, reason: str, detail: str, token: str) -> Dict[str, Any]:
    body = (
        f"**原因**\n{reason or 'Codex 需要你的确认才能继续。'}\n\n"
        f"**操作内容**\n```\n{_truncate(detail, 1400) or '未提供详情'}\n```\n\n"
        "“允许一次”只授权当前请求；“本任务始终允许”会在当前任务会话内记住授权。"
    )
    elements: List[Dict[str, Any]] = markdown_elements(body)
    elements.append(
        button_row(
            [
                button("允许一次", {"action": "approval", "token": token, "decision": "accept"}, "primary"),
                button(
                    "本任务始终允许",
                    {"action": "approval", "token": token, "decision": "acceptForSession"},
                ),
            ]
        )
    )
    elements.append(
        button_row([button("拒绝", {"action": "approval", "token": token, "decision": "decline"}, "danger")])
    )
    return card(elements, title=f"需要授权：{request_type}", template="orange")


def approval_result_card(request_type: str, decision: str, detail: str = "") -> Dict[str, Any]:
    labels = {
        "accept": "✅ 已批准（仅本次）",
        "acceptForSession": "✅ 已批准（本任务内）",
        "decline": "⛔ 已拒绝",
        "cancel": "⛔ 已取消",
        "expired": "⌛ 该请求已失效",
    }
    template = "green" if decision in ("accept", "acceptForSession") else "grey"
    if decision == "decline":
        template = "red"
    body = labels.get(decision, decision)
    if detail:
        body += f"\n\n{_truncate(detail, 600)}"
    return card(markdown_elements(body), title=f"授权：{request_type}", template=template)


def model_picker_card(models: List[Dict[str, Any]], thread_id: str, current: str = "") -> Dict[str, Any]:
    elements: List[Dict[str, Any]] = markdown_elements(
        "选择这个任务使用的模型。选中后会记住，之后可以随时用 `/model` 换。"
    )
    for entry in models:
        name = str(entry.get("model") or entry.get("id") or entry.get("slug") or "")
        if not name:
            continue
        label = str(entry.get("displayName") or entry.get("display_name") or name)
        marker = " ✅" if name == current else ""
        elements.append(
            button_row(
                [
                    button(
                        f"{label}{marker}",
                        {"action": "model", "model": name, "thread": thread_id},
                        "primary" if name == current else "default",
                    )
                ]
            )
        )
    return card(elements, title="选择模型", template="blue")


def project_picker_card(
    aliases: List[str], prompt_preview: str, token: str, page: int = 0, per_page: int = 8
) -> Dict[str, Any]:
    start = page * per_page
    window = aliases[start : start + per_page]
    elements: List[Dict[str, Any]] = markdown_elements(
        f"这条消息要交给哪个项目处理？\n\n> {_truncate(prompt_preview, 300)}"
    )
    for alias in window:
        elements.append(
            button_row([button(alias, {"action": "project", "token": token, "project": alias}, "primary")])
        )
    navigation: List[Dict[str, Any]] = []
    if page > 0:
        navigation.append(button("上一页", {"action": "project_page", "token": token, "page": page - 1}))
    total_pages = max(1, (len(aliases) + per_page - 1) // per_page)
    navigation.append(button(f"{page + 1}/{total_pages}", {"action": "noop"}))
    if start + per_page < len(aliases):
        navigation.append(button("下一页", {"action": "project_page", "token": token, "page": page + 1}))
    if navigation:
        elements.append(button_row(navigation))
    return card(elements, title="选择项目", template="blue")


def progress_card(
    title: str,
    lines: List[str],
    thread_id: str,
    running: bool,
) -> Dict[str, Any]:
    elements: List[Dict[str, Any]] = markdown_elements("\n".join(lines) or "暂无进度信息。")
    actions: List[Dict[str, Any]] = [
        button("刷新", {"action": "progress_refresh", "thread": thread_id})
    ]
    if running:
        actions.append(button("中断当前处理", {"action": "interrupt", "thread": thread_id}, "danger"))
    elements.append(button_row(actions))
    return card(elements, title=title, template="blue")


def task_list_card(
    tasks: List[Dict[str, Any]],
    token_prefix: str,
    page: int,
    has_next: bool,
) -> Dict[str, Any]:
    elements: List[Dict[str, Any]] = []
    if not tasks:
        elements.append({"tag": "markdown", "content": "没有找到匹配的任务。"})
    for task in tasks:
        thread_id = str(task.get("id") or "")
        label = str(task.get("title") or "(未命名)")[:60]
        status = str(task.get("status") or "")
        short = thread_id[:8]
        elements.append(
            button_row(
                [
                    button(
                        f"{label} · {status} · {short}",
                        {"action": "resume", "thread": thread_id},
                    )
                ]
            )
        )
    navigation: List[Dict[str, Any]] = []
    if page > 0:
        navigation.append(button("上一页", {"action": "tasks_page", "page": page - 1}))
    navigation.append(button(f"第 {page + 1} 页", {"action": "noop"}))
    if has_next:
        navigation.append(button("下一页", {"action": "tasks_page", "page": page + 1}))
    if navigation:
        elements.append(button_row(navigation))
    return card(elements, title="任务列表", template="blue")


def result_card(status: str, body: str) -> Dict[str, Any]:
    template = STATUS_TEMPLATE.get((status or "").lower(), "grey")
    prefix = {
        "completed": "✅ 完成",
        "failed": "❌ 失败",
        "interrupted": "⏹ 已中断",
    }.get((status or "").lower(), f"ℹ️ {status}")
    elements = markdown_elements(body) or [{"tag": "markdown", "content": "(无输出)"}]
    return card(elements, title=prefix, template=template)
