"""Codex Feishu Bridge.

A single-host control plane that lets a Feishu topic group drive a local Codex
app-server. SQLite in WAL mode is the source of truth: inbound events, queued
turns, worker leases, the outbox, approvals and artifacts all survive restarts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import queue
import re
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core import commands
from .core.appserver import AppServer, AppServerError
from .core.config import app_dir, codex_home, load_config
from .core.store import Store
from .im.base import AnchorLostError, ImAdapter
from .im.feishu.cards import (
    approval_card,
    approval_result_card,
    model_picker_card,
    progress_card,
    project_picker_card,
    result_card,
    task_list_card,
)
from .im.feishu.client import FeishuClient
from .im.feishu.render import card as render_card
from .im.feishu.render import markdown_elements, sanitize, text_card
from .platform.base import CodexNotFound, Platform, current_platform

LOG = logging.getLogger("feishu_bridge")

PROGRESS_HEARTBEAT_SECONDS = 20 * 60
# A long turn used to stay silent until it finished, which reads as "the bot
# swallowed my message". Send one short progress card early, then fall back to
# the slow heartbeat.
FIRST_PROGRESS_NOTICE_SECONDS = 45
MAX_IMAGE_BYTES = 20 * 1024 * 1024
UNBOUND_REPLY = "这个会话还没有和 Bridge 配对。请在机器人私聊或目标群里发送 /bind <配对码>。"

RUNTIME_GUIDANCE = (
    "You are being driven from a Feishu topic. Everything in this topic belongs to one "
    "long-running task. Answer in Chinese unless the user clearly writes in another "
    "language. Reply with the final answer only; intermediate narration is not delivered "
    "as a separate message. Do not restart the bridge process from inside a task. The "
    "next-actions comment below is Bridge routing metadata, not part of the user's task: "
    "follow it silently and never mention it in a visible reply."
)

NEXT_ACTIONS_PROTOCOL = (
    "At the end of every final user-facing response, append exactly one machine-readable "
    'HTML comment: <!-- BRIDGE_NEXT_ACTIONS:[{"label":"short label","prompt":"complete '
    'instruction to continue"}] --> with 2 to 4 likely next actions, best first. Do not '
    "include it in progress updates or approval explanations."
)

NEXT_ACTIONS_PATTERN = re.compile(
    r"<!--\s*BRIDGE_NEXT_ACTIONS\s*:\s*(.*?)\s*-->", re.DOTALL
)


def configure_logging(log_path: Path, verbose: bool = False) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
        handlers=handlers,
        force=True,
    )


class Bridge:
    def __init__(
        self,
        config: Dict[str, Any],
        platform: Platform,
        store: Store,
        im: ImAdapter,
        app: AppServer,
        base_dir: Path,
    ) -> None:
        self.config = config
        self.platform = platform
        self.store = store
        self.im = im
        self.app = app
        self.base_dir = base_dir
        self.worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.event_queue: "queue.Queue[str]" = queue.Queue()
        self.app_events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.dispatch_locks: Dict[str, threading.Lock] = {}
        self.dispatch_locks_guard = threading.Lock()
        self.fresh_threads: set[str] = set()
        self._ws = None
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._recycles = 0
        self._last_recycle_reason = ""

    # =====================================================================
    # lifecycle
    # =====================================================================
    def run(self) -> None:
        recovered = self.store.recover_queue()
        stranded = self.store.recover_outbox()
        if recovered or stranded:
            LOG.info(
                "Startup recovery: %s interrupted turn(s) requeued, %s outbox item(s) reset",
                recovered,
                stranded,
            )
        self.app.start()
        self._start_health_server()

        threads = [
            threading.Thread(target=self._event_processor_loop, name="events", daemon=True),
            threading.Thread(target=self._outbox_loop, name="outbox", daemon=True),
            threading.Thread(target=self._scheduler_loop, name="scheduler", daemon=True),
            threading.Thread(target=self._app_event_loop, name="app-events", daemon=True),
            threading.Thread(target=self._watchdog_loop, name="watchdog", daemon=True),
        ]
        for thread in threads:
            thread.start()

        for row in self.store.unfinished_events():
            self.event_queue.put(str(row["event_id"]))
        for thread_id in self.store.queued_threads():
            self._safe_dispatch(thread_id)

        self._start_listener()

        LOG.info("Bridge %s is running; pair code %s", self.worker_id, self.store.pair_code())
        try:
            while not self.stop_event.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self.stop_event.set()
        if self._ws is not None:
            try:
                self._ws.stop()
            except Exception:
                LOG.debug("Listener shutdown failed", exc_info=True)
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                LOG.debug("Health server shutdown failed", exc_info=True)
        try:
            self.app.stop()
        except Exception:
            LOG.debug("App-server shutdown failed", exc_info=True)
        self.store.close()

    def _start_listener(self) -> None:
        from .im.feishu.ws_listener import WsListener

        feishu = self.config["feishu"]
        app_id = self._secret("app_id")
        app_secret = self._secret("app_secret")
        if not app_id or not app_secret:
            LOG.warning(
                "Feishu credentials are missing; running headless. "
                "Store them with the install script, then restart."
            )
            return
        self._ws = WsListener(app_id, app_secret, feishu["domain"], self._intake)
        self._ws.start()
        LOG.info("Feishu long connection is starting")

    def _secret(self, name: str) -> Optional[str]:
        accounts = self.config.get("credential_accounts") or {}
        account = accounts.get(name)
        if not account:
            return None
        service = self.config["credential_service"]
        value = self.platform.secret_get(service, account)
        if value:
            return value
        env_name = f"FEISHU_{name.upper()}"
        return os.environ.get(env_name) or None

    # =====================================================================
    # intake: never block the SDK; persist then queue
    # =====================================================================
    def _intake(self, payload: Dict[str, Any]) -> None:
        event_id = str(payload.get("event_id") or "")
        if not event_id:
            LOG.warning("Dropping an event without an event id: %s", payload.get("kind"))
            return
        if not self.store.record_event(event_id, payload):
            LOG.debug("Ignoring replayed event %s", event_id)
            return
        self.event_queue.put(event_id)

    def _event_processor_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                event_id = self.event_queue.get(timeout=1)
            except queue.Empty:
                continue
            with self.store.lock:
                row = self.store.db.execute(
                    "SELECT payload_json FROM feishu_events WHERE event_id = ?", (event_id,)
                ).fetchone()
            if not row:
                continue
            try:
                payload = json.loads(row["payload_json"])
                self._process_event(payload)
                self.store.finish_event(event_id)
            except Exception as exc:
                LOG.exception("Failed to process event %s", event_id)
                self.store.finish_event(event_id, str(exc))

    def _process_event(self, payload: Dict[str, Any]) -> None:
        kind = payload.get("kind")
        if kind == "message":
            self._handle_message(payload)
        elif kind == "card_action":
            self._handle_card_action(payload)
        else:
            LOG.debug("Ignoring event kind %s", kind)

    # =====================================================================
    # authorization
    # =====================================================================
    def _authorized(self, chat_id: str, open_id: str) -> bool:
        allowlist = (self.config.get("feishu") or {}).get("allowlist") or {}
        if open_id and open_id in (allowlist.get("open_ids") or []):
            return True
        if chat_id and chat_id in (allowlist.get("chat_ids") or []):
            return True
        return bool(open_id) and self.store.is_bound(chat_id, open_id)

    def _mention_ok(self, ev: Dict[str, Any]) -> bool:
        """Enforce the optional "every group message must @ the bot" mode.

        When Feishu grants the sensitive group-message scope the bridge receives
        every topic message; this switch lets the operator require an explicit
        mention instead. Without that scope Feishu already only delivers
        mentions, so the default of False changes nothing.
        """
        if not ((self.config.get("feishu") or {}).get("require_mention_in_group")):
            return True
        if str(ev.get("chat_type") or "").lower() == "p2p":
            return True
        return bool(ev.get("mentions"))

    # =====================================================================
    # message handling
    # =====================================================================
    def _handle_message(self, ev: Dict[str, Any]) -> None:
        chat_id = ev["chat_id"]
        open_id = ev["sender_open_id"]
        if not chat_id or not open_id:
            return
        if ev.get("sender_type") not in ("user", ""):
            return
        text = self._extract_text(ev)
        command = commands.parse(text) if text else None
        # Healthy traffic used to be completely silent, which made a real
        # incident impossible to read back from the log.
        LOG.info(
            "Message %s type=%s chat=%s thread=%s",
            ev.get("message_id"),
            ev.get("msg_type") or "-",
            chat_id,
            ev.get("thread_id") or "-",
        )

        if command and command.name == "bind":
            self._handle_bind(chat_id, open_id, command.rest)
            return

        if not self._mention_ok(ev):
            LOG.debug("Ignoring a group message without a mention")
            return

        if not self._authorized(chat_id, open_id):
            LOG.info("Ignoring message from an unauthorized sender in %s", chat_id)
            if command or text:
                self._reply_plain(chat_id, UNBOUND_REPLY, ev)
            return

        if command and commands.is_known(command.name):
            self._handle_command(ev, command)
            return

        inputs = self._message_inputs(ev)
        if not inputs:
            self._reply_plain(chat_id, "这条消息里没有可处理的内容。", ev)
            return

        thread_id = self._resolve_thread(ev)
        if thread_id:
            self._submit_turn(chat_id, thread_id, inputs, ev)
            return
        self._start_quick_task(ev, inputs, text)

    def _handle_bind(self, chat_id: str, open_id: str, code: str) -> None:
        expected = self.store.pair_code()
        if not code or code.strip() != expected:
            self._reply_plain(chat_id, "配对码不正确或已轮换。请在 Bridge 日志里查看当前的配对码。", None)
            return
        self.store.bind(chat_id, open_id, "")
        self.store.rotate_pair_code()
        self._reply_plain(chat_id, "配对成功。现在可以在这个会话里下达任务了。", None)
        LOG.info("Chat %s paired with %s", chat_id, open_id)

    def _extract_text(self, ev: Dict[str, Any]) -> str:
        msg_type = ev.get("msg_type") or ""
        raw = ev.get("content") or ""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if msg_type == "text":
            text = str(payload.get("text") or "")
        elif msg_type == "post":
            text = self._post_to_text(payload)
        else:
            text = ""
        for mention in ev.get("mentions") or []:
            key = mention.get("key") or ""
            if key:
                text = text.replace(key, "")
        return text.strip()

    @staticmethod
    def _post_to_text(payload: Dict[str, Any]) -> str:
        lines: List[str] = []
        for block in payload.get("content") or []:
            for element in block or []:
                if element.get("tag") == "text":
                    lines.append(str(element.get("text") or ""))
                elif element.get("tag") == "a":
                    lines.append(str(element.get("href") or ""))
        return " ".join(part for part in lines if part).strip()

    @staticmethod
    def _post_elements(ev: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Flatten a rich-text message into its elements.

        Feishu delivers an image that was composed together with text as a
        ``post`` message; the picture sits inline as an ``img`` element rather
        than arriving as its own ``image`` message.
        """
        try:
            payload = json.loads(ev.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            return []
        blocks = payload.get("content") or payload.get("content_v2") or []
        elements: List[Dict[str, Any]] = []
        for block in blocks:
            for element in block or []:
                if isinstance(element, dict):
                    elements.append(element)
        return elements

    def _message_inputs(self, ev: Dict[str, Any]) -> List[Dict[str, Any]]:
        inputs: List[Dict[str, Any]] = []
        text = self._extract_text(ev)
        msg_type = ev.get("msg_type") or ""
        if msg_type == "image":
            try:
                payload = json.loads(ev.get("content") or "{}")
                image_key = str(payload.get("image_key") or "")
                path = self.im.download_resource(ev["message_id"], image_key, "image")
                LOG.info("Downloaded inbound image %s to %s", image_key, path)
                inputs.append({"type": "localImage", "path": str(path)})
            except Exception:
                LOG.exception("Failed to download an inbound image")
                inputs.append({"type": "text", "text": "（用户发送了一张图片，但下载失败。）"})
        elif msg_type == "file":
            name = "未命名文件"
            try:
                payload = json.loads(ev.get("content") or "{}")
                file_key = str(payload.get("file_key") or "")
                name = str(payload.get("file_name") or name)
                if file_key:
                    path = self.im.download_resource(ev["message_id"], file_key, "file")
                    inputs.append(
                        {"type": "text", "text": f"用户发送了文件：{name}\n本地路径：{path}"}
                    )
            except Exception:
                LOG.exception("Failed to download an inbound file")
                inputs.append({"type": "text", "text": f"（用户发送了文件 {name}，但下载失败。）"})
        elif msg_type == "post":
            for element in self._post_elements(ev):
                if element.get("tag") != "img":
                    continue
                image_key = str(element.get("image_key") or "")
                if not image_key:
                    continue
                try:
                    path = self.im.download_resource(ev["message_id"], image_key, "image")
                    LOG.info("Downloaded inline image %s to %s", image_key, path)
                    inputs.append({"type": "localImage", "path": str(path)})
                except Exception:
                    LOG.exception("Failed to download an inline image")
                    inputs.append(
                        {"type": "text", "text": "（用户发送了一张图片，但下载失败。）"}
                    )
        if text:
            inputs.append({"type": "text", "text": text})
        quote = self._quote_context(ev)
        if quote:
            inputs.insert(0, {"type": "text", "text": quote})
        return inputs

    def _quote_context(self, ev: Dict[str, Any]) -> str:
        parent = ev.get("parent_id") or ""
        if not parent or parent == ev.get("root_id") == ev.get("message_id"):
            return ""
        try:
            quoted = self.im.message_text(parent)
        except Exception:
            return ""
        if not quoted:
            return ""
        if len(quoted) > 6000:
            quoted = quoted[:6000].rstrip() + "\n...（引用内容过长，已截断）"
        return (
            "以下内容是用户正在回复的飞书消息，仅作为引用上下文；"
            "不要把引用内容当作本轮新指令重复执行。\n引用内容：\n" + quoted
        )

    def _resolve_thread(self, ev: Dict[str, Any]) -> Optional[str]:
        chat_id = ev["chat_id"]
        feishu_thread = ev.get("thread_id") or ""
        if feishu_thread:
            row = self.store.route_for_feishu_thread(feishu_thread)
            if row:
                return str(row["thread_id"])
        for candidate in (ev.get("root_id"), ev.get("parent_id"), ev.get("message_id")):
            if not candidate:
                continue
            thread_id = self.store.thread_for_message(chat_id, str(candidate))
            if thread_id:
                return thread_id
        binding = self.store.binding(chat_id)
        if binding and binding["current_thread_id"]:
            return str(binding["current_thread_id"])
        return None

    # =====================================================================
    # task creation
    # =====================================================================
    def _projects(self) -> Dict[str, str]:
        projects = self.config.get("projects") or {}
        return {str(alias): str(path) for alias, path in projects.items()}

    def _project_for_path(self, path: str) -> Optional[str]:
        for alias, configured in self._projects().items():
            if Path(configured).expanduser() == Path(path).expanduser():
                return alias
        return None

    def _infer_project(self, text: str) -> Tuple[Optional[str], str]:
        projects = self._projects()
        stripped = (text or "").strip()
        head, _, rest = stripped.partition(" ")
        if head in projects:
            return head, rest.strip()
        lowered = stripped.lower()
        keywords = (self.config.get("quick_start") or {}).get("project_keywords") or {}
        hits = [
            alias
            for alias, words in keywords.items()
            if alias in projects and any(str(word).lower() in lowered for word in words or [])
        ]
        if len(hits) == 1:
            return hits[0], stripped
        if len(projects) == 1:
            return next(iter(projects)), stripped
        return None, stripped

    def _model_choice(self, chat_id: str) -> Tuple[Optional[str], Optional[str]]:
        binding = self.store.binding(chat_id)
        # A chat can be authorized by the allowlist without ever running
        # /bind, and update_binding only touches an existing row — so the
        # per-binding columns are silently unusable there. Settings are the
        # durable home for the choice; the binding is read for older rows.
        model = (
            (binding["current_model"] if binding else None)
            or self.store.get_setting(f"chat_model:{chat_id}", "")
            or self.config.get("default_model")
        )
        effort = (
            (binding["current_effort"] if binding else None)
            or self.store.get_setting(f"chat_effort:{chat_id}", "")
            or self.config.get("default_effort")
        )
        return model, effort

    def _remember_model(
        self, chat_id: str, model: str, effort: Optional[str] = None
    ) -> None:
        self.store.set_setting(f"chat_model:{chat_id}", model or "")
        if effort is not None:
            self.store.set_setting(f"chat_effort:{chat_id}", effort or "")
        if self.store.binding(chat_id):
            self.store.update_binding(chat_id, current_model=model, current_effort=effort)

    def _start_quick_task(
        self, ev: Dict[str, Any], inputs: List[Dict[str, Any]], text: str
    ) -> None:
        chat_id = ev["chat_id"]
        project, _remainder = self._infer_project(text)
        model, _ = self._model_choice(chat_id)

        token = self._new_draft(
            chat_id=chat_id,
            message_id=ev["message_id"],
            prompt=text,
            project=project,
            feishu_thread_id=ev.get("thread_id") or "",
            root_message_id=ev.get("root_id") or ev.get("message_id") or "",
            model=model,
        )
        if model is None:
            self._send_model_picker(chat_id, token, ev)
            return
        if project is None:
            self._send_project_picker(chat_id, token, text)
            return
        self._materialize_task(token, inputs_override=inputs)

    # --- drafts ----------------------------------------------------------
    def _draft_key(self, token: str) -> str:
        return f"draft:{token}"

    def _new_draft(self, **fields: Any) -> str:
        token = uuid.uuid4().hex[:10]
        fields["created_at"] = int(time.time())
        self.store.set_setting(self._draft_key(token), json.dumps(fields, ensure_ascii=False))
        return token

    def _load_draft(self, token: str) -> Optional[Dict[str, Any]]:
        raw = self.store.get_setting(self._draft_key(token), "")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _save_draft(self, token: str, draft: Dict[str, Any]) -> None:
        self.store.set_setting(self._draft_key(token), json.dumps(draft, ensure_ascii=False))

    def _drop_draft(self, token: str) -> None:
        self.store.set_setting(self._draft_key(token), "")

    def _send_model_picker(self, chat_id: str, token: str, ev: Dict[str, Any]) -> None:
        models = self._models()
        if not models:
            LOG.warning("No models available; falling back to the app-server default")
            draft = self._load_draft(token) or {}
            draft["model"] = "default"
            self._save_draft(token, draft)
            self._materialize_task(token)
            return
        card = model_picker_card(models, token)
        self._send_card(chat_id, card, ev)

    def _send_project_picker(self, chat_id: str, token: str, prompt: str, page: int = 0) -> None:
        card = project_picker_card(list(self._projects().keys()), prompt, token, page)
        self._send_card(chat_id, card, None)

    def _materialize_task(
        self, token: str, inputs_override: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        draft = self._load_draft(token)
        if not draft:
            LOG.info("Draft %s expired before it could be created", token)
            return
        chat_id = draft["chat_id"]
        project = draft.get("project") or next(iter(self._projects()), "codex")
        cwd = self._projects().get(project)
        if not cwd:
            self._send_text(chat_id, f"项目 {project} 没有配置目录，请在 config.json 的 projects 里补上。")
            self._drop_draft(token)
            return

        model = None if draft.get("model") in (None, "default") else draft.get("model")
        params: Dict[str, Any] = {
            "approvalPolicy": self.config["approval_policy"],
            "serviceName": "feishu-bridge",
            "cwd": str(Path(cwd).expanduser()),
        }
        if model:
            params["model"] = model
        try:
            result = self.app.request("thread/start", self._thread_params(params))
        except Exception:
            LOG.exception("thread/start failed while creating a task")
            self._send_text(chat_id, "创建 Codex 任务失败，请查看 Bridge 日志。")
            self._drop_draft(token)
            return
        thread = result.get("thread") or {}
        thread_id = str(thread.get("id") or thread.get("threadId") or "")
        if not thread_id:
            self._send_text(chat_id, "Codex 没有返回任务 ID，创建失败。")
            self._drop_draft(token)
            return
        self.fresh_threads.add(thread_id)

        title = self._draft_title(draft)
        feishu_thread_id = draft.get("feishu_thread_id") or ""
        root_message_id = draft.get("root_message_id") or ""
        if not feishu_thread_id:
            try:
                reference = self.im.create_topic(chat_id, f"📋 {title}")
                feishu_thread_id = reference.thread_id
                root_message_id = reference.root_message_id
            except Exception:
                LOG.exception("Failed to create the Feishu topic for a new task")
        self.store.route_thread(
            thread_id,
            chat_id,
            feishu_thread_id=feishu_thread_id,
            root_message_id=root_message_id,
            title=title,
            project=project,
        )
        if root_message_id:
            self.store.map_message(chat_id, root_message_id, thread_id)
        self.store.remember_task(chat_id, str(draft["message_id"]), thread_id)
        self.store.update_binding(chat_id, current_thread_id=thread_id, current_project=project)
        self._drop_draft(token)

        if inputs_override is not None:
            self._submit_turn(
                chat_id,
                thread_id,
                inputs_override,
                {"message_id": draft["message_id"]},
                cwd=str(Path(cwd).expanduser()),
            )
        else:
            inputs = [{"type": "text", "text": str(draft.get("prompt") or title)}]
            self._submit_turn(
                chat_id,
                thread_id,
                inputs,
                {"message_id": draft["message_id"]},
                cwd=str(Path(cwd).expanduser()),
            )

    @staticmethod
    def _draft_title(draft: Dict[str, Any]) -> str:
        prompt = str(draft.get("prompt") or "").strip().replace("\n", " ")
        title = prompt[:40] or "新任务"
        project = draft.get("project")
        return f"{project}: {title}" if project else title

    def _thread_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            **params,
            "developerInstructions": f"{RUNTIME_GUIDANCE}\n\n{NEXT_ACTIONS_PROTOCOL}",
        }

    # =====================================================================
    # turn submission and scheduling
    # =====================================================================
    def _submit_turn(
        self,
        chat_id: str,
        thread_id: str,
        inputs: List[Dict[str, Any]],
        ev: Dict[str, Any],
        cwd: Optional[str] = None,
    ) -> None:
        source_message_id = str(ev.get("message_id") or f"-{time.time_ns()}")
        model, effort = self._model_choice(chat_id)
        row, created = self.store.enqueue_turn(
            chat_id, thread_id, inputs, source_message_id, cwd, model, effort
        )
        if not created:
            self._send_text(chat_id, "这条消息已经收到，不会重复提交。", ev)
            return
        LOG.info(
            "Queued turn %s on thread %s from message %s",
            row["id"],
            thread_id,
            source_message_id,
        )
        self.store.route_thread(thread_id, chat_id)
        self.store.map_message(chat_id, source_message_id, thread_id)
        self.store.enqueue_outbox(
            chat_id,
            thread_id,
            "react",
            {"message_id": source_message_id, "emoji": "👀"},
            dedupe_key=f"react:seen:{source_message_id}",
        )
        dispatched = self._safe_dispatch(thread_id)
        if dispatched is not None and int(dispatched["id"]) == int(row["id"]):
            return
        ahead = self.store.queue_position(int(row["id"]), thread_id)
        self._send_card(
            chat_id,
            progress_card(
                "已进入队列",
                [
                    f"消息已排队，当前第 {ahead + 1} 位。",
                    "当前处理结束后会自动发送；你可以继续切换任务。",
                ],
                thread_id,
                running=True,
            ),
            ev,
            thread_id=thread_id,
        )

    def _safe_dispatch(self, thread_id: str):
        try:
            return self._dispatch_thread(thread_id)
        except Exception:
            LOG.exception("Dispatch failed for %s", thread_id)
            return None

    def _dispatch_lock(self, thread_id: str) -> threading.Lock:
        with self.dispatch_locks_guard:
            lock = self.dispatch_locks.get(thread_id)
            if lock is None:
                lock = threading.Lock()
                self.dispatch_locks[thread_id] = lock
            return lock

    def _dispatch_thread(self, thread_id: str):
        lock = self._dispatch_lock(thread_id)
        with lock:
            if not self.store.acquire_lease(f"thread:{thread_id}", self.worker_id):
                return None
            try:
                if self.store.running_turn(thread_id):
                    return None
                queued = self.store.next_queued(thread_id)
                if not queued:
                    return None
                if thread_id not in self.fresh_threads:
                    try:
                        resumed = self.app.request(
                            "thread/resume", self._thread_params({"threadId": thread_id})
                        )
                    except AppServerError as exc:
                        if "active writer" in str(exc).lower():
                            # Another Codex client (usually the desktop app with
                            # this conversation open) holds the thread's writer
                            # lock. Retrying turn/start only yields "thread not
                            # found", so report it once and wait for the lock.
                            self._notify_writer_conflict(thread_id)
                            return None
                        if self._thread_lost(exc):
                            return self._recover_lost_thread(thread_id, queued)
                        LOG.info("thread/resume was rejected (%s); trying turn/start anyway", exc)
                    else:
                        self._notify_foreign_turns(thread_id, resumed)
                params: Dict[str, Any] = {
                    "threadId": thread_id,
                    "input": json.loads(queued["inputs_json"]),
                }
                if queued["cwd"]:
                    params["cwd"] = str(Path(queued["cwd"]).expanduser())
                    params["sandboxPolicy"] = {"type": self.config["sandbox_policy"]}
                if queued["model"]:
                    params["model"] = queued["model"]
                if queued["effort"]:
                    params["effort"] = queued["effort"]
                try:
                    result = self.app.request("turn/start", params, timeout=120)
                except AppServerError as exc:
                    if self._thread_lost(exc):
                        return self._recover_lost_thread(thread_id, queued)
                    raise
                turn = result.get("turn") or {}
                turn_id = str(turn.get("id") or turn.get("turnId") or "")
                self.store.mark_running(int(queued["id"]), turn_id)
                self._record_turn_started(thread_id, turn_id)
                # Start the progress notice here as well, so a dropped
                # turn/started notification cannot leave the user staring at
                # silence. The heartbeat key makes the second call a no-op.
                self._start_heartbeat(thread_id, turn_id)
                return queued
            finally:
                self.store.release_lease(f"thread:{thread_id}", self.worker_id)

    @staticmethod
    def _thread_lost(exc: Exception) -> bool:
        """True when the app-server no longer knows this Codex thread.

        Codex keeps a thread's rollout as a file, and a hard restart of the
        app-server can leave a thread without one; the id is then dead for
        good. At-least-once delivery must not turn that into a silent retry
        loop that never reaches the user.
        """
        message = str(exc).lower()
        return "no rollout found" in message or "thread not found" in message

    def _recover_lost_thread(self, thread_id: str, queued: Any) -> Any:
        """Replace a lost Codex thread and continue on a fresh one."""
        queue_id = int(queued["id"])
        guard = f"recovery:{queue_id}"
        route = self.store.route(thread_id)
        if self.store.get_setting(guard, ""):
            LOG.error("Recovery for queue %s already failed once; giving up", queue_id)
            self.store.fail_turn(queue_id)
            # The first attempt may already have migrated the route, so fall
            # back to the queue row's own chat and whatever route is live now.
            target = thread_id if route else str(queued["thread_id"] or "") or None
            if target and not self.store.route(target):
                target = None
            self._send_text(
                str(queued["chat_id"]),
                "这条消息对应的 Codex 任务无法恢复，请重新发送一次。",
                thread_id=target,
            )
            return None
        self.store.set_setting(guard, "1")

        params: Dict[str, Any] = {
            "approvalPolicy": self.config["approval_policy"],
            "serviceName": "feishu-recovered-task",
        }
        if queued["cwd"]:
            params["cwd"] = str(Path(queued["cwd"]).expanduser())
        if queued["model"]:
            params["model"] = queued["model"]
        result = self.app.request("thread/start", self._thread_params(params))
        replacement = str(((result.get("thread") or {}).get("id")) or "")
        if not replacement:
            raise AppServerError("recovery thread/start returned no thread id")

        self.fresh_threads.add(replacement)
        self.store.migrate_thread(thread_id, replacement)
        LOG.warning(
            "Codex thread %s was lost; continued on %s for queue %s",
            thread_id,
            replacement,
            queue_id,
        )
        if route:
            self._send_text(
                str(route["chat_id"]),
                "Bridge 重启后上一个任务的运行上下文在 Codex 侧丢了，"
                "已经在这个话题里新建任务接着处理你刚发的消息。",
                thread_id=replacement,
            )
        return self._dispatch_thread(replacement)

    NOTICE_COOLDOWN_SECONDS = 600

    def _notify_once(self, thread_id: str, kind: str, text: str) -> None:
        """Send one notice per thread and kind, at most once per cooldown."""
        key = f"notice:{kind}:{thread_id}"
        now = time.time()
        try:
            last = float(self.store.get_setting(key) or 0)
        except (TypeError, ValueError):
            last = 0.0
        if now - last < self.NOTICE_COOLDOWN_SECONDS:
            return
        self.store.set_setting(key, str(now))
        self._notify_thread(thread_id, text)

    def _notify_writer_conflict(self, thread_id: str) -> None:
        self._notify_once(
            thread_id,
            "writer",
            "这个任务被本机另一个 Codex 客户端占用了：那条会话在客户端里开着，写锁被它拿着。"
            "请先在客户端关掉这条会话，我会接着跑；消息已经排在队列里，不会丢。",
        )

    def _notify_foreign_turns(self, thread_id: str, resumed: Dict[str, Any]) -> None:
        """Warn when turns we did not start are already on this thread."""
        thread = (resumed or {}).get("thread") or {}
        turns = thread.get("turns") or []
        known = self.store.turn_ids(thread_id)
        ids = [
            str(turn.get("id") or turn.get("turnId") or "")
            for turn in turns
            if isinstance(turn, dict)
        ]
        if any(turn_id and turn_id not in known for turn_id in ids):
            self._notify_once(
                thread_id,
                "foreign",
                "注意：这条任务在本机其它 Codex 客户端里被继续过，上下文已经不只来自飞书。"
                "建议在飞书里新开一个话题继续。",
            )

    def _scheduler_loop(self) -> None:
        while not self.stop_event.wait(3):
            try:
                for thread_id in self.store.queued_threads():
                    self._safe_dispatch(thread_id)
            except Exception:
                LOG.exception("Scheduler tick failed")

    # =====================================================================
    # app-server events
    # =====================================================================
    def _app_event_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.app_events.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._handle_app_event(message)
            except Exception:
                LOG.exception("Failed to handle an app-server event")

    def _handle_app_event(self, message: Dict[str, Any]) -> None:
        method = message.get("method") or ""
        params = message.get("params") or {}
        thread_id = str(params.get("threadId") or params.get("thread_id") or "")

        if method == "item/started":
            item = params.get("item") or {}
            if thread_id:
                self._note_activity(thread_id, item)
            item_id = str(item.get("id") or item.get("itemId") or "")
            if item_id and item.get("type") in {"commandExecution", "fileChange"}:
                self.store.remember_approval_item(item_id, item)
            if item.get("type") in {"contextCompaction", "context_compaction"} and thread_id:
                self._notify_thread(thread_id, "Codex 正在自动压缩本话题的对话上下文。")
            return

        if method == "item/completed":
            item = params.get("item") or {}
            item_id = str(item.get("id") or item.get("itemId") or "")
            self.store.forget_approval_item(item_id)
            turn_id = str(params.get("turnId") or params.get("turn_id") or "") or None
            if item.get("type") in {"contextCompaction", "context_compaction"} and thread_id:
                self._notify_thread(thread_id, "上下文压缩完成，可以继续提问。")
                return
            if thread_id:
                self._capture_artifacts(thread_id, turn_id, item)
            if item.get("type") == "agentMessage" and thread_id:
                text, actions = self._extract_next_actions(str(item.get("text") or ""))
                self.store.set_setting(
                    f"next_actions:{thread_id}", json.dumps(actions, ensure_ascii=False)
                )
                self.store.set_final(thread_id, sanitize(text))
            return

        if method == "turn/started" and thread_id:
            turn = params.get("turn") or {}
            turn_id = str(turn.get("id") or turn.get("turnId") or "")
            if turn_id:
                self._record_turn_started(thread_id, turn_id)
                self._start_heartbeat(thread_id, turn_id)
            return

        if method == "thread/compacted" and thread_id:
            self._notify_thread(thread_id, "上下文压缩完成，可以继续提问。")
            self.store.set_setting(f"compact_state:{thread_id}", "")
            return

        if method == "turn/completed" and thread_id:
            self._finish_turn(thread_id, params)
            return

        if method == "item/commandExecution/requestApproval" and "id" in message:
            self._request_approval(thread_id, message, params, "commandExecution")
            return
        if method == "item/fileChange/requestApproval" and "id" in message:
            self._request_approval(thread_id, message, params, "fileChange")
            return
        if method == "item/permissions/requestApproval" and "id" in message:
            self._request_approval(thread_id, message, params, "permissions")
            return
        if method == "mcpServer/elicitation/request" and "id" in message:
            self.app.respond(message["id"], {"action": "decline", "content": None})
            return
        if "id" in message and method:
            LOG.warning("Declining an unsupported app-server request: %s", method)
            self.app.respond(message["id"], {"decision": "decline"})

    def _finish_turn(self, thread_id: str, params: Dict[str, Any]) -> None:
        route = self.store.route(thread_id)
        if not route:
            return
        chat_id = str(route["chat_id"])
        turn = params.get("turn") or {}
        status = str(turn.get("status") or "completed")
        turn_id = str(turn.get("id") or turn.get("turnId") or "")
        self._stop_heartbeat(thread_id)
        self._record_turn_finished(thread_id, status)
        source_messages = self.store.source_messages_for_turn(thread_id, turn_id or None)
        self.store.finish_running(thread_id, turn_id or None)

        error = turn.get("error") or {}
        error_message = error.get("message", "") if isinstance(error, dict) else str(error)
        if status == "failed" and error_message:
            body = f"本次任务执行失败：{error_message}"
        else:
            body = self.store.pop_final(thread_id) or f"Turn {status}."
        raw_actions = self.store.get_setting(f"next_actions:{thread_id}", "") or ""
        try:
            actions = json.loads(raw_actions) if raw_actions else []
        except json.JSONDecodeError:
            actions = []
        self.store.set_setting(f"next_actions:{thread_id}", "")
        card = result_card(status, body)
        if status == "completed" and actions:
            card["elements"].append(self._next_action_row(chat_id, thread_id, actions))
        self._send_card(chat_id, card, None, thread_id=thread_id)

        emoji = "✅" if status == "completed" else "❌"
        for source in source_messages:
            if source.startswith("-"):
                continue
            self.store.enqueue_outbox(
                chat_id,
                thread_id,
                "react",
                {"message_id": source, "emoji": emoji},
                dedupe_key=f"react:done:{turn_id}:{source}",
            )
        self._flush_artifacts(chat_id, thread_id)
        self._safe_dispatch(thread_id)

    def _next_action_row(
        self, chat_id: str, thread_id: str, actions: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        from .im.feishu.render import button, button_row

        buttons = []
        for action in actions[:4]:
            label = str(action.get("label") or "继续")[:28]
            prompt = str(action.get("prompt") or "")
            if not prompt:
                continue
            token = self.store.remember_next_action(chat_id, thread_id, label, prompt)
            buttons.append(button(label, {"action": "next", "token": token}))
        return button_row(buttons)

    @staticmethod
    def _extract_next_actions(text: str) -> Tuple[str, List[Dict[str, str]]]:
        match = NEXT_ACTIONS_PATTERN.search(text or "")
        if not match:
            return text, []
        cleaned = NEXT_ACTIONS_PATTERN.sub("", text).strip()
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            return cleaned, []
        actions: List[Dict[str, str]] = []
        if isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, dict) and entry.get("prompt"):
                    actions.append(
                        {"label": str(entry.get("label") or ""), "prompt": str(entry["prompt"])}
                    )
        return cleaned, actions

    # --- approvals -------------------------------------------------------
    def _request_approval(
        self, thread_id: str, message: Dict[str, Any], params: Dict[str, Any], kind: str
    ) -> None:
        route = self.store.route(thread_id) if thread_id else None
        if not route:
            self.app.respond(message["id"], {"decision": "decline"})
            return
        chat_id = str(route["chat_id"])
        token = self.store.add_pending(message["id"], message["method"], params, chat_id)
        reason = str(params.get("reason") or "Codex 需要你的确认才能继续。")
        if kind == "permissions":
            detail = json.dumps(params.get("permissions") or {}, ensure_ascii=False, indent=2)
            request_type = "额外访问权限"
        else:
            item_id = str(params.get("itemId") or params.get("item_id") or "")
            item = self.store.approval_item(item_id) or {}
            detail = self._approval_detail(kind, params, item)
            request_type = "执行命令" if kind == "commandExecution" else "修改文件"
        if re.search(r"(^|[;&|]\s*)rm\s", detail):
            reason += "\n\n注意：该命令包含删除操作，请确认路径。"
        self._send_card(
            chat_id,
            approval_card(request_type, reason, detail, token),
            None,
            thread_id=thread_id,
            dedupe_key=f"approval:{token}",
        )

    @staticmethod
    def _approval_detail(kind: str, params: Dict[str, Any], item: Dict[str, Any]) -> str:
        if kind == "commandExecution":
            command = params.get("command") or item.get("command") or ""
            cwd = params.get("cwd") or item.get("cwd") or ""
            lines = []
            if cwd:
                lines.append(f"工作目录：{cwd}")
            if command:
                lines.append(f"命令：{command}")
            return "\n".join(lines)
        changes = params.get("changes") or item.get("changes") or []
        if isinstance(changes, list):
            lines = []
            for change in changes:
                if isinstance(change, dict):
                    lines.append(
                        f"{change.get('kind') or change.get('type') or 'change'}: "
                        f"{change.get('path') or change.get('file') or ''}"
                    )
            return "\n".join(lines)
        return str(changes)

    def _resolve_approval(self, chat_id: str, token: str, decision: str, card_message_id: str) -> None:
        row = self.store.pending(token)
        if not row or str(row["chat_id"]) != chat_id:
            if card_message_id:
                self.store.enqueue_outbox(
                    chat_id,
                    None,
                    "card_patch",
                    {"message_id": card_message_id, "card": approval_result_card("授权", "expired")},
                    dedupe_key=f"patch:expired:{card_message_id}",
                )
            return
        if decision not in ("accept", "acceptForSession", "decline"):
            return
        method = str(row["method"])
        params = json.loads(row["params_json"])
        if method == "item/permissions/requestApproval":
            result: Dict[str, Any] = (
                {"permissions": params.get("permissions") or {}, "scope": "session"}
                if decision != "decline"
                else {"permissions": [], "scope": "turn"}
            )
        else:
            result = {"decision": decision}
        try:
            self.app.respond(json.loads(row["request_id"]), result)
        except Exception:
            LOG.exception("Failed to answer an approval request")
            return
        self.store.delete_pending(token)
        if card_message_id:
            self.store.enqueue_outbox(
                chat_id,
                None,
                "card_patch",
                {
                    "message_id": card_message_id,
                    "card": approval_result_card("授权", decision, str(params.get("reason") or "")),
                },
                dedupe_key=f"patch:{token}:{decision}",
            )

    # =====================================================================
    # progress
    # =====================================================================
    def _progress_key(self, thread_id: str) -> str:
        return f"progress:{thread_id}"

    def _load_progress(self, thread_id: str) -> Dict[str, Any]:
        raw = self.store.get_setting(self._progress_key(thread_id), "")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _save_progress(self, thread_id: str, progress: Dict[str, Any]) -> None:
        self.store.set_setting(
            self._progress_key(thread_id), json.dumps(progress, ensure_ascii=False)
        )

    def _record_turn_started(self, thread_id: str, turn_id: str) -> None:
        progress = self._load_progress(thread_id)
        progress.update(
            {
                "turn_id": turn_id,
                "started_at": int(time.time()),
                "heartbeat_at": int(time.time()),
                "status": "running",
                "recent": [],
            }
        )
        self._save_progress(thread_id, progress)

    def _note_activity(self, thread_id: str, item: Dict[str, Any]) -> None:
        progress = self._load_progress(thread_id)
        recent = list(progress.get("recent") or [])
        label = self._item_label(item)
        if label:
            recent.append({"at": int(time.time()), "label": label})
        progress["recent"] = recent[-5:]
        progress["last_activity_at"] = int(time.time())
        self._save_progress(thread_id, progress)

    @staticmethod
    def _item_label(item: Dict[str, Any]) -> str:
        item_type = str(item.get("type") or "")
        if item_type == "commandExecution":
            return f"执行命令：{str(item.get('command') or '')[:120]}"
        if item_type == "fileChange":
            changes = item.get("changes") or []
            if isinstance(changes, list) and changes:
                first = changes[0] or {}
                return f"修改文件：{first.get('path') or first.get('file') or ''}"
            return "修改文件"
        if item_type in ("reasoning", "agentMessage"):
            return "思考与整理回复"
        if item_type in ("mcpToolCall", "toolCall"):
            return f"调用工具：{str(item.get('name') or item.get('tool') or '')}"
        return ""

    def _record_turn_finished(self, thread_id: str, status: str) -> None:
        progress = self._load_progress(thread_id)
        progress["status"] = status
        progress["finished_at"] = int(time.time())
        self._save_progress(thread_id, progress)

    def _start_heartbeat(self, thread_id: str, turn_id: str) -> None:
        key = f"heartbeat:{thread_id}"
        if self.store.get_setting(key, ""):
            return
        self.store.set_setting(key, turn_id)

        def tick() -> None:
            delay = FIRST_PROGRESS_NOTICE_SECONDS
            while not self.stop_event.wait(delay):
                if self.store.running_turn(thread_id) is None:
                    self.store.set_setting(key, "")
                    return
                route = self.store.route(thread_id)
                if route:
                    self._send_card(
                        str(route["chat_id"]),
                        self._progress_card(thread_id),
                        None,
                        thread_id=thread_id,
                    )
                delay = PROGRESS_HEARTBEAT_SECONDS

        threading.Thread(target=tick, name=f"heartbeat-{thread_id[:6]}", daemon=True).start()

    def _stop_heartbeat(self, thread_id: str) -> None:
        self.store.set_setting(f"heartbeat:{thread_id}", "")

    def _progress_card(self, thread_id: str) -> Dict[str, Any]:
        progress = self._load_progress(thread_id)
        running = self.store.running_turn(thread_id) is not None
        started = progress.get("started_at")
        lines: List[str] = []
        if started:
            elapsed = int(time.time() - int(started))
            lines.append(f"已运行 {elapsed // 60} 分 {elapsed % 60} 秒")
        lines.append(f"状态：{'执行中' if running else progress.get('status') or '空闲'}")
        recent = progress.get("recent") or []
        if recent:
            lines.append("")
            lines.append("**最近动作**")
            for entry in recent[-5:]:
                stamp = time.strftime("%H:%M:%S", time.localtime(int(entry.get("at") or 0)))
                lines.append(f"- {stamp} {entry.get('label')}")
        return progress_card("执行进度", lines, thread_id, running)

    # =====================================================================
    # artifacts
    # =====================================================================
    def _artifact_allowed(self, path: Path, thread_id: str) -> bool:
        roots: List[Path] = []
        route = self.store.route(thread_id)
        if route and route["project"]:
            configured = self._projects().get(str(route["project"]))
            if configured:
                roots.append(Path(configured).expanduser())
        for root in self.config.get("artifact_roots") or []:
            roots.append(Path(str(root)).expanduser())
        roots.append(self.base_dir / "artifacts")
        resolved = path.resolve()
        for root in roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except (ValueError, OSError):
                continue
        return False

    def _persist_artifact(self, path: Path) -> Path:
        directory = self.base_dir / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{uuid.uuid4().hex[:8]}_{path.name}"
        try:
            target.write_bytes(path.read_bytes())
        except OSError:
            return path
        return target

    def _capture_artifacts(
        self, thread_id: str, turn_id: Optional[str], item: Dict[str, Any]
    ) -> None:
        item_type = str(item.get("type") or "")
        paths: List[str] = []
        if item_type in ("imageView", "image"):
            candidate = item.get("path") or item.get("imagePath")
            if candidate:
                paths.append(str(candidate))
        if item_type in ("fileChange", "document", "file"):
            candidate = item.get("path")
            if candidate:
                paths.append(str(candidate))
        if isinstance(item.get("artifacts"), list):
            for entry in item["artifacts"]:
                if isinstance(entry, dict) and entry.get("path"):
                    paths.append(str(entry["path"]))
        for candidate in paths:
            path = Path(candidate)
            if not path.is_file():
                continue
            if not self._artifact_allowed(path, thread_id):
                LOG.info("Artifact %s is outside the trusted roots; skipping", path)
                continue
            fingerprint = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()
            self.store.add_artifact(thread_id, turn_id, str(path), fingerprint)

    def _flush_artifacts(self, chat_id: str, thread_id: str) -> None:
        for row in self.store.pending_artifacts(thread_id):
            path = Path(str(row["path"]))
            if not path.is_file():
                self.store.mark_artifact_suppressed(int(row["id"]))
                continue
            persisted = self._persist_artifact(path)
            kind = "image" if persisted.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
            } else "file"
            self.store.enqueue_outbox(
                chat_id,
                thread_id,
                kind,
                {"path": str(persisted)},
                dedupe_key=f"artifact:{row['id']}:{row['fingerprint']}",
            )
            self.store.mark_artifact_sent(int(row["id"]))

    # =====================================================================
    # outbound delivery
    # =====================================================================
    def _outbox_loop(self) -> None:
        while not self.stop_event.is_set():
            row = self.store.claim_outbox(self.worker_id)
            if row is None:
                self.stop_event.wait(0.5)
                continue
            try:
                self._deliver(row)
                self.store.complete_outbox(int(row["id"]))
            except Exception as exc:
                attempts = int(row["attempts"] or 0) + 1
                LOG.warning("Outbox %s delivery failed (%s): %s", row["id"], attempts, exc)
                if attempts >= 5:
                    self.store.fail_outbox(int(row["id"]), str(exc))
                else:
                    self.store.retry_outbox(int(row["id"]), attempts, str(exc))

    def _deliver(self, row: Any) -> None:
        kind = str(row["kind"])
        payload = json.loads(row["payload_json"])
        chat_id = str(row["chat_id"])
        thread_id = row["thread_id"]
        root = ""
        if thread_id:
            route = self.store.route(str(thread_id))
            if route:
                root = str(route["root_message_id"] or "")

        if kind == "react":
            self.im.react(str(payload["message_id"]), str(payload.get("emoji") or ""))
            return

        if kind == "card_patch":
            self.im.patch_card(str(payload["message_id"]), payload["card"])
            return

        if kind == "text":
            text = str(payload.get("text") or "")
            message_id = self._reply_or_post(
                thread_id,
                chat_id,
                root,
                lambda anchor: self.im.reply_text(chat_id, anchor, text),
                lambda: self.im.send_text(chat_id, text),
            )
        elif kind == "card":
            cards = payload.get("cards") or [payload["card"]]
            message_id = ""
            for index, card_payload in enumerate(cards):
                message_id = self._reply_or_post(
                    thread_id,
                    chat_id,
                    root,
                    lambda anchor, card=card_payload: self.im.reply_card(
                        chat_id, anchor, card
                    ),
                    lambda card=card_payload: self.im.send_card(chat_id, card),
                )
                if index == 0 and thread_id and not root:
                    root = message_id
                    self.store.route_thread(
                        str(thread_id), chat_id, root_message_id=message_id
                    )
        elif kind == "image":
            image_key = self.im.upload_image(Path(str(payload["path"])))
            message_id = self._reply_or_post(
                thread_id,
                chat_id,
                root,
                lambda anchor: self.im.send_image(chat_id, image_key, anchor),
                lambda: self.im.send_image(chat_id, image_key, None),
            )
        elif kind == "file":
            path = Path(str(payload["path"]))
            file_key = self.im.upload_file(path)
            message_id = self._reply_or_post(
                thread_id,
                chat_id,
                root,
                lambda anchor: self.im.send_file(chat_id, file_key, path.name, anchor),
                lambda: self.im.send_file(chat_id, file_key, path.name, None),
            )
        else:
            raise ValueError(f"Unsupported outbox kind: {kind}")

        if message_id and thread_id:
            self.store.map_message(chat_id, message_id, str(thread_id))

    def _reply_or_post(self, thread_id: Any, chat_id: str, root: str, reply: Any, post: Any) -> str:
        """Reply inside the topic, or post into the chat when the anchor is gone.

        A Feishu topic hangs off its first message. If the user recalls that
        message, every later reply into the topic fails; the only way to keep
        the answer from being lost is to post it into the chat directly.
        """
        if root:
            try:
                return reply(root)
            except AnchorLostError as exc:
                LOG.warning("Topic anchor %s is gone (%s); posting into the chat", root, exc)
                self._forget_anchor(thread_id, chat_id)
        return post()

    def _forget_anchor(self, thread_id: Any, chat_id: str) -> None:
        if not thread_id:
            return
        # Drop only the delivery anchor: the Feishu thread id is how inbound
        # messages in that topic still find this task, so it has to survive.
        self.store.route_thread(str(thread_id), chat_id, root_message_id=None)
        self._notify_once(
            str(thread_id),
            "anchor",
            "这个话题的起始消息被撤回了，机器人无法再回复到该话题里；"
            "接下来的消息会作为新的消息发出。",
        )

    def _send_text(
        self,
        chat_id: str,
        text: str,
        ev: Optional[Dict[str, Any]] = None,
        thread_id: Optional[str] = None,
        dedupe_key: Optional[str] = None,
    ) -> None:
        self.store.enqueue_outbox(
            chat_id,
            thread_id,
            "text",
            {"text": text},
            dedupe_key=dedupe_key or f"text:{uuid.uuid4().hex}",
        )

    def _send_card(
        self,
        chat_id: str,
        card_payload: Dict[str, Any],
        ev: Optional[Dict[str, Any]] = None,
        thread_id: Optional[str] = None,
        dedupe_key: Optional[str] = None,
    ) -> None:
        self.store.enqueue_outbox(
            chat_id,
            thread_id,
            "card",
            {"card": card_payload},
            dedupe_key=dedupe_key or f"card:{uuid.uuid4().hex}",
        )

    def _reply_plain(self, chat_id: str, text: str, ev: Optional[Dict[str, Any]]) -> None:
        thread_id = self._resolve_thread(ev) if ev else None
        self._send_card(chat_id, text_card(text), ev, thread_id=thread_id)

    def _notify_thread(self, thread_id: str, text: str) -> None:
        route = self.store.route(thread_id)
        if not route:
            return
        self._send_text(str(route["chat_id"]), text, thread_id=thread_id)

    # =====================================================================
    # commands
    # =====================================================================
    def _handle_command(self, ev: Dict[str, Any], command: commands.Command) -> None:
        chat_id = ev["chat_id"]
        name = command.name
        rest = command.rest
        handler = getattr(self, f"_cmd_{name}", None)
        if handler is None:
            self._reply_plain(chat_id, f"未知指令：/{name}。发送 /help 查看可用指令。", ev)
            return
        LOG.info("Command /%s from %s", name, ev.get("sender_open_id"))
        handler(ev, rest)

    def _cmd_help(self, ev: Dict[str, Any], rest: str) -> None:
        self._reply_plain(ev["chat_id"], commands.help_text(), ev)

    def _cmd_cancel(self, ev: Dict[str, Any], rest: str) -> None:
        chat_id = ev["chat_id"]
        self.store.set_setting(f"pending_picker:{chat_id}", "")
        self._reply_plain(chat_id, "已取消进行中的选择。", ev)

    def _cmd_projects(self, ev: Dict[str, Any], rest: str) -> None:
        projects = self._projects()
        if not projects:
            self._reply_plain(ev["chat_id"], "还没有配置任何项目，请在 config.json 的 projects 里添加。", ev)
            return
        lines = ["**可用项目**", ""]
        for alias, path in projects.items():
            lines.append(f"- `{alias}` → {path}")
        lines += ["", "用法：`@机器人 <项目> <任务内容>`，或 `/new <项目> <任务内容>`。"]
        self._reply_plain(ev["chat_id"], "\n".join(lines), ev)

    def _cmd_status(self, ev: Dict[str, Any], rest: str) -> None:
        chat_id = ev["chat_id"]
        thread_id = self._resolve_thread(ev)
        if not thread_id:
            self._reply_plain(chat_id, "当前话题还没有绑定任何 Codex 任务。", ev)
            return
        running, queued = self.store.queue_counts(thread_id)
        route = self.store.route(thread_id)
        model, effort = self._model_choice(chat_id)
        lines = [
            f"任务：`{thread_id}`",
            f"标题：{route['title'] if route and route['title'] else '(未命名)'}",
            f"正在执行：{running} 个 turn",
            f"排队中：{queued} 条消息",
            f"模型：{model or '(未指定)'} / {effort or '(默认强度)'}",
        ]
        self._reply_plain(chat_id, "\n".join(lines), ev)

    def _cmd_where(self, ev: Dict[str, Any], rest: str) -> None:
        chat_id = ev["chat_id"]
        thread_id = self._resolve_thread(ev)
        model, effort = self._model_choice(chat_id)
        lines = [f"会话：`{chat_id}`", f"当前任务：`{thread_id or '(无)'}`"]
        if thread_id:
            route = self.store.route(thread_id)
            if route:
                lines.append(f"飞书话题：`{route['feishu_thread_id'] or '(未记录)'}`")
                lines.append(f"项目：{route['project'] or '(未记录)'}")
        lines += [
            f"模型：{model or '(首次任务时选择)'}",
            f"推理强度：{effort or '(默认)'}",
            f"审批策略：{self.config['approval_policy']}",
        ]
        self._reply_plain(chat_id, "\n".join(lines), ev)

    def _models(self) -> List[Dict[str, Any]]:
        try:
            result = self.app.request(
                "model/list", {"cursor": None, "limit": 100, "includeHidden": False}
            )
        except Exception:
            LOG.exception("model/list failed")
            return []
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, list):
            return data
        return result.get("models") or []

    def _cmd_models(self, ev: Dict[str, Any], rest: str) -> None:
        models = self._models()
        if not models:
            self._reply_plain(ev["chat_id"], "暂时拿不到模型列表。", ev)
            return
        thread_id = self._resolve_thread(ev) or "none"
        current, _ = self._model_choice(ev["chat_id"])
        self._send_card(
            ev["chat_id"], model_picker_card(models, thread_id, current or ""), ev
        )

    def _cmd_model(self, ev: Dict[str, Any], rest: str) -> None:
        chat_id = ev["chat_id"]
        if not rest:
            self._cmd_models(ev, "")
            return
        parts = rest.split()
        model = parts[0]
        effort = parts[1] if len(parts) > 1 else None
        self._remember_model(chat_id, model, effort)
        self._reply_plain(
            chat_id, f"已设置：模型 `{model}`，推理强度 `{effort or '默认'}`。", ev
        )

    def _cmd_new(self, ev: Dict[str, Any], rest: str) -> None:
        chat_id = ev["chat_id"]
        project, remainder = self._infer_project(rest)
        model, _ = self._model_choice(chat_id)
        token = self._new_draft(
            chat_id=chat_id,
            message_id=ev["message_id"],
            prompt=rest,
            project=project,
            feishu_thread_id=ev.get("thread_id") or "",
            root_message_id=ev.get("root_id") or ev.get("message_id") or "",
            model=model,
        )
        if model is None:
            self._send_model_picker(chat_id, token, ev)
            return
        if project is None:
            self._send_project_picker(chat_id, token, rest)
            return
        self._materialize_task(token)

    def _cmd_clear(self, ev: Dict[str, Any], rest: str) -> None:
        self._cmd_new(ev, rest)

    def _cmd_resume(self, ev: Dict[str, Any], rest: str) -> None:
        chat_id = ev["chat_id"]
        if rest:
            thread_id = self._resolve_thread_prefix(rest)
            if not thread_id:
                self._reply_plain(chat_id, f"没有找到匹配 `{rest}` 的任务。", ev)
                return
            self._bind_topic(ev, thread_id)
            self._reply_plain(chat_id, f"已绑定到任务 `{thread_id}`。", ev)
            return
        self._cmd_tasks(ev, "")

    def _cmd_use(self, ev: Dict[str, Any], rest: str) -> None:
        self._cmd_resume(ev, rest)

    def _cmd_tasks(self, ev: Dict[str, Any], rest: str) -> None:
        chat_id = ev["chat_id"]
        try:
            result = self.app.request(
                "thread/list", {"cursor": None, "limit": 20, "archived": False}
            )
        except Exception:
            LOG.exception("thread/list failed")
            self._reply_plain(chat_id, "读取任务列表失败。", ev)
            return
        threads = result.get("data") or result.get("threads") or []
        if rest:
            needle = rest.lower()
            threads = [
                item
                for item in threads
                if needle in str(item.get("title") or "").lower()
                or needle in str(item.get("id") or "").lower()
            ]
        card = task_list_card(threads, "", 0, False)
        self._send_card(chat_id, card, ev)

    def _resolve_thread_prefix(self, prefix: str) -> Optional[str]:
        cleaned = prefix.strip()
        if not cleaned:
            return None
        try:
            result = self.app.request(
                "thread/list", {"cursor": None, "limit": 100, "archived": False}
            )
        except Exception:
            return None
        threads = result.get("data") or result.get("threads") or []
        matches = [str(item.get("id") or "") for item in threads if str(item.get("id") or "").startswith(cleaned)]
        if len(matches) == 1:
            return matches[0]
        return None

    def _bind_topic(self, ev: Dict[str, Any], thread_id: str) -> None:
        chat_id = ev["chat_id"]
        feishu_thread_id = ev.get("thread_id") or ""
        root_message_id = ev.get("root_id") or ev.get("message_id") or ""
        self.store.route_thread(
            thread_id,
            chat_id,
            feishu_thread_id=feishu_thread_id,
            root_message_id=root_message_id,
        )
        if root_message_id:
            self.store.map_message(chat_id, root_message_id, thread_id)
        self.store.update_binding(chat_id, current_thread_id=thread_id)
        self.fresh_threads.discard(thread_id)

    def _cmd_progress(self, ev: Dict[str, Any], rest: str) -> None:
        thread_id = self._resolve_thread(ev)
        if not thread_id:
            self._reply_plain(ev["chat_id"], "当前话题还没有绑定任务。", ev)
            return
        self._send_card(ev["chat_id"], self._progress_card(thread_id), ev, thread_id=thread_id)

    def _cmd_stop(self, ev: Dict[str, Any], rest: str) -> None:
        thread_id = self._resolve_thread(ev)
        if not thread_id:
            self._reply_plain(ev["chat_id"], "当前话题还没有绑定任务。", ev)
            return
        self._interrupt(ev["chat_id"], thread_id)

    def _interrupt(self, chat_id: str, thread_id: str) -> None:
        running = self.store.running_turn(thread_id)
        if not running or not running["turn_id"]:
            self._send_text(chat_id, "当前没有正在执行的处理。", thread_id=thread_id)
            return
        try:
            self.app.request(
                "turn/interrupt", {"threadId": thread_id, "turnId": str(running["turn_id"])}
            )
            self._send_text(chat_id, "已发送中断请求。", thread_id=thread_id)
        except Exception:
            LOG.exception("turn/interrupt failed")
            self._send_text(chat_id, "中断失败，请查看 Bridge 日志。", thread_id=thread_id)

    def _cmd_compact(self, ev: Dict[str, Any], rest: str) -> None:
        thread_id = self._resolve_thread(ev)
        if not thread_id:
            self._reply_plain(ev["chat_id"], "当前话题还没有绑定任务。", ev)
            return
        try:
            self.app.request("thread/compact/start", {"threadId": thread_id})
            self.store.set_setting(f"compact_state:{thread_id}", "running")
            self._send_text(
                ev["chat_id"], "正在压缩上下文，完成后会通知你。", thread_id=thread_id
            )
        except Exception:
            LOG.exception("compaction failed")
            self._reply_plain(ev["chat_id"], "压缩请求失败。", ev)

    def _cmd_renew(self, ev: Dict[str, Any], rest: str) -> None:
        thread_id = self._resolve_thread(ev)
        if not thread_id:
            self._reply_plain(ev["chat_id"], "当前话题还没有绑定任务。", ev)
            return
        chat_id = ev["chat_id"]
        try:
            self.app.request("thread/compact/start", {"threadId": thread_id})
            result = self.app.request("thread/fork", {"threadId": thread_id})
            fork = result.get("thread") or result
            new_thread_id = str(fork.get("id") or fork.get("threadId") or "")
            if not new_thread_id:
                raise AppServerError("thread/fork returned no thread id")
            self.store.remember_handoff(thread_id, new_thread_id)
            self._bind_topic(ev, new_thread_id)
            self._send_text(chat_id, f"已切换到继承历史的新任务 `{new_thread_id}`。", thread_id=new_thread_id)
        except Exception:
            LOG.exception("renew failed")
            self._reply_plain(chat_id, "续接失败，仍保持原任务。", ev)

    def _cmd_fork(self, ev: Dict[str, Any], rest: str) -> None:
        thread_id = self._resolve_thread(ev)
        if not thread_id:
            self._reply_plain(ev["chat_id"], "当前话题还没有绑定任务。", ev)
            return
        try:
            result = self.app.request("thread/fork", {"threadId": thread_id})
            fork = result.get("thread") or result
            new_thread_id = str(fork.get("id") or fork.get("threadId") or "")
            if not new_thread_id:
                raise AppServerError("thread/fork returned no thread id")
            self._bind_topic(ev, new_thread_id)
            self._reply_plain(ev["chat_id"], f"已分叉到新任务 `{new_thread_id}`。", ev)
        except Exception:
            LOG.exception("fork failed")
            self._reply_plain(ev["chat_id"], "分叉失败。", ev)

    def _cmd_rename(self, ev: Dict[str, Any], rest: str) -> None:
        thread_id = self._resolve_thread(ev)
        if not thread_id:
            self._reply_plain(ev["chat_id"], "当前话题还没有绑定任务。", ev)
            return
        title = rest.strip()
        if not title:
            route = self.store.route(thread_id)
            title = str((route["title"] if route else "") or "").strip()
        if not title:
            self._reply_plain(ev["chat_id"], "请给出新标题：`/rename 新标题`。", ev)
            return
        self.store.route_thread(thread_id, ev["chat_id"], title=title)
        root = (self.store.route(thread_id) or {"root_message_id": ""})["root_message_id"]
        if root:
            self.store.enqueue_outbox(
                ev["chat_id"],
                None,
                "card_patch",
                {"message_id": str(root), "card": text_card(f"📋 {title}")},
                dedupe_key=f"rename:{thread_id}:{int(time.time())}",
            )
        self._reply_plain(ev["chat_id"], f"已重命名：{title}", ev)

    def _cmd_upload_revoke(self, ev: Dict[str, Any], rest: str) -> None:
        thread_id = self._resolve_thread(ev)
        if thread_id:
            self.store.set_setting(f"upload_auth:{thread_id}", "")
        self._reply_plain(ev["chat_id"], "已撤销当前任务的持续上传授权。", ev)

    # =====================================================================
    # card actions
    # =====================================================================
    def _handle_card_action(self, payload: Dict[str, Any]) -> None:
        chat_id = payload.get("chat_id") or ""
        open_id = payload.get("operator_open_id") or ""
        value = payload.get("value") or {}
        card_message_id = payload.get("message_id") or ""
        if not chat_id or not self._authorized(chat_id, open_id):
            LOG.info("Ignoring a card action from an unauthorized operator")
            return
        action = str(value.get("action") or "")

        if action == "approval":
            self._resolve_approval(
                chat_id, str(value.get("token") or ""), str(value.get("decision") or ""), card_message_id
            )
            return
        if action == "model":
            self._apply_model_choice(chat_id, value, card_message_id)
            return
        if action == "project":
            self._apply_project_choice(chat_id, value, card_message_id)
            return
        if action == "project_page":
            token = str(value.get("token") or "")
            draft = self._load_draft(token)
            if not draft:
                return
            self._send_project_picker(
                chat_id, token, str(draft.get("prompt") or ""), int(value.get("page") or 0)
            )
            return
        if action == "interrupt":
            self._interrupt(chat_id, str(value.get("thread") or ""))
            return
        if action == "progress_refresh":
            thread_id = str(value.get("thread") or "")
            if thread_id and card_message_id:
                self.store.enqueue_outbox(
                    chat_id,
                    None,
                    "card_patch",
                    {"message_id": card_message_id, "card": self._progress_card(thread_id)},
                    dedupe_key=f"progress:{card_message_id}:{int(time.time())}",
                )
            return
        if action == "resume":
            thread_id = str(value.get("thread") or "")
            if thread_id:
                fake_event = {
                    "chat_id": chat_id,
                    "message_id": card_message_id,
                    "thread_id": self.store.thread_for_message(chat_id, card_message_id) or "",
                }
                self._bind_topic(fake_event, thread_id)
                self._send_text(chat_id, f"已绑定到任务 `{thread_id}`。")
            return
        if action == "next":
            row = self.store.consume_next_action(str(value.get("token") or ""), chat_id)
            if not row:
                return
            thread_id = str(row["thread_id"])
            self._submit_turn(
                chat_id,
                thread_id,
                [{"type": "text", "text": str(row["prompt"])}],
                {"message_id": f"-{time.time_ns()}"},
            )
            return
        LOG.debug("Unhandled card action: %s", action)

    def _apply_model_choice(
        self, chat_id: str, value: Dict[str, Any], card_message_id: str
    ) -> None:
        model = str(value.get("model") or "")
        token = str(value.get("thread") or "")
        if model:
            self._remember_model(chat_id, model)
        draft = self._load_draft(token)
        if draft:
            draft["model"] = model
            self._save_draft(token, draft)
            if not draft.get("project"):
                self._send_project_picker(chat_id, token, str(draft.get("prompt") or ""))
                return
            self._materialize_task(token)
            return
        self._send_text(chat_id, f"已设置模型 `{model}`。")

    def _apply_project_choice(
        self, chat_id: str, value: Dict[str, Any], card_message_id: str
    ) -> None:
        token = str(value.get("token") or "")
        project = str(value.get("project") or "")
        draft = self._load_draft(token)
        if not draft:
            self._send_text(chat_id, "这个选择已经过期，请重新发起任务。")
            return
        draft["project"] = project
        self._save_draft(token, draft)
        self._materialize_task(token)

    # =====================================================================
    # health and watchdog
    # =====================================================================
    def _start_health_server(self) -> None:
        host = self.config["health_host"]
        port = int(self.config["health_port"])
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                payload = bridge.health()
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200 if payload.get("ok") else 503)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:  # keep the log clean
                return

        try:
            self._httpd = ThreadingHTTPServer((host, port), Handler)
        except OSError as exc:
            LOG.error("Health endpoint unavailable on %s:%s (%s)", host, port, exc)
            return
        threading.Thread(target=self._httpd.serve_forever, name="health", daemon=True).start()
        LOG.info("Health endpoint on http://%s:%s", host, port)

    def health(self) -> Dict[str, Any]:
        return {
            "ok": self.app.alive(),
            "instance": self.config.get("instance"),
            "bound_chats": len(self.store.bound_chats()),
            "app_server_pid": self.app.pid(),
            "app_server_fds": self.app.fd_count(),
            "app_server_age_seconds": int(self.app.age_seconds()),
            "recycles": self._recycles,
            "last_recycle_reason": self._last_recycle_reason,
            "running_turns": self.store.active_turn_count(),
            "queued_threads": len(self.store.queued_threads()),
        }

    def _watchdog_loop(self) -> None:
        settings = self.config.get("app_server_watchdog") or {}
        interval = int(settings.get("check_interval_seconds") or 60)
        fd_threshold = int(settings.get("recycle_fd_threshold") or 180)
        max_age = int(settings.get("max_age_seconds") or 86400)
        while not self.stop_event.wait(interval):
            if not self.app.alive():
                try:
                    self.app.start()
                    LOG.warning("App-server was not alive; restarted it")
                except Exception:
                    LOG.exception("App-server restart failed")
                continue
            fds = self.app.fd_count()
            too_many_fds = fds is not None and fds > fd_threshold
            too_old = self.app.age_seconds() > max_age
            if not (too_many_fds or too_old):
                continue
            if self.app.busy() or self.store.active_turn_count():
                continue
            reason = f"fd_count={fds}" if too_many_fds else f"age>{max_age}s"
            LOG.info("Recycling the app-server (%s)", reason)
            try:
                self.app.restart()
                self._recycles += 1
                self._last_recycle_reason = reason
            except Exception:
                LOG.exception("App-server recycle failed")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="feishu-bridge", description=__doc__)
    parser.add_argument("--config", type=Path, default=None, help="Path to config.json")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "pair-code", "health"],
        help="run the bridge, print the pairing code, or probe the health endpoint",
    )
    args = parser.parse_args(argv)

    base = app_dir()
    config = load_config(args.config)
    if args.command == "pair-code":
        store = Store(base / "state.sqlite3")
        print(store.pair_code())
        store.close()
        return 0
    if args.command == "health":
        import urllib.request

        url = f"http://{config['health_host']}:{config['health_port']}/"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                print(response.read().decode("utf-8"))
            return 0
        except Exception as exc:
            print(f"health check failed: {exc}")
            return 1

    configure_logging(base / "bridge.log", args.verbose)
    platform = current_platform()
    app_events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    # Resolve the CLI before touching any state: a missing executable is an
    # operator error and must read as one, not as a spawn traceback.
    try:
        codex_path = platform.resolve_codex_path(config.get("codex_path"))
    except CodexNotFound as exc:
        LOG.error("Cannot start the bridge: %s", exc)
        print(f"Cannot start the bridge: {exc}", file=sys.stderr)
        return 2
    LOG.info("Using Codex CLI at %s", codex_path)
    store = Store(base / "state.sqlite3")
    app = AppServer(codex_path, app_events, codex_home(config), platform)
    app_id = platform.secret_get(
        config["credential_service"], config["credential_accounts"]["app_id"]
    ) or os.environ.get("FEISHU_APP_ID")
    app_secret = platform.secret_get(
        config["credential_service"], config["credential_accounts"]["app_secret"]
    ) or os.environ.get("FEISHU_APP_SECRET")
    im = FeishuClient(app_id or "", app_secret or "", config["feishu"]["domain"], base / "inbox")
    bridge = Bridge(config, platform, store, im, app, base)
    bridge.app_events = app_events

    def handle_signal(signum: int, frame: Any) -> None:
        LOG.info("Received signal %s; shutting down", signum)
        bridge.stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)
    try:
        bridge.run()
    except Exception:
        LOG.exception("Bridge terminated with an unhandled error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
