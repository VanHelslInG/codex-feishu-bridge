"""Markdown -> Feishu card rendering.

Feishu cards render a markdown subset. Tables are not usable on mobile, so we
flatten them the same way the Telegram bridge does; local image paths are
reported as plain text because the media is delivered as separate messages.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

CARD_CONTENT_LIMIT = 3800

_FENCED = re.compile(r"```.*?```", re.DOTALL)
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?[\s:\-|]+\|?\s*$")
_LOCAL_IMAGE = re.compile(r"!\[([^\]]*)\]\((?:/|[A-Za-z]:[\\/])[^)]+\)")
_LOCAL_LINK = re.compile(r"\[([^\]]+)\]\((?:/|[A-Za-z]:[\\/])[^)]+\)")


def has_table(text: str) -> bool:
    return any(_TABLE_ROW.match(line) for line in text.splitlines())


def _split_row(line: str) -> List[str]:
    match = _TABLE_ROW.match(line)
    if not match:
        return []
    return [cell.strip() for cell in match.group(1).split("|")]


def tables_to_lists(text: str) -> str:
    """Rewrite pipe tables into ``- header: value`` bullets."""
    lines = text.splitlines()
    out: List[str] = []
    index = 0
    while index < len(lines):
        if _TABLE_ROW.match(lines[index]) and index + 1 < len(lines) and _TABLE_RULE.match(
            lines[index + 1]
        ):
            header = _split_row(lines[index])
            index += 2
            while index < len(lines) and _TABLE_ROW.match(lines[index]):
                cells = _split_row(lines[index])
                if header and len(cells) > 1:
                    first = cells[0]
                    pairs = [
                        f"{header[i] if i < len(header) else i}: {cells[i]}"
                        for i in range(1, len(cells))
                    ]
                    out.append(f"- **{first}** — " + "；".join(pairs))
                else:
                    out.append("- " + " | ".join(cells))
                index += 1
            out.append("")
            continue
        out.append(lines[index])
        index += 1
    return "\n".join(out)


def strip_local_media(text: str) -> str:
    """Replace absolute local paths with readable placeholders."""
    text = _LOCAL_IMAGE.sub(lambda m: f"[图片 {m.group(1) or '已发送'}]", text)
    return _LOCAL_LINK.sub(lambda m: m.group(1), text)


def sanitize(text: str) -> str:
    """Prepare model output for a Feishu card."""
    text = text.replace("\r\n", "\n")
    text = strip_local_media(text)
    if has_table(text):
        # Keep tables inside code fences verbatim; only rewrite prose tables.
        parts = _FENCED.split(text)
        fences = _FENCED.findall(text)
        rebuilt: List[str] = []
        for position, part in enumerate(parts):
            rebuilt.append(tables_to_lists(part))
            if position < len(fences):
                rebuilt.append(fences[position])
        text = "".join(rebuilt)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    return text.strip()


def chunk(text: str, size: int = CARD_CONTENT_LIMIT) -> List[str]:
    """Split long text on paragraph boundaries to fit card element limits."""
    if len(text) <= size:
        return [text] if text else []
    chunks: List[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(block) > size:
            chunks.append(block[:size])
            block = block[size:]
        current = block
    if current:
        chunks.append(current)
    return chunks


def markdown_elements(text: str) -> List[Dict[str, Any]]:
    return [{"tag": "markdown", "content": block} for block in chunk(sanitize(text))]


def text_card(text: str, title: str = "") -> Dict[str, Any]:
    """A plain text reply rendered as a card."""
    elements = markdown_elements(text) or [{"tag": "markdown", "content": "(空)"}]
    return card(elements, title=title)


def card(
    elements: List[Dict[str, Any]],
    title: str = "",
    template: str = "blue",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "elements": elements,
    }
    if title:
        payload["header"] = {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        }
    return payload


def button_row(buttons: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"tag": "action", "actions": buttons}


def button(label: str, value: Dict[str, Any], kind: str = "default") -> Dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": kind,
        "value": value,
    }


def status_prefix(status: str) -> str:
    normalized = (status or "").lower()
    if normalized == "completed":
        return "✅ 完成"
    if normalized == "failed":
        return "❌ 失败"
    if normalized == "interrupted":
        return "⏹ 已中断"
    return f"ℹ️ {status or 'unknown'}"
