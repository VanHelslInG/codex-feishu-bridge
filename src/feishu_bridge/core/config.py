"""Bridge configuration: defaults, loading and validation.

The config file lives next to the SQLite state file in the application
directory. Secrets never live here; only the credential *locator* does.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_HEALTH_PORT = 49660

CONFIG_DEFAULTS: Dict[str, Any] = {
    # Absolute path or bare command name resolved from PATH.
    "codex_path": None,
    "codex_home": None,
    "instance": "default",
    "health_host": "127.0.0.1",
    "health_port": DEFAULT_HEALTH_PORT,
    "approval_policy": "never",
    "sandbox_policy": "dangerFullAccess",
    # Empty means "ask the user with a picker on the first task".
    "default_model": None,
    "default_effort": None,
    "credential_service": "codex-feishu-bridge",
    "credential_accounts": {
        "app_id": "feishu-app-id",
        "app_secret": "feishu-app-secret",
    },
    "artifact_roots": [],
    "max_image_bytes": 20 * 1024 * 1024,
    "max_file_bytes": 50 * 1024 * 1024,
    "app_server_watchdog": {
        "check_interval_seconds": 60,
        "recycle_fd_threshold": 180,
        "max_age_seconds": 86400,
    },
    "quick_start": {"project_keywords": {}},
    "projects": {},
    # Sidebar section that holds Feishu tasks in the Codex desktop app. Null
    # leaves threads where the app would otherwise file them (by working
    # directory), which mixes them into unrelated projects.
    "thread_section": "飞书",
    "feishu": {
        "domain": "https://open.feishu.cn",
        # False means "receive every message in the topic group", which
        # requires the sensitive im:message.group_msg scope. When the scope is
        # unavailable set this to True and every message must mention the bot.
        "require_mention_in_group": False,
        "allowlist": {"chat_ids": [], "open_ids": []},
    },
}


def app_dir() -> Path:
    """Return the application directory, honouring the env override."""
    override = os.environ.get("CODEX_FEISHU_BRIDGE_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return _windows_parent() / "CodexFeishuBridge"
    return Path.home() / "Library" / "Application Support" / "CodexFeishuBridge"


def _windows_parent() -> Path:
    """Directory the app dir hangs off on Windows.

    State, logs and downloaded images stay off the system drive: when a D:
    drive exists the app directory lives in ``D:\\Codex``, otherwise it falls
    back to ``%LOCALAPPDATA%``.
    """
    if Path("D:/").exists():
        return Path("D:/Codex")
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def merge_defaults(raw: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(CONFIG_DEFAULTS)
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    config_path = path or (app_dir() / "config.json")
    if not config_path.exists():
        return merge_defaults({})
    with config_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a JSON object")
    return merge_defaults(raw)


def codex_home(config: Dict[str, Any]) -> Path:
    configured = config.get("codex_home")
    if configured:
        return Path(str(configured)).expanduser()
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
