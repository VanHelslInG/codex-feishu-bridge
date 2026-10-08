from __future__ import annotations

import json
import time

from feishu_bridge.core.store import Store


def test_event_idempotency(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    assert store.record_event("evt-1", {"kind": "message"}) is True
    assert store.record_event("evt-1", {"kind": "message"}) is False
    store.close()


def test_turn_idempotency_on_source_message(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    row, created = store.enqueue_turn("oc_1", "th_1", [{"type": "text", "text": "hi"}], "om_1")
    assert created is True
    duplicate, created_again = store.enqueue_turn(
        "oc_1", "th_1", [{"type": "text", "text": "hi"}], "om_1"
    )
    assert created_again is False
    assert duplicate["id"] == row["id"]
    store.close()


def test_lease_excludes_other_worker(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    assert store.acquire_lease("thread:th_1", "worker-a", ttl=60) is True
    assert store.acquire_lease("thread:th_1", "worker-b", ttl=60) is False
    assert store.acquire_lease("thread:th_1", "worker-a", ttl=60) is True
    store.close()


def test_recover_queue_returns_running_turns(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    row, _ = store.enqueue_turn("oc_1", "th_1", [{"type": "text", "text": "hi"}], "om_1")
    store.mark_running(int(row["id"]), "turn_1")
    assert store.running_turn("th_1") is not None
    assert store.recover_queue() == 1
    assert store.running_turn("th_1") is None
    assert store.next_queued("th_1") is not None
    store.close()


def test_coalesce_merges_queued_inputs(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    for index in range(3):
        store.enqueue_turn(
            "oc_1", "th_1", [{"type": "text", "text": f"m{index}"}], f"om_{index}"
        )
    assert store.coalesce_queued_turns("th_1") == 2
    remaining = store.next_queued("th_1")
    assert remaining is not None
    assert "m0" in remaining["inputs_json"] and "m2" in remaining["inputs_json"]
    store.close()


def test_outbox_dedupe_and_retry(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    assert store.enqueue_outbox("oc_1", None, "text", {"text": "a"}, "k1") is True
    assert store.enqueue_outbox("oc_1", None, "text", {"text": "a"}, "k1") is False
    row = store.claim_outbox("worker-a")
    assert row is not None
    store.retry_outbox(int(row["id"]), 1, "boom")
    assert store.claim_outbox("worker-a") is None  # still backing off
    store.close()


def test_route_lookup_by_feishu_thread(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.route_thread(
        "th_1", "oc_1", feishu_thread_id="omt_9", root_message_id="om_9", title="t"
    )
    route = store.route_for_feishu_thread("omt_9")
    assert route is not None and route["thread_id"] == "th_1"
    assert store.thread_for_message("oc_1", "om_9") is None  # only mapped explicitly
    store.close()


def test_artifact_dedupe(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    assert store.add_artifact("th_1", "turn_1", "/tmp/a.png", "fp1") is True
    assert store.add_artifact("th_1", "turn_1", "/tmp/a.png", "fp1") is False
    assert len(store.pending_artifacts("th_1")) == 1
    store.close()


def test_pair_code_rotates(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    first = store.pair_code()
    assert store.pair_code() == first
    assert store.rotate_pair_code() != first
    store.close()


def test_pending_approval_roundtrip(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    token = store.add_pending("req-1", "item/commandExecution/requestApproval", {"a": 1}, "oc_1")
    row = store.pending(token)
    # The JSON-RPC id is stored verbatim so it can be echoed back byte-for-byte.
    assert row is not None and json.loads(row["request_id"]) == "req-1"
    store.delete_pending(token)
    assert store.pending(token) is None
    store.close()


def test_recent_routes_orders_by_update(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.route_thread("th_1", "oc_1", title="first")
    time.sleep(0.01)
    store.route_thread("th_2", "oc_1", title="second")
    routes = store.recent_routes("oc_1")
    assert [route["thread_id"] for route in routes][:2] == ["th_2", "th_1"]
    store.close()
