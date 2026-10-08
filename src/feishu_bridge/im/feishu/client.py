"""Feishu adapter built on the official ``lark-oapi`` SDK."""

from __future__ import annotations

import json
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageRequest,
    GetMessageResourceRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from ..base import AnchorLostError, ImAdapter, ImError, TopicRef

# Feishu reaction types are a fixed enum. These three are the documented
# values closest to the Telegram bridge's 👀/✅/❌ acknowledgements.
REACTION_TYPES = {"👀": "OnIt", "✅": "DONE", "❌": "ERROR"}


class FeishuError(ImError):
    def __init__(self, message: str, code: Any = None) -> None:
        super().__init__(message)
        self.code = code


# Feishu replies with these when the message being replied to is gone.
# 230011 is "The message was withdrawn"; the text check catches the rest of
# the family without guessing at every numeric code.
ANCHOR_LOST_CODES = {230011, 230009, 230012}


class FeishuClient(ImAdapter):
    def __init__(self, app_id: str, app_secret: str, domain: str, media_dir: Path) -> None:
        self.app_id = app_id
        self.media_dir = media_dir
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    # --- helpers ---------------------------------------------------------
    @staticmethod
    def _check(response: Any, action: str) -> Any:
        code = getattr(response, "code", None)
        if code not in (0, None):
            message = getattr(response, "msg", "") or "unknown error"
            raise FeishuError(f"{action} failed: code={code} msg={message}", code)
        return response

    @staticmethod
    def _is_anchor_lost(exc: FeishuError) -> bool:
        if exc.code in ANCHOR_LOST_CODES:
            return True
        text = str(exc).lower()
        return "withdrawn" in text or "not found" in text

    @staticmethod
    def _body(response: Any) -> Any:
        return getattr(response, "data", None)

    def _send(
        self,
        chat_id: str,
        msg_type: str,
        content: Dict[str, Any],
        uuid_hint: Optional[str] = None,
    ) -> tuple[str, str]:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
            .uuid(uuid_hint or uuid.uuid4().hex)
            .build()
        )
        request = (
            CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        )
        response = self._check(self.client.im.v1.message.create(request), "create message")
        data = self._body(response)
        return str(getattr(data, "message_id", "")), str(getattr(data, "thread_id", "") or "")

    def _reply(
        self,
        message_id: str,
        msg_type: str,
        content: Dict[str, Any],
        uuid_hint: Optional[str] = None,
    ) -> str:
        body = (
            ReplyMessageRequestBody.builder()
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
            .reply_in_thread(True)
            .uuid(uuid_hint or uuid.uuid4().hex)
            .build()
        )
        request = (
            ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        )
        try:
            response = self._check(self.client.im.v1.message.reply(request), "reply message")
        except FeishuError as exc:
            if self._is_anchor_lost(exc):
                raise AnchorLostError(str(exc)) from exc
            raise
        data = self._body(response)
        return str(getattr(data, "message_id", ""))

    # --- topics and messages --------------------------------------------
    def create_topic(self, chat_id: str, text: str) -> TopicRef:
        message_id, thread_id = self._send(chat_id, "text", {"text": text})
        return TopicRef(thread_id=thread_id, root_message_id=message_id)

    def reply_text(self, chat_id: str, root_message_id: str, text: str) -> str:
        return self._reply(root_message_id, "text", {"text": text})

    def reply_card(self, chat_id: str, root_message_id: str, card: Dict[str, Any]) -> str:
        return self._reply(root_message_id, "interactive", card)

    def send_text(self, chat_id: str, text: str) -> str:
        message_id, _ = self._send(chat_id, "text", {"text": text})
        return message_id

    def send_card(self, chat_id: str, card: Dict[str, Any]) -> str:
        message_id, _ = self._send(chat_id, "interactive", card)
        return message_id

    def patch_card(self, message_id: str, card: Dict[str, Any]) -> None:
        body = (
            PatchMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        request = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        self._check(self.client.im.v1.message.patch(request), "patch card")

    def send_image(self, chat_id: str, image_key: str, root_message_id: Optional[str] = None) -> str:
        content = {"image_key": image_key}
        if root_message_id:
            return self._reply(root_message_id, "image", content)
        message_id, _ = self._send(chat_id, "image", content)
        return message_id

    def send_file(
        self,
        chat_id: str,
        file_key: str,
        file_name: str,
        root_message_id: Optional[str] = None,
    ) -> str:
        content = {"file_key": file_key, "file_name": file_name}
        if root_message_id:
            return self._reply(root_message_id, "file", content)
        message_id, _ = self._send(chat_id, "file", content)
        return message_id

    def react(self, message_id: str, emoji: str) -> None:
        reaction_type = REACTION_TYPES.get(emoji)
        if not reaction_type:
            return
        body = (
            CreateMessageReactionRequestBody.builder()
            .reaction_type({"emoji_type": reaction_type})
            .build()
        )
        request = (
            CreateMessageReactionRequest.builder().message_id(message_id).request_body(body).build()
        )
        # Reactions are decorative: a failure must never fail the delivery.
        try:
            self._check(self.client.im.v1.message_reaction.create(request), "react to message")
        except Exception:
            logging.debug("Reaction %s on %s was rejected", emoji, message_id, exc_info=True)

    # --- media -----------------------------------------------------------
    def upload_image(self, path: Path) -> str:
        with path.open("rb") as handle:
            body = (
                CreateImageRequestBody.builder().image(handle).image_type("message").build()
            )
            request = CreateImageRequest.builder().request_body(body).build()
            response = self._check(self.client.im.v1.image.create(request), "upload image")
        return str(getattr(self._body(response), "image_key", ""))

    def upload_file(self, path: Path, file_type: str = "stream") -> str:
        with path.open("rb") as handle:
            body = (
                CreateFileRequestBody.builder()
                .file(handle)
                .file_name(path.name)
                .file_type(file_type)
                .build()
            )
            request = CreateFileRequest.builder().request_body(body).build()
            response = self._check(self.client.im.v1.file.create(request), "upload file")
        return str(getattr(self._body(response), "file_key", ""))

    def download_resource(self, message_id: str, file_key: str, resource_type: str) -> Path:
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        response = self._check(
            self.client.im.v1.message_resource.get(request), "download resource"
        )
        stream = getattr(response, "file", None)
        if stream is None:
            raise FeishuError("message resource download returned no stream")
        suffix = Path(file_key).suffix or mimetypes.guess_extension(
            getattr(response, "file_name", "") or ""
        ) or ""
        target = self.media_dir / f"{message_id}_{file_key[:24]}{suffix}"
        data = stream.read() if hasattr(stream, "read") else stream
        if isinstance(data, str):
            data = data.encode("utf-8")
        target.write_bytes(data)
        return target

    def message_text(self, message_id: str) -> str:
        request = GetMessageRequest.builder().message_id(message_id).build()
        try:
            response = self._check(self.client.im.v1.message.get(request), "get message")
        except FeishuError:
            return ""
        items = getattr(self._body(response), "items", None) or []
        if not items:
            return ""
        raw = getattr(getattr(items[0], "body", None), "content", "") or ""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return str(raw)
        return str(payload.get("text") or payload.get("content") or raw)
