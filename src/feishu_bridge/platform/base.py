"""Platform adapter contract.

Everything that differs between Windows and macOS lives behind this interface
so the core and the IM layer stay portable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional


class CodexNotFound(RuntimeError):
    """No usable Codex executable could be located.

    Raised before any process is spawned so the operator sees which locations
    were searched instead of a bare ``FileNotFoundError [WinError 2]``.
    """


class Platform:
    name = "base"

    # --- secret storage -------------------------------------------------
    def secret_get(self, service: str, account: str) -> Optional[str]:
        raise NotImplementedError

    def secret_set(self, service: str, account: str, value: str) -> None:
        raise NotImplementedError

    def secret_delete(self, service: str, account: str) -> None:
        raise NotImplementedError

    # --- process control ------------------------------------------------
    def popen_kwargs(self) -> Dict[str, Any]:
        """Extra Popen kwargs that isolate the child process tree."""
        return {}

    def terminate_tree(self, proc: subprocess.Popen, timeout: float = 10.0) -> None:
        """Terminate the process and every descendant it spawned."""
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass

    # --- environment ----------------------------------------------------
    def resolve_codex_path(self, configured: Optional[str]) -> str:
        """Locate the Codex CLI, or explain why it could not be found."""
        if configured:
            candidate = Path(str(configured)).expanduser()
            if candidate.exists():
                return str(candidate)
            raise CodexNotFound(
                f"config.json sets codex_path = {configured!r}, but that file does "
                "not exist. Correct the path or remove the key to auto-detect."
            )
        found = shutil.which("codex.exe") or shutil.which("codex")
        if found:
            return found
        raise CodexNotFound(
            "could not find the Codex CLI. Searched PATH for 'codex.exe' and "
            "'codex'. Set codex_path in config.json to the full path."
        )

    def fd_count(self, pid: int) -> Optional[int]:
        """Best-effort count of open descriptors for the app-server."""
        return None

    def child_env(self, codex_home: Path) -> Dict[str, str]:
        import os

        return {**os.environ, "CODEX_HOME": str(codex_home)}


def current_platform() -> Platform:
    import os

    if os.name != "nt":
        raise NotImplementedError(
            "This build ships the Windows adapter only. Run it on Windows, or add "
            "an adapter under src/feishu_bridge/platform/ and wire it up here."
        )
    from .windows import WindowsPlatform

    return WindowsPlatform()
