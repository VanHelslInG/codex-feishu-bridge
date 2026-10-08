"""Durable state store.

SQLite in WAL mode is the single source of truth, exactly as in the Telegram
bridge: inbound events, queued turns, worker leases, the outbox, approvals and
artifacts all survive a restart.

Feishu identifiers are opaque strings: chat_id ``oc_...``, message_id
``om_...``, thread_id ``omt_...``, open_id ``ou_...``.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bindings (
    chat_id TEXT PRIMARY KEY,
    open_id TEXT NOT NULL,
    user_name TEXT,
    current_thread_id TEXT,
    current_model TEXT,
    current_effort TEXT,
    current_project TEXT,
    bound_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS message_routes (
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    PRIMARY KEY(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS thread_routes (
    thread_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    feishu_thread_id TEXT,
    root_message_id TEXT,
    title TEXT,
    project TEXT,
    last_turn_id TEXT,
    final_text TEXT,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS thread_routes_feishu_thread
ON thread_routes(feishu_thread_id);
CREATE TABLE IF NOT EXISTS pending_requests (
    token TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    method TEXT NOT NULL,
    params_json TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_items (
    item_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS next_actions (
    token TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    label TEXT NOT NULL,
    prompt TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    used_at INTEGER
);
CREATE TABLE IF NOT EXISTS queued_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    cwd TEXT,
    model TEXT,
    effort TEXT,
    source_message_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    turn_id TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    UNIQUE(chat_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS queued_turns_thread_status
ON queued_turns(thread_id, status, id);
CREATE TABLE IF NOT EXISTS task_creations (
    chat_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    PRIMARY KEY(chat_id, source_message_id)
);
CREATE TABLE IF NOT EXISTS turn_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    turn_id TEXT,
    path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    sent_at INTEGER,
    UNIQUE(thread_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS turn_artifacts_pending
ON turn_artifacts(thread_id, status, id);
CREATE TABLE IF NOT EXISTS feishu_events (
    event_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    received_at INTEGER NOT NULL,
    processed_at INTEGER
);
CREATE TABLE IF NOT EXISTS feishu_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    thread_id TEXT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at INTEGER,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    sent_at INTEGER
);
CREATE INDEX IF NOT EXISTS feishu_outbox_ready
ON feishu_outbox(status, available_at, id);
CREATE TABLE IF NOT EXISTS worker_leases (
    resource_key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    heartbeat_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS thread_handoffs (
    source_thread_id TEXT PRIMARY KEY,
    continuation_thread_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""

# Everything the queue can hold. Each kind is delivered by the outbox worker.
OUTBOX_KINDS = ("text", "card", "card_patch", "image", "file", "react")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # --- settings --------------------------------------------------------
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self.db.commit()

    # --- pairing ---------------------------------------------------------
    def pair_code(self) -> str:
        current = self.get_setting("pair_code")
        if current:
            return current
        code = f"{secrets.randbelow(10**8):08d}"
        self.set_setting("pair_code", code)
        return code

    def rotate_pair_code(self) -> str:
        code = f"{secrets.randbelow(10**8):08d}"
        self.set_setting("pair_code", code)
        return code

    def is_bound(self, chat_id: str, open_id: str) -> bool:
        with self.lock:
            row = self.db.execute(
                "SELECT 1 FROM bindings WHERE chat_id = ? AND open_id = ?",
                (chat_id, open_id),
            ).fetchone()
            return row is not None

    def bind(self, chat_id: str, open_id: str, user_name: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO bindings(chat_id, open_id, user_name, bound_at) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET open_id = excluded.open_id, "
                "user_name = excluded.user_name",
                (chat_id, open_id, user_name, int(time.time())),
            )
            self.db.commit()

    def binding(self, chat_id: str) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM bindings WHERE chat_id = ?", (chat_id,)
            ).fetchone()

    def bound_chats(self) -> List[str]:
        with self.lock:
            rows = self.db.execute("SELECT chat_id FROM bindings").fetchall()
            return [str(row["chat_id"]) for row in rows]

    def update_binding(self, chat_id: str, **values: Optional[str]) -> None:
        if not values:
            return
        columns = ", ".join(f"{key} = ?" for key in values)
        with self.lock:
            self.db.execute(
                f"UPDATE bindings SET {columns} WHERE chat_id = ?",
                (*values.values(), chat_id),
            )
            self.db.commit()

    # --- inbound events --------------------------------------------------
    def record_event(self, event_id: str, payload: Dict[str, Any]) -> bool:
        """Return True when this event is new, False when it is a replay."""
        with self.lock:
            try:
                self.db.execute(
                    "INSERT INTO feishu_events(event_id, payload_json, received_at) "
                    "VALUES(?, ?, ?)",
                    (event_id, json.dumps(payload, ensure_ascii=False), int(time.time())),
                )
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def finish_event(self, event_id: str, error: Optional[str] = None) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE feishu_events SET status = ?, attempts = attempts + 1, "
                "last_error = ?, processed_at = ? WHERE event_id = ?",
                ("failed" if error else "done", error, int(time.time()), event_id),
            )
            self.db.commit()

    def unfinished_events(self, limit: int = 50) -> List[sqlite3.Row]:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM feishu_events WHERE status = 'received' "
                "ORDER BY received_at LIMIT ?",
                (limit,),
            ).fetchall()

    # --- outbox ----------------------------------------------------------
    def enqueue_outbox(
        self,
        chat_id: str,
        thread_id: Optional[str],
        kind: str,
        payload: Dict[str, Any],
        dedupe_key: str,
        available_at: Optional[float] = None,
    ) -> bool:
        with self.lock:
            try:
                self.db.execute(
                    "INSERT INTO feishu_outbox(dedupe_key, chat_id, thread_id, kind, "
                    "payload_json, available_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        dedupe_key,
                        chat_id,
                        thread_id,
                        kind,
                        json.dumps(payload, ensure_ascii=False),
                        int(available_at or time.time()),
                        int(time.time()),
                    ),
                )
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def claim_outbox(self, owner: str, lease_seconds: int = 90) -> Optional[sqlite3.Row]:
        now = int(time.time())
        with self.lock:
            self.db.execute(
                "UPDATE feishu_outbox SET status = 'pending', lease_owner = NULL "
                "WHERE status = 'sending' AND lease_expires_at < ?",
                (now,),
            )
            row = self.db.execute(
                "SELECT * FROM feishu_outbox WHERE status = 'pending' AND available_at <= ? "
                "ORDER BY id LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                self.db.commit()
                return None
            self.db.execute(
                "UPDATE feishu_outbox SET status = 'sending', lease_owner = ?, "
                "lease_expires_at = ? WHERE id = ?",
                (owner, now + lease_seconds, row["id"]),
            )
            self.db.commit()
            return self.db.execute(
                "SELECT * FROM feishu_outbox WHERE id = ?", (row["id"],)
            ).fetchone()

    def complete_outbox(self, outbox_id: int) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE feishu_outbox SET status = 'sent', sent_at = ?, "
                "lease_owner = NULL WHERE id = ?",
                (int(time.time()), outbox_id),
            )
            self.db.commit()

    def retry_outbox(self, outbox_id: int, attempts: int, error: str) -> None:
        delay = min(300, 2 ** min(attempts, 8))
        with self.lock:
            self.db.execute(
                "UPDATE feishu_outbox SET status = 'pending', attempts = ?, "
                "available_at = ?, last_error = ?, lease_owner = NULL WHERE id = ?",
                (attempts, int(time.time()) + delay, error[:500], outbox_id),
            )
            self.db.commit()

    def fail_outbox(self, outbox_id: int, error: str) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE feishu_outbox SET status = 'failed', last_error = ?, "
                "lease_owner = NULL WHERE id = ?",
                (error[:500], outbox_id),
            )
            self.db.commit()

    def recover_outbox(self) -> int:
        with self.lock:
            cursor = self.db.execute(
                "UPDATE feishu_outbox SET status = 'pending', lease_owner = NULL "
                "WHERE status = 'sending'"
            )
            self.db.commit()
            return cursor.rowcount or 0

    # --- leases ----------------------------------------------------------
    def acquire_lease(self, resource_key: str, owner: str, ttl: int = 60) -> bool:
        now = int(time.time())
        with self.lock:
            row = self.db.execute(
                "SELECT owner, expires_at FROM worker_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            if row and row["owner"] != owner and row["expires_at"] > now:
                return False
            self.db.execute(
                "INSERT INTO worker_leases(resource_key, owner, expires_at, heartbeat_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(resource_key) DO UPDATE SET "
                "owner = excluded.owner, expires_at = excluded.expires_at, "
                "heartbeat_at = excluded.heartbeat_at",
                (resource_key, owner, now + ttl, now),
            )
            self.db.commit()
            return True

    def release_lease(self, resource_key: str, owner: Optional[str] = None) -> None:
        with self.lock:
            if owner:
                self.db.execute(
                    "DELETE FROM worker_leases WHERE resource_key = ? AND owner = ?",
                    (resource_key, owner),
                )
            else:
                self.db.execute(
                    "DELETE FROM worker_leases WHERE resource_key = ?", (resource_key,)
                )
            self.db.commit()

    # --- routing ---------------------------------------------------------
    def map_message(self, chat_id: str, message_id: str, thread_id: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO message_routes(chat_id, message_id, thread_id) VALUES(?, ?, ?) "
                "ON CONFLICT(chat_id, message_id) DO UPDATE SET thread_id = excluded.thread_id",
                (chat_id, message_id, thread_id),
            )
            self.db.commit()

    def thread_for_message(self, chat_id: str, message_id: str) -> Optional[str]:
        with self.lock:
            row = self.db.execute(
                "SELECT thread_id FROM message_routes WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            return str(row["thread_id"]) if row else None

    def route_thread(
        self,
        thread_id: str,
        chat_id: str,
        turn_id: Optional[str] = None,
        **fields: Optional[str],
    ) -> None:
        now_ms = int(time.time() * 1000)
        with self.lock:
            existing = self.db.execute(
                "SELECT * FROM thread_routes WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if existing is None:
                columns = ["thread_id", "chat_id", "last_turn_id", "updated_at"]
                values: List[Any] = [thread_id, chat_id, turn_id, now_ms]
                for key, value in fields.items():
                    columns.append(key)
                    values.append(value)
                placeholders = ", ".join("?" for _ in columns)
                self.db.execute(
                    f"INSERT INTO thread_routes({', '.join(columns)}) VALUES({placeholders})",
                    values,
                )
            else:
                assignments = ["chat_id = ?", "updated_at = ?"]
                values = [chat_id, now_ms]
                if turn_id is not None:
                    assignments.append("last_turn_id = ?")
                    values.append(turn_id)
                for key, value in fields.items():
                    assignments.append(f"{key} = ?")
                    values.append(value)
                values.append(thread_id)
                self.db.execute(
                    f"UPDATE thread_routes SET {', '.join(assignments)} WHERE thread_id = ?",
                    values,
                )
            self.db.commit()

    def route(self, thread_id: str) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM thread_routes WHERE thread_id = ?", (thread_id,)
            ).fetchone()

    def route_for_feishu_thread(self, feishu_thread_id: str) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM thread_routes WHERE feishu_thread_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (feishu_thread_id,),
            ).fetchone()

    def recent_routes(self, chat_id: str, limit: int = 4) -> List[sqlite3.Row]:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM thread_routes WHERE chat_id = ? "
                "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()

    def set_final(self, thread_id: str, text: str) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE thread_routes SET final_text = ?, updated_at = ? WHERE thread_id = ?",
                (text, int(time.time()), thread_id),
            )
            self.db.commit()

    def pop_final(self, thread_id: str) -> Optional[str]:
        with self.lock:
            row = self.db.execute(
                "SELECT final_text FROM thread_routes WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if not row:
                return None
            self.db.execute(
                "UPDATE thread_routes SET final_text = NULL WHERE thread_id = ?", (thread_id,)
            )
            self.db.commit()
            return row["final_text"]

    # --- approvals -------------------------------------------------------
    def add_pending(
        self, request_id: Any, method: str, params: Dict[str, Any], chat_id: str
    ) -> str:
        token = secrets.token_hex(8)
        with self.lock:
            self.db.execute(
                "INSERT INTO pending_requests(token, request_id, method, params_json, "
                "chat_id, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (
                    token,
                    # Keep the JSON-RPC id verbatim: the app-server matches
                    # responses by exact value, so an int must stay an int.
                    json.dumps(request_id),
                    method,
                    json.dumps(params, ensure_ascii=False),
                    chat_id,
                    int(time.time()),
                ),
            )
            self.db.commit()
        return token

    def pending(self, token: str) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM pending_requests WHERE token = ?", (token,)
            ).fetchone()

    def delete_pending(self, token: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM pending_requests WHERE token = ?", (token,))
            self.db.commit()

    def remember_approval_item(self, item_id: str, item: Dict[str, Any]) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO approval_items(item_id, payload_json, updated_at) "
                "VALUES(?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET "
                "payload_json = excluded.payload_json, updated_at = excluded.updated_at",
                (item_id, json.dumps(item, ensure_ascii=False), int(time.time())),
            )
            self.db.commit()

    def approval_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute(
                "SELECT payload_json FROM approval_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None

    def forget_approval_item(self, item_id: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM approval_items WHERE item_id = ?", (item_id,))
            self.db.commit()

    # --- next actions ----------------------------------------------------
    def remember_next_action(self, chat_id: str, thread_id: str, label: str, prompt: str) -> str:
        token = secrets.token_hex(6)
        with self.lock:
            self.db.execute(
                "INSERT INTO next_actions(token, chat_id, thread_id, label, prompt, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (token, chat_id, thread_id, label, prompt, int(time.time())),
            )
            self.db.commit()
        return token

    def consume_next_action(self, token: str, chat_id: str) -> Optional[sqlite3.Row]:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM next_actions WHERE token = ? AND chat_id = ? AND used_at IS NULL",
                (token, chat_id),
            ).fetchone()
            if not row:
                return None
            self.db.execute(
                "UPDATE next_actions SET used_at = ? WHERE token = ?",
                (int(time.time()), token),
            )
            self.db.commit()
            return row

    # --- turn queue ------------------------------------------------------
    def enqueue_turn(
        self,
        chat_id: str,
        thread_id: str,
        inputs: List[Dict[str, Any]],
        source_message_id: str,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> Tuple[sqlite3.Row, bool]:
        with self.lock:
            try:
                cursor = self.db.execute(
                    "INSERT INTO queued_turns(chat_id, thread_id, inputs_json, cwd, model, "
                    "effort, source_message_id, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chat_id,
                        thread_id,
                        json.dumps(inputs, ensure_ascii=False),
                        cwd,
                        model,
                        effort,
                        source_message_id,
                        int(time.time()),
                    ),
                )
                self.db.commit()
                created = True
                row_id = cursor.lastrowid
            except sqlite3.IntegrityError:
                created = False
                existing = self.db.execute(
                    "SELECT id FROM queued_turns WHERE chat_id = ? AND source_message_id = ?",
                    (chat_id, source_message_id),
                ).fetchone()
                row_id = existing["id"] if existing else None
            row = self.db.execute(
                "SELECT * FROM queued_turns WHERE id = ?", (row_id,)
            ).fetchone()
            self.db.commit()
            return row, created

    def next_queued(self, thread_id: str) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM queued_turns WHERE thread_id = ? AND status = 'queued' "
                "ORDER BY id LIMIT 1",
                (thread_id,),
            ).fetchone()

    def mark_steered(self, queue_id: int, turn_id: str) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE queued_turns SET status = 'steered', turn_id = ? WHERE id = ?",
                (turn_id, queue_id),
            )
            self.db.commit()

    def coalesce_queued_turns(self, thread_id: str) -> int:
        """Merge every queued turn of a thread into the newest one."""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM queued_turns WHERE thread_id = ? AND status = 'queued' "
                "ORDER BY id",
                (thread_id,),
            ).fetchall()
            if len(rows) < 2:
                return 0
            merged: List[Dict[str, Any]] = []
            for row in rows:
                merged.extend(json.loads(row["inputs_json"]))
            keeper = rows[-1]
            self.db.execute(
                "UPDATE queued_turns SET inputs_json = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), keeper["id"]),
            )
            ids = [row["id"] for row in rows[:-1]]
            placeholders = ",".join("?" for _ in ids)
            self.db.execute(
                f"UPDATE queued_turns SET status = 'coalesced', finished_at = ? "
                f"WHERE id IN ({placeholders})",
                (int(time.time()), *ids),
            )
            self.db.commit()
            return len(ids)

    def source_messages_for_queue(self, queue_id: int, thread_id: str) -> List[str]:
        with self.lock:
            rows = self.db.execute(
                "SELECT source_message_id FROM queued_turns WHERE thread_id = ? AND id <= ? "
                "ORDER BY id",
                (thread_id, queue_id),
            ).fetchall()
            return [str(row["source_message_id"]) for row in rows]

    def running_turn(self, thread_id: str) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM queued_turns WHERE thread_id = ? AND status = 'running' "
                "ORDER BY id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()

    def queued_threads(self) -> List[str]:
        with self.lock:
            rows = self.db.execute(
                "SELECT DISTINCT thread_id FROM queued_turns WHERE status = 'queued'"
            ).fetchall()
            return [str(row["thread_id"]) for row in rows]

    def running_threads(self) -> List[str]:
        with self.lock:
            rows = self.db.execute(
                "SELECT DISTINCT thread_id FROM queued_turns WHERE status = 'running'"
            ).fetchall()
            return [str(row["thread_id"]) for row in rows]

    def queue_position(self, row_id: int, thread_id: str) -> int:
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS ahead FROM queued_turns "
                "WHERE thread_id = ? AND status = 'queued' AND id < ?",
                (thread_id, row_id),
            ).fetchone()
            return int(row["ahead"]) if row else 0

    def mark_running(self, row_id: int, turn_id: str) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE queued_turns SET status = 'running', turn_id = ?, started_at = ? "
                "WHERE id = ?",
                (turn_id, int(time.time()), row_id),
            )
            self.db.commit()

    def finish_running(self, thread_id: str, turn_id: Optional[str] = None) -> None:
        with self.lock:
            if turn_id:
                self.db.execute(
                    "UPDATE queued_turns SET status = 'done', finished_at = ? "
                    "WHERE thread_id = ? AND turn_id = ?",
                    (int(time.time()), thread_id, turn_id),
                )
            self.db.execute(
                "UPDATE queued_turns SET status = 'done', finished_at = ? "
                "WHERE thread_id = ? AND status = 'running'",
                (int(time.time()), thread_id),
            )
            self.db.commit()

    def queue_counts(self, thread_id: str) -> Tuple[int, int]:
        with self.lock:
            running = self.db.execute(
                "SELECT COUNT(*) AS n FROM queued_turns WHERE thread_id = ? AND status = 'running'",
                (thread_id,),
            ).fetchone()["n"]
            queued = self.db.execute(
                "SELECT COUNT(*) AS n FROM queued_turns WHERE thread_id = ? AND status = 'queued'",
                (thread_id,),
            ).fetchone()["n"]
            return int(running), int(queued)

    def source_messages_for_turn(self, thread_id: str, turn_id: Optional[str]) -> List[str]:
        with self.lock:
            if turn_id:
                rows = self.db.execute(
                    "SELECT source_message_id FROM queued_turns "
                    "WHERE thread_id = ? AND turn_id = ?",
                    (thread_id, turn_id),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT source_message_id FROM queued_turns "
                    "WHERE thread_id = ? AND status = 'running'",
                    (thread_id,),
                ).fetchall()
            return [str(row["source_message_id"]) for row in rows]

    def recover_queue(self) -> int:
        """Return interrupted turns to the queue after a restart."""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM queued_turns WHERE status = 'running'"
            ).fetchall()
            for row in rows:
                self.db.execute(
                    "UPDATE queued_turns SET status = 'queued', turn_id = NULL, "
                    "started_at = NULL WHERE id = ?",
                    (row["id"],),
                )
            self.db.execute("DELETE FROM worker_leases")
            self.db.commit()
            return len(rows)

    # --- task creations --------------------------------------------------
    def created_task(self, chat_id: str, source_message_id: str) -> Optional[str]:
        with self.lock:
            row = self.db.execute(
                "SELECT thread_id FROM task_creations WHERE chat_id = ? AND source_message_id = ?",
                (chat_id, source_message_id),
            ).fetchone()
            return str(row["thread_id"]) if row else None

    def remember_task(self, chat_id: str, source_message_id: str, thread_id: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO task_creations(chat_id, source_message_id, thread_id) "
                "VALUES(?, ?, ?) ON CONFLICT(chat_id, source_message_id) DO UPDATE SET "
                "thread_id = excluded.thread_id",
                (chat_id, source_message_id, thread_id),
            )
            self.db.commit()

    # --- artifacts -------------------------------------------------------
    def add_artifact(
        self, thread_id: str, turn_id: Optional[str], path: str, fingerprint: str
    ) -> bool:
        with self.lock:
            try:
                self.db.execute(
                    "INSERT INTO turn_artifacts(thread_id, turn_id, path, fingerprint, "
                    "created_at) VALUES(?, ?, ?, ?, ?)",
                    (thread_id, turn_id, path, fingerprint, int(time.time())),
                )
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def pending_artifacts(self, thread_id: str) -> List[sqlite3.Row]:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM turn_artifacts WHERE thread_id = ? AND status = 'pending' "
                "ORDER BY id",
                (thread_id,),
            ).fetchall()

    def mark_artifact_sent(self, artifact_id: int) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE turn_artifacts SET status = 'sent', sent_at = ? WHERE id = ?",
                (int(time.time()), artifact_id),
            )
            self.db.commit()

    def mark_artifact_suppressed(self, artifact_id: int) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE turn_artifacts SET status = 'suppressed' WHERE id = ?",
                (artifact_id,),
            )
            self.db.commit()

    # --- handoffs --------------------------------------------------------
    def handoff_for(self, source_thread_id: str) -> Optional[str]:
        with self.lock:
            row = self.db.execute(
                "SELECT continuation_thread_id FROM thread_handoffs WHERE source_thread_id = ?",
                (source_thread_id,),
            ).fetchone()
            return str(row["continuation_thread_id"]) if row else None

    def remember_handoff(self, source_thread_id: str, continuation_thread_id: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO thread_handoffs(source_thread_id, continuation_thread_id, "
                "created_at) VALUES(?, ?, ?) ON CONFLICT(source_thread_id) DO UPDATE SET "
                "continuation_thread_id = excluded.continuation_thread_id",
                (source_thread_id, continuation_thread_id, int(time.time())),
            )
            self.db.commit()

    def active_turn_count(self) -> int:
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS n FROM queued_turns WHERE status = 'running'"
            ).fetchone()
            return int(row["n"]) if row else 0
