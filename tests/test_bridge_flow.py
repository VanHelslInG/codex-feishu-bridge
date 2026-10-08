"""End-to-end flow tests against an in-memory Feishu and Codex."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from feishu_bridge.bridge import Bridge
from feishu_bridge.core.config import merge_defaults
from feishu_bridge.core.store import Store

from .fakes import FakeApp, FakeIm, FakePlatform


def build(tmp_path, **config_overrides) -> tuple[Bridge, Store, FakeIm, FakeApp]:
    projects = {"codex": str(tmp_path / "workspace")}
    (tmp_path / "workspace").mkdir(exist_ok=True)
    config = merge_defaults(
        {
            "projects": projects,
            "quick_start": {"project_keywords": {}},
            "health_port": 0,
            **config_overrides,
        }
    )
    store = Store(tmp_path / "state.sqlite3")
    im = FakeIm()
    app = FakeApp()
    bridge = Bridge(config, FakePlatform(), store, im, app, tmp_path)
    bridge.app_events = app.events
    return bridge, store, im, app


def message_event(**overrides: Any) -> Dict[str, Any]:
    event = {
        "kind": "message",
        "event_id": "evt-1",
        "message_id": "om_100",
        "chat_id": "oc_1",
        "chat_type": "group",
        "thread_id": "omt_1",
        "root_id": "om_100",
        "parent_id": "",
        "msg_type": "text",
        "content": json.dumps({"text": "codex 检查构建"}, ensure_ascii=False),
        "mentions": [],
        "sender_open_id": "ou_1",
        "sender_type": "user",
    }
    event.update(overrides)
    return event


def drain(bridge: Bridge) -> None:
    """Deliver everything currently queued in the outbox."""
    while True:
        row = bridge.store.claim_outbox("test")
        if row is None:
            return
        bridge._deliver(row)
        bridge.store.complete_outbox(int(row["id"]))


def buttons(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for element in card.get("elements") or []:
        for action in element.get("actions") or []:
            result.append(action.get("value") or {})
    return result


def find_card(im: FakeIm, needle: str) -> Optional[Dict[str, Any]]:
    for _, payload in im.sent:
        if isinstance(payload, dict) and needle in json.dumps(payload, ensure_ascii=False):
            return payload
    for _, _, payload in im.replies:
        if isinstance(payload, dict) and needle in json.dumps(payload, ensure_ascii=False):
            return payload
    return None


def draft_token(store: Store) -> str:
    with store.lock:
        row = store.db.execute(
            "SELECT key FROM settings WHERE key LIKE 'draft:%' AND value <> '' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "expected an open task draft"
    return str(row["key"]).split(":", 1)[1]


def bind(bridge: Bridge, store: Store) -> None:
    code = store.pair_code()
    bridge._handle_message(
        message_event(
            content=json.dumps({"text": f"/bind {code}"}), event_id="bind-1", message_id="om_bind"
        )
    )
    assert store.is_bound("oc_1", "ou_1")


def test_unbound_sender_is_ignored(tmp_path):
    bridge, store, im, app = build(tmp_path)
    bridge._handle_message(message_event())
    drain(bridge)
    assert app.called("turn/start") == []
    assert any("配对" in str(payload) for _, payload in im.sent) or im.sent
    store.close()


def test_bind_rotates_the_pair_code(tmp_path):
    bridge, store, _, _ = build(tmp_path)
    first = store.pair_code()
    bind(bridge, store)
    assert store.pair_code() != first
    store.close()


def test_first_task_asks_for_a_model_then_starts(tmp_path):
    bridge, store, im, app = build(tmp_path)
    bind(bridge, store)

    bridge._handle_message(message_event())
    drain(bridge)
    picker = find_card(im, "选择模型")
    assert picker is not None, "the first task must ask for a model"
    assert app.called("thread/start") == []

    token = draft_token(store)
    bridge._handle_card_action(
        {
            "kind": "card_action",
            "event_id": "card-1",
            "chat_id": "oc_1",
            "operator_open_id": "ou_1",
            "message_id": "om_card",
            "value": {"action": "model", "model": "ark-code-latest", "thread": token},
        }
    )
    drain(bridge)

    starts = app.called("thread/start")
    assert len(starts) == 1
    turns = app.called("turn/start")
    assert len(turns) == 1
    assert turns[0]["model"] == "ark-code-latest"
    assert turns[0]["cwd"].endswith("workspace")
    assert any(
        part.get("type") == "text" and "检查构建" in str(part.get("text"))
        for part in turns[0]["input"]
    )
    # The topic already existed, so no extra topic root is created.
    assert im.topics == []
    store.close()


def test_second_task_reuses_the_chosen_model(tmp_path):
    bridge, store, im, app = build(tmp_path)
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    token = draft_token(store)
    bridge._handle_card_action(
        {
            "kind": "card_action",
            "event_id": "card-1",
            "chat_id": "oc_1",
            "operator_open_id": "ou_1",
            "message_id": "om_card",
            "value": {"action": "model", "model": "deepseek-v4.1-flash", "thread": token},
        }
    )
    drain(bridge)

    # A subsequent message needs no model picker any more.
    bridge._handle_message(
        message_event(message_id="om_101", event_id="evt-2", thread_id="omt_1")
    )
    drain(bridge)
    turns = app.called("turn/start")
    assert turns[-1]["model"] == "deepseek-v4.1-flash"
    store.close()


def test_explicit_model_in_config_skips_the_picker(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    assert find_card(im, "选择模型") is None
    assert len(app.called("turn/start")) == 1
    store.close()


def texts(im: FakeIm) -> List[str]:
    result = [str(payload) for _, payload in im.sent]
    result += [str(payload) for _, _, payload in im.replies]
    return result


def start_first_task(bridge: Bridge, store: Store, im: FakeIm) -> str:
    """Create the task and return its Codex thread id."""
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    token = draft_token(store)
    bridge._handle_card_action(
        {
            "kind": "card_action",
            "event_id": "card-1",
            "chat_id": "oc_1",
            "operator_open_id": "ou_1",
            "message_id": "om_card",
            "value": {"action": "model", "model": "ark-code-latest", "thread": token},
        }
    )
    drain(bridge)
    thread_id = str(store.binding("oc_1")["current_thread_id"])
    # The fake never emits turn/completed, so close the first turn by hand —
    # otherwise the thread still looks busy and later dispatches are skipped.
    store.finish_running(thread_id, "turn_1")
    return thread_id


def test_turn_input_carries_no_bridge_protocol(tmp_path):
    bridge, store, im, app = build(tmp_path)
    start_first_task(bridge, store, im)

    starts = app.called("thread/start")
    assert starts and "BRIDGE_NEXT_ACTIONS" in str(starts[0]["developerInstructions"])
    turns = app.called("turn/start")
    assert len(turns) == 1
    payload = json.dumps(turns[0]["input"], ensure_ascii=False)
    # The protocol lives in developerInstructions; a second copy in the user
    # message is what used to leak into the thread title.
    assert "bridge_runtime_instruction" not in payload
    assert "检查构建" in payload
    store.close()


def test_writer_conflict_is_reported_once(tmp_path):
    bridge, store, im, app = build(tmp_path)
    thread_id = start_first_task(bridge, store, im)
    app.resume_error = f"thread {thread_id} already has an active writer"
    bridge.fresh_threads.clear()  # what a bridge restart looks like

    bridge._handle_message(message_event(message_id="om_101", event_id="evt-2"))
    drain(bridge)

    assert len(app.called("turn/start")) == 1, "a locked thread must not launch a turn"
    assert sum("另一个 Codex 客户端占用了" in text for text in texts(im)) == 1

    # The scheduler keeps retrying every few seconds; the topic must not fill up.
    bridge._safe_dispatch(thread_id)
    drain(bridge)
    assert sum("另一个 Codex 客户端占用了" in text for text in texts(im)) == 1
    store.close()


def test_foreign_turn_on_the_thread_is_reported(tmp_path):
    bridge, store, im, app = build(tmp_path)
    thread_id = start_first_task(bridge, store, im)
    app.thread_turns = [{"id": "turn_from_the_desktop_app"}]
    bridge.fresh_threads.clear()

    bridge._handle_message(message_event(message_id="om_101", event_id="evt-2"))
    drain(bridge)

    assert any("在本机其它 Codex 客户端里被继续过" in text for text in texts(im))
    assert len(app.called("turn/start")) == 2
    assert store.turn_ids(thread_id) == {"turn_1", "turn_2"}
    store.close()


def test_lost_thread_is_replaced_and_the_message_still_runs(tmp_path):
    """A thread the app-server forgot must be replaced, not retried forever."""
    bridge, store, im, app = build(tmp_path)
    thread_id = start_first_task(bridge, store, im)
    route_before = store.route(thread_id)
    app.resume_error = f"no rollout found for thread id {thread_id}"
    bridge.fresh_threads.clear()  # what a bridge restart looks like

    bridge._handle_message(message_event(message_id="om_101", event_id="evt-2"))
    drain(bridge)

    new_thread = str(store.binding("oc_1")["current_thread_id"])
    assert new_thread != thread_id, "the lost thread must be replaced"
    assert store.route(thread_id) is None, "the dead route must be gone"
    migrated = store.route(new_thread)
    assert migrated is not None
    assert migrated["feishu_thread_id"] == route_before["feishu_thread_id"]
    assert migrated["root_message_id"] == route_before["root_message_id"]

    turns = app.called("turn/start")
    assert turns[-1]["threadId"] == new_thread
    assert any("运行上下文在 Codex 侧丢了" in text for text in texts(im))
    assert store.running_turn(new_thread) is not None
    store.close()


def test_thread_recovery_is_attempted_once_per_message(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    old_thread = str(store.binding("oc_1")["current_thread_id"])
    store.recover_queue()
    queued = store.next_queued(old_thread)
    assert queued is not None

    bridge._recover_lost_thread(old_thread, queued)
    created = len(app.called("thread/start"))
    assert created == 2  # the original task plus one replacement

    # A second attempt for the same message must not spawn another thread.
    bridge._recover_lost_thread(old_thread, queued)
    drain(bridge)
    assert len(app.called("thread/start")) == created
    status = store.db.execute(
        "SELECT status FROM queued_turns WHERE id = ?", (queued["id"],)
    ).fetchone()["status"]
    assert status == "failed"
    assert any("无法恢复" in text for text in texts(im))
    store.close()


def test_turn_completion_renders_the_result_and_next_actions(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    thread_id = app.called("thread/start")[0] and store.binding("oc_1")["current_thread_id"]
    turn_id = app.called("turn/start") and "turn_1"

    bridge._handle_app_event(
        {
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "type": "agentMessage",
                    "text": (
                        "构建通过。\n\n"
                        '<!-- BRIDGE_NEXT_ACTIONS:[{"label":"部署","prompt":"部署到预发"}] -->'
                    ),
                },
            },
        }
    )
    bridge._handle_app_event(
        {"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}}}
    )
    drain(bridge)

    result = find_card(im, "构建通过")
    assert result is not None
    assert "完成" in json.dumps(result, ensure_ascii=False)
    values = buttons(result)
    assert any(value.get("action") == "next" for value in values)
    assert store.running_turn(thread_id) is None
    store.close()


def test_next_action_button_submits_a_new_turn(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    thread_id = store.binding("oc_1")["current_thread_id"]

    bridge._handle_app_event(
        {
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": "turn_1",
                "item": {
                    "type": "agentMessage",
                    "text": '好了 <!-- BRIDGE_NEXT_ACTIONS:[{"label":"继续","prompt":"继续修复"}] -->',
                },
            },
        }
    )
    bridge._handle_app_event(
        {"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": "turn_1", "status": "completed"}}}
    )
    drain(bridge)
    card = find_card(im, "好了")
    next_value = next(value for value in buttons(card) if value.get("action") == "next")

    bridge._handle_card_action(
        {
            "kind": "card_action",
            "event_id": "card-2",
            "chat_id": "oc_1",
            "operator_open_id": "ou_1",
            "message_id": "om_card",
            "value": next_value,
        }
    )
    turns = app.called("turn/start")
    assert any(
        "继续修复" in str(part.get("text"))
        for part in turns[-1]["input"]
        for part in [part] if part.get("type") == "text"
    )
    store.close()


def test_command_approval_card_and_decision(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    thread_id = store.binding("oc_1")["current_thread_id"]

    bridge._handle_app_event(
        {
            "id": 99,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": thread_id, "command": "git push origin main", "cwd": "/tmp"},
        }
    )
    drain(bridge)
    card = find_card(im, "需要授权")
    assert card is not None
    values = buttons(card)
    accept = next(value for value in values if value.get("decision") == "accept")

    bridge._handle_card_action(
        {
            "kind": "card_action",
            "event_id": "card-3",
            "chat_id": "oc_1",
            "operator_open_id": "ou_1",
            "message_id": "om_card",
            "value": accept,
        }
    )
    assert app.responses[-1] == (99, {"decision": "accept"})
    drain(bridge)
    assert im.patches, "the original card must be updated in place"
    store.close()


def test_expired_approval_marks_the_card(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_card_action(
        {
            "kind": "card_action",
            "event_id": "card-4",
            "chat_id": "oc_1",
            "operator_open_id": "ou_1",
            "message_id": "om_card",
            "value": {"action": "approval", "token": "missing", "decision": "accept"},
        }
    )
    drain(bridge)
    assert im.patches and "失效" in json.dumps(im.patches[-1][1], ensure_ascii=False)
    store.close()


def test_queued_message_waits_for_the_running_turn(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    thread_id = store.binding("oc_1")["current_thread_id"]

    bridge._handle_message(message_event(message_id="om_200", event_id="evt-2"))
    drain(bridge)
    assert len(app.called("turn/start")) == 1
    running, queued = store.queue_counts(thread_id)
    assert running == 1 and queued == 1
    assert find_card(im, "已进入队列") is not None

    bridge._handle_app_event(
        {"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": "turn_1", "status": "completed"}}}
    )
    drain(bridge)
    assert len(app.called("turn/start")) == 2
    store.close()


def test_restart_recovers_interrupted_turns(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    thread_id = store.binding("oc_1")["current_thread_id"]
    assert store.running_turn(thread_id) is not None

    # Simulate a crash: state stays "running" with no process to finish it.
    assert store.recover_queue() == 1
    assert store.running_turn(thread_id) is None
    assert store.next_queued(thread_id) is not None
    store.close()


def test_duplicate_event_is_not_processed_twice(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    event = message_event(event_id="evt-dup")
    bridge._intake(event)
    bridge._intake(event)
    with store.lock:
        count = store.db.execute(
            "SELECT COUNT(*) AS n FROM feishu_events WHERE event_id = 'evt-dup'"
        ).fetchone()["n"]
    assert count == 1
    store.close()


def test_help_and_status_commands_answer(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(
        message_event(content=json.dumps({"text": "/help"}), event_id="c1", message_id="om_h")
    )
    drain(bridge)
    assert find_card(im, "可用指令") is not None

    bridge._handle_message(
        message_event(content=json.dumps({"text": "/status"}), event_id="c2", message_id="om_s")
    )
    drain(bridge)
    assert find_card(im, "还没有绑定") is not None
    store.close()


def test_projects_command_lists_aliases(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(
        message_event(content=json.dumps({"text": "/projects"}), event_id="c3", message_id="om_p")
    )
    drain(bridge)
    card = find_card(im, "可用项目")
    assert card is not None and "codex" in json.dumps(card, ensure_ascii=False)
    store.close()


def test_image_message_becomes_a_local_image_input(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"\x89PNG\r\n")
    im.downloads["img_key"] = image_path
    bridge._handle_message(
        message_event(
            event_id="img-1",
            message_id="om_img",
            msg_type="image",
            content=json.dumps({"image_key": "img_key"}),
        )
    )
    drain(bridge)
    turns = app.called("turn/start")
    assert turns
    assert any(part.get("type") == "localImage" for part in turns[0]["input"])
    store.close()


def test_post_message_with_an_inline_image_still_yields_the_image(tmp_path):
    """Text and image composed together arrive as one rich-text message."""
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    image_path = tmp_path / "inline.png"
    image_path.write_bytes(b"\x89PNG\r\n")
    im.downloads["img_inline"] = image_path
    content = json.dumps(
        {
            "title": "",
            "content": [
                [
                    {"tag": "text", "text": "这是什么", "style": []},
                    {"tag": "img", "image_key": "img_inline"},
                ]
            ],
        },
        ensure_ascii=False,
    )
    bridge._handle_message(
        message_event(
            event_id="post-1",
            message_id="om_post",
            msg_type="post",
            content=content,
        )
    )
    drain(bridge)

    turns = app.called("turn/start")
    assert turns, "an inline image plus text must still start a turn"
    payload = turns[0]["input"]
    assert any(part.get("type") == "localImage" for part in payload)
    assert any(
        part.get("type") == "text" and "这是什么" in str(part.get("text"))
        for part in payload
    )
    store.close()


def test_hostile_artifact_path_is_rejected(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    bridge._capture_artifacts("th_1", "turn_1", {"type": "file", "path": str(outside)})
    assert store.pending_artifacts("th_1") == []
    store.close()


def test_artifact_inside_the_workspace_is_delivered(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    workspace = tmp_path / "workspace"
    shot = workspace / "chart.png"
    shot.write_bytes(b"\x89PNG\r\n")
    store.route_thread("th_1", "oc_1", project="codex", root_message_id="om_9")
    bridge._capture_artifacts("th_1", "turn_1", {"type": "file", "path": str(shot)})
    assert len(store.pending_artifacts("th_1")) == 1
    bridge._flush_artifacts("oc_1", "th_1")
    drain(bridge)
    assert im.uploads, "the artifact must be uploaded to Feishu"
    store.close()


def test_health_payload_reports_state(tmp_path):
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    payload = bridge.health()
    assert payload["ok"] is True
    assert payload["app_server_pid"] == 4242
    store.close()


def test_long_running_turn_reports_progress_instead_of_silence(tmp_path, monkeypatch):
    """A slow turn must say something before it finishes."""
    from feishu_bridge import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "FIRST_PROGRESS_NOTICE_SECONDS", 0.2)
    monkeypatch.setattr(bridge_module, "PROGRESS_HEARTBEAT_SECONDS", 0.2)

    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    thread_id = store.binding("oc_1")["current_thread_id"]
    assert store.running_turn(thread_id) is not None
    assert find_card(im, "执行进度") is None, "nothing should be sent before the delay"

    deadline = time.time() + 5
    card = None
    while time.time() < deadline and card is None:
        time.sleep(0.05)
        drain(bridge)
        card = find_card(im, "执行进度")

    assert card is not None, "the bridge must report progress while a turn runs"
    text = json.dumps(card, ensure_ascii=False)
    assert "执行中" in text
    store.close()


def test_progress_notice_stops_after_the_turn_finishes(tmp_path, monkeypatch):
    from feishu_bridge import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "FIRST_PROGRESS_NOTICE_SECONDS", 0.2)
    monkeypatch.setattr(bridge_module, "PROGRESS_HEARTBEAT_SECONDS", 0.2)

    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    thread_id = store.binding("oc_1")["current_thread_id"]

    bridge._handle_app_event(
        {
            "method": "turn/completed",
            "params": {"threadId": thread_id, "turn": {"id": "turn_1", "status": "completed"}},
        }
    )
    drain(bridge)
    before = len(im.sent) + len(im.replies)
    time.sleep(0.6)
    drain(bridge)
    assert len(im.sent) + len(im.replies) == before, "no progress after completion"
    store.close()


def test_withdrawn_topic_anchor_falls_back_to_a_plain_message(tmp_path):
    """A recalled topic root must not swallow the answer."""
    bridge, store, im, app = build(tmp_path, default_model="ark-code-latest")
    bind(bridge, store)
    bridge._handle_message(message_event())
    drain(bridge)
    thread_id = str(store.binding("oc_1")["current_thread_id"])
    store.route_thread(
        thread_id, "oc_1", root_message_id="om_root", feishu_thread_id="omt_keep"
    )
    im.anchor_error = "reply message failed: code=230011 msg=The message was withdrawn."

    bridge._send_text("oc_1", "这是任务结果", thread_id=thread_id)
    drain(bridge)

    assert any("这是任务结果" in str(payload) for _, payload in im.sent), (
        "the answer must be posted into the chat when the topic anchor is gone"
    )
    assert store.route(thread_id)["root_message_id"] is None
    assert store.route(thread_id)["feishu_thread_id"] == "omt_keep", (
        "topic routing must survive losing the delivery anchor"
    )
    assert any("起始消息被撤回" in str(payload) for _, payload in im.sent)
    store.close()


def test_model_choice_persists_in_an_allowlisted_chat_without_a_binding(tmp_path):
    """An allowlisted chat is never /bind'ed, so it has no binding row to update."""
    bridge, store, im, app = build(
        tmp_path, feishu={"allowlist": {"open_ids": ["ou_1"], "chat_ids": []}}
    )
    assert store.binding("oc_1") is None

    def pickers() -> int:
        cards = [payload for _, payload in im.sent]
        cards += [payload for _, _, payload in im.replies]
        return sum(
            1
            for card in cards
            if isinstance(card, dict) and "选择模型" in json.dumps(card, ensure_ascii=False)
        )

    bridge._handle_message(message_event())
    drain(bridge)
    assert pickers() == 1, "the first task still asks once"

    token = draft_token(store)
    bridge._handle_card_action(
        {
            "kind": "card_action",
            "event_id": "card-model",
            "chat_id": "oc_1",
            "operator_open_id": "ou_1",
            "message_id": "om_card",
            "value": {"action": "model", "model": "ark-code-latest", "thread": token},
        }
    )
    drain(bridge)
    assert store.binding("oc_1") is None, "remembering a model must not imply authority"
    assert store.get_setting("chat_model:oc_1") == "ark-code-latest"

    bridge._handle_message(message_event(message_id="om_102", event_id="evt-3"))
    drain(bridge)
    # Counting matters: the first card stays in the history, so a plain
    # "is there a picker" check can never fail here.
    assert pickers() == 1, "the second task must not ask again"
    latest = store.db.execute(
        "SELECT model FROM queued_turns ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert latest["model"] == "ark-code-latest"
    store.close()
