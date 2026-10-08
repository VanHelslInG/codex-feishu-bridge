"""In-memory stand-ins for Feishu and the Codex app-server."""

from __future__ import annotations

import itertools
import queue
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from feishu_bridge.im.base import ImAdapter, TopicRef


class FakeIm(ImAdapter):
    def __init__(self) -> None:
        self.counter = itertools.count(1)
        self.topics: List[Tuple[str, str]] = []
        self.replies: List[Tuple[str, str, Any]] = []
        self.sent: List[Tuple[str, Any]] = []
        self.reactions: List[Tuple[str, str]] = []
        self.patches: List[Tuple[str, Any]] = []
        self.uploads: List[str] = []
        self.messages: Dict[str, str] = {}
        self.downloads: Dict[str, Path] = {}

    def _next(self, prefix: str) -> str:
        return f"{prefix}_{next(self.counter)}"

    def create_topic(self, chat_id: str, text: str) -> TopicRef:
        message_id = self._next("om")
        self.topics.append((chat_id, text))
        return TopicRef(thread_id=self._next("omt"), root_message_id=message_id)

    def reply_text(self, chat_id: str, root_message_id: str, text: str) -> str:
        message_id = self._next("om")
        self.replies.append((root_message_id, "text", text))
        return message_id

    def reply_card(self, chat_id: str, root_message_id: str, card: Dict[str, Any]) -> str:
        message_id = self._next("om")
        self.replies.append((root_message_id, "card", card))
        return message_id

    def send_text(self, chat_id: str, text: str) -> str:
        message_id = self._next("om")
        self.sent.append((chat_id, text))
        return message_id

    def send_card(self, chat_id: str, card: Dict[str, Any]) -> str:
        message_id = self._next("om")
        self.sent.append((chat_id, card))
        return message_id

    def patch_card(self, message_id: str, card: Dict[str, Any]) -> None:
        self.patches.append((message_id, card))

    def send_image(self, chat_id: str, image_key: str, root_message_id: Optional[str] = None) -> str:
        message_id = self._next("om")
        self.sent.append((chat_id, {"image_key": image_key}))
        return message_id

    def send_file(
        self,
        chat_id: str,
        file_key: str,
        file_name: str,
        root_message_id: Optional[str] = None,
    ) -> str:
        message_id = self._next("om")
        self.sent.append((chat_id, {"file_key": file_key, "file_name": file_name}))
        return message_id

    def react(self, message_id: str, emoji: str) -> None:
        self.reactions.append((message_id, emoji))

    def upload_image(self, path: Path) -> str:
        self.uploads.append(str(path))
        return self._next("img")

    def upload_file(self, path: Path, file_type: str = "stream") -> str:
        self.uploads.append(str(path))
        return self._next("file")

    def download_resource(self, message_id: str, file_key: str, resource_type: str) -> Path:
        return self.downloads.get(file_key, Path(f"/tmp/{file_key}"))

    def message_text(self, message_id: str) -> str:
        return self.messages.get(message_id, "")

    # --- assertions helpers ---------------------------------------------
    def cards_sent(self) -> List[Dict[str, Any]]:
        return [payload for _, payload in self.sent if isinstance(payload, dict)]

    def find_card(self, needle: str) -> Optional[Dict[str, Any]]:
        for payload in self.cards_sent():
            if needle in str(payload):
                return payload
        for _, _, payload in self.replies:
            if isinstance(payload, dict) and needle in str(payload):
                return payload
        return None


class FakeApp:
    def __init__(self) -> None:
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.responses: List[Tuple[Any, Dict[str, Any]]] = []
        self.thread_seq = itertools.count(1)
        self.turn_seq = itertools.count(1)
        self.started = 0
        self.model_list: List[Dict[str, Any]] = [
            {"id": "ark-code-latest", "displayName": "Ark Code (Auto)"},
            {"id": "deepseek-v4.1-flash", "displayName": "DeepSeek V4.1 Flash"},
        ]

    # --- AppServer surface ----------------------------------------------
    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        pass

    def alive(self) -> bool:
        return True

    def pid(self) -> Optional[int]:
        return 4242

    def fd_count(self) -> Optional[int]:
        return 10

    def age_seconds(self) -> float:
        return 1.0

    def busy(self) -> bool:
        return False

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self.calls.append((method, params))

    def respond(self, request_id: Any, result: Dict[str, Any]) -> None:
        self.responses.append((request_id, result))

    def request(self, method: str, params: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
        self.calls.append((method, params))
        if method == "model/list":
            return {"data": self.model_list}
        if method == "thread/start":
            return {"thread": {"id": f"th_{next(self.thread_seq)}"}}
        if method == "thread/fork":
            return {"thread": {"id": f"th_{next(self.thread_seq)}"}}
        if method == "thread/list":
            return {"data": [{"id": "th_1", "title": "示例任务", "status": "idle"}]}
        if method == "turn/start":
            return {"turn": {"id": f"turn_{next(self.turn_seq)}"}}
        return {}

    # --- helpers ---------------------------------------------------------
    def called(self, method: str) -> List[Dict[str, Any]]:
        return [params for name, params in self.calls if name == method]


class FakePlatform:
    name = "test"

    def __init__(self) -> None:
        self.secrets: Dict[str, str] = {}

    def secret_get(self, service: str, account: str) -> Optional[str]:
        return self.secrets.get(f"{service}/{account}")

    def secret_set(self, service: str, account: str, value: str) -> None:
        self.secrets[f"{service}/{account}"] = value

    def secret_delete(self, service: str, account: str) -> None:
        self.secrets.pop(f"{service}/{account}", None)

    def popen_kwargs(self) -> Dict[str, Any]:
        return {}

    def resolve_codex_path(self, configured: Optional[str]) -> str:
        return configured or "codex"

    def child_env(self, codex_home: Path) -> Dict[str, str]:
        return {}

    def fd_count(self, pid: int) -> Optional[int]:
        return 10
