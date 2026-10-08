"""Instant-messaging adapter contract.

The core never talks to Feishu directly: everything goes through this
interface, which is what makes the integration tests possible with an
in-memory fake.

A "topic" is the unit that maps to one Codex task. In Feishu that is a
message thread in a topic-mode group; its identity is the message id of the
thread's first message, plus the thread id Feishu assigns to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class TopicRef:
    thread_id: str
    root_message_id: str


class ImError(RuntimeError):
    """Any failure while talking to the messaging platform."""


class AnchorLostError(ImError):
    """The message a reply was anchored to no longer exists.

    Feishu topics hang off their first message: once that message is recalled
    or deleted, every later ``reply_in_thread`` into the topic fails. The
    adapter raises this instead of a generic error so the bridge can fall back
    to posting into the chat directly.
    """


class ImAdapter:
    # --- outbound -------------------------------------------------------
    def create_topic(self, chat_id: str, text: str) -> TopicRef:
        """Post the first message of a new topic and return its identity."""
        raise NotImplementedError

    def reply_text(self, chat_id: str, root_message_id: str, text: str) -> str:
        """Reply inside an existing topic; returns the new message id."""
        raise NotImplementedError

    def reply_card(self, chat_id: str, root_message_id: str, card: Dict[str, Any]) -> str:
        raise NotImplementedError

    def send_text(self, chat_id: str, text: str) -> str:
        raise NotImplementedError

    def send_card(self, chat_id: str, card: Dict[str, Any]) -> str:
        raise NotImplementedError

    def patch_card(self, message_id: str, card: Dict[str, Any]) -> None:
        raise NotImplementedError

    def send_image(self, chat_id: str, image_key: str, root_message_id: Optional[str] = None) -> str:
        raise NotImplementedError

    def send_file(
        self,
        chat_id: str,
        file_key: str,
        file_name: str,
        root_message_id: Optional[str] = None,
    ) -> str:
        raise NotImplementedError

    def react(self, message_id: str, emoji: str) -> None:
        """Best-effort acknowledgement reaction; failures are non-fatal."""
        raise NotImplementedError

    # --- inbound --------------------------------------------------------
    def upload_image(self, path: Path) -> str:
        raise NotImplementedError

    def upload_file(self, path: Path, file_type: str = "stream") -> str:
        raise NotImplementedError

    def download_resource(self, message_id: str, file_key: str, resource_type: str) -> Path:
        raise NotImplementedError

    def message_text(self, message_id: str) -> str:
        """Fetch the plain text of a message (used for quoted replies)."""
        raise NotImplementedError

    def close(self) -> None:
        return None
