"""Long-connection event intake.

Feishu pushes cb events over a WebSocket when the app subscribes with the
"long connection" mode, so the bridge needs no public endpoint. The callback
must return within 3 seconds or Feishu retries, therefore the handlers here do
nothing but normalise the event and hand it to the intake callback, which
persists it durably before returning.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.ws import Client as WsClient

Sink = Callable[[Dict[str, Any]], None]


def _mentions(mentions: Any) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    for mention in mentions or []:
        # MentionEvent.id is a nested UserId object, not a bare string.
        identity = getattr(mention, "id", None)
        open_id = getattr(identity, "open_id", "") if identity is not None else ""
        if not open_id and isinstance(identity, str):
            open_id = identity
        result.append(
            {
                "key": str(getattr(mention, "key", "") or ""),
                "open_id": str(open_id or ""),
                "name": str(getattr(mention, "name", "") or ""),
            }
        )
    return result


class WsListener:
    def __init__(self, app_id: str, app_secret: str, domain: str, sink: Sink) -> None:
        self.sink = sink
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._client = WsClient(
            app_id,
            app_secret,
            event_handler=self._build_handler(),
            log_level=lark.LogLevel.WARNING,
            domain=domain,
            auto_reconnect=True,
        )

    # --- handlers --------------------------------------------------------
    def _build_handler(self) -> lark.EventDispatcherHandler:
        return (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_card_action_trigger(self._on_card_action)
            .build()
        )

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        try:
            event = data.event
            header = data.header
            message = event.message
            sender = event.sender
            sender_id = getattr(sender, "sender_id", None)
            payload = {
                "kind": "message",
                "event_id": str(getattr(header, "event_id", "") or message.message_id),
                "event_type": str(getattr(header, "event_type", "") or ""),
                "message_id": str(message.message_id or ""),
                "chat_id": str(message.chat_id or ""),
                "chat_type": str(getattr(message, "chat_type", "") or ""),
                "thread_id": str(message.thread_id or ""),
                "root_id": str(message.root_id or ""),
                "parent_id": str(message.parent_id or ""),
                # The SDK field is message_type; older payloads used msg_type.
                "msg_type": str(
                    getattr(message, "message_type", None)
                    or getattr(message, "msg_type", None)
                    or ""
                ),
                "content": str(message.content or ""),
                "create_time": int(message.create_time or 0),
                "mentions": _mentions(message.mentions),
                "sender_open_id": str(getattr(sender_id, "open_id", "") or ""),
                "sender_type": str(getattr(sender, "sender_type", "") or ""),
            }
            self.sink(payload)
        except Exception:
            # Never raise into the SDK loop: a failure here would break the
            # long connection for every subsequent event.
            logging.exception("Failed to normalise an inbound message event")

    def _on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        try:
            event = data.event
            header = data.header
            context = getattr(event, "context", None)
            operator = getattr(event, "operator", None)
            action = getattr(event, "action", None)
            payload = {
                "kind": "card_action",
                "event_id": str(getattr(header, "event_id", "") or ""),
                "event_type": str(getattr(header, "event_type", "") or ""),
                "message_id": str(getattr(context, "open_message_id", "") or ""),
                "chat_id": str(getattr(context, "open_chat_id", "") or ""),
                "operator_open_id": str(getattr(operator, "open_id", "") or ""),
                "value": dict(getattr(action, "value", None) or {}),
            }
            if not payload["event_id"]:
                payload["event_id"] = (
                    f"card:{payload['message_id']}:"
                    f"{json.dumps(payload['value'], sort_keys=True, ensure_ascii=False)}"
                )
            self.sink(payload)
        except Exception:
            logging.exception("Failed to normalise a card action event")
        return (
            P2CardActionTriggerResponse.builder()
            .toast({"type": "info", "content": "已收到"})
            .build()
        )

    # --- lifecycle -------------------------------------------------------
    def start(self) -> None:
        """Connect in a background thread; returns immediately."""
        self._thread = threading.Thread(target=self._run, name="feishu-ws", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                logging.info("Connecting the Feishu long connection")
                self._client.start()
            except Exception:
                logging.exception("Feishu long connection dropped")
            if self._stop.wait(5):
                return
            logging.info("Reconnecting the Feishu long connection")

    def stop(self) -> None:
        self._stop.set()
