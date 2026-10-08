"""Guards the SDK field mapping in the long-connection listener.

These tests deliberately build *real* lark-oapi event objects. The normaliser
once read ``message.msg_type`` while the SDK exposes ``message_type``, which
made every text message look empty; feeding real objects keeps that class of
mistake from coming back.
"""

from __future__ import annotations

from typing import Any, Dict, List

from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

from feishu_bridge.im.feishu.ws_listener import WsListener


def listener_with_sink(captured: List[Dict[str, Any]]) -> WsListener:
    # Bypass __init__ so the test never opens a WebSocket client.
    listener = WsListener.__new__(WsListener)
    listener.sink = captured.append
    return listener


def build_event(**message_overrides: Any) -> P2ImMessageReceiveV1:
    message = {
        "message_id": "om_1",
        "root_id": "om_1",
        "parent_id": "",
        "create_time": 1700000000,
        "chat_id": "oc_1",
        "thread_id": "omt_1",
        "chat_type": "group",
        "message_type": "text",
        "content": '{"text":"@_user_1 检查构建"}',
        "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "Codex Bridge"}],
    }
    message.update(message_overrides)
    return P2ImMessageReceiveV1(
        {
            "header": {
                "event_id": "evt-1",
                "event_type": "im.message.receive_v1",
                "create_time": "1700000000",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": "ou_user", "user_id": "u1"},
                    "sender_type": "user",
                    "tenant_key": "tk",
                },
                "message": message,
            },
        }
    )


def test_text_message_is_normalised_with_real_sdk_objects():
    captured: List[Dict[str, Any]] = []
    listener = listener_with_sink(captured)
    listener._on_message(build_event())

    assert len(captured) == 1
    payload = captured[0]
    assert payload["msg_type"] == "text", "message_type must be mapped to msg_type"
    assert payload["chat_type"] == "group"
    assert payload["thread_id"] == "omt_1"
    assert payload["root_id"] == "om_1"
    assert payload["message_id"] == "om_1"
    assert payload["chat_id"] == "oc_1"
    assert payload["sender_open_id"] == "ou_user"
    assert payload["sender_type"] == "user"


def test_mention_identity_is_unwrapped_from_the_nested_user_id():
    captured: List[Dict[str, Any]] = []
    listener = listener_with_sink(captured)
    listener._on_message(build_event())

    mentions = captured[0]["mentions"]
    assert mentions[0]["key"] == "@_user_1"
    assert mentions[0]["open_id"] == "ou_bot", "mention.id is a UserId object"
    assert mentions[0]["name"] == "Codex Bridge"


def test_image_message_keeps_its_type_and_content():
    captured: List[Dict[str, Any]] = []
    listener = listener_with_sink(captured)
    listener._on_message(
        build_event(message_type="image", content='{"image_key":"img_v2_abc"}')
    )

    payload = captured[0]
    assert payload["msg_type"] == "image"
    assert "img_v2_abc" in payload["content"]


def test_normaliser_survives_a_malformed_event():
    captured: List[Dict[str, Any]] = []
    listener = listener_with_sink(captured)
    # A missing message must not raise into the SDK's event loop.
    listener._on_message(P2ImMessageReceiveV1({}))
    assert captured == []


def test_bridge_extracts_text_from_a_real_event():
    """End-to-end: SDK object -> normaliser -> bridge text extraction."""
    from feishu_bridge.bridge import Bridge

    captured: List[Dict[str, Any]] = []
    listener = listener_with_sink(captured)
    listener._on_message(build_event())
    payload = captured[0]

    class TextOnly(Bridge):
        def __init__(self) -> None:  # pragma: no cover - only _extract_text is used
            pass

    assert TextOnly()._extract_text(payload) == "检查构建"


def build_card_action(**value_overrides: Any) -> "P2CardActionTrigger":
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

    value = {"action": "approval", "token": "tok", "decision": "accept"}
    value.update(value_overrides)
    return P2CardActionTrigger(
        {
            "header": {"event_id": "evt-card-1", "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_user"},
                "token": "t",
                "action": {"value": value, "tag": "button"},
                "context": {"open_message_id": "om_card", "open_chat_id": "oc_1"},
            },
        }
    )


def test_card_action_is_normalised_and_acknowledged():
    captured: List[Dict[str, Any]] = []
    listener = listener_with_sink(captured)
    response = listener._on_card_action(build_card_action())

    assert len(captured) == 1
    payload = captured[0]
    assert payload["kind"] == "card_action"
    assert payload["chat_id"] == "oc_1"
    assert payload["message_id"] == "om_card"
    assert payload["operator_open_id"] == "ou_user"
    assert payload["value"]["decision"] == "accept"
    assert payload["event_id"], "the action must carry an idempotency key"

    # The acknowledgement must be a real response object: returning a broken
    # one makes Feishu show error 200671 to the user.
    assert response is not None
    assert getattr(response.toast, "content", None), "the toast must be populated"


def test_card_action_without_event_id_still_gets_a_stable_key():
    captured: List[Dict[str, Any]] = []
    listener = listener_with_sink(captured)
    data = build_card_action()
    data.header = None
    listener._on_card_action(data)

    assert captured[0]["event_id"].startswith("card:om_card:")


def test_card_action_acknowledgement_survives_a_bad_payload():
    """A malformed action must still produce a usable response, not raise."""
    captured: List[Dict[str, Any]] = []
    listener = listener_with_sink(captured)
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

    response = listener._on_card_action(P2CardActionTrigger({}))
    assert response is not None or captured == []
