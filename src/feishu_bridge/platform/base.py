"""Platform adapter contract.

Everything that differs between Windows and macOS lives behind this interface
so the core and the IM layer stay portable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Optional


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
        if configured:
            candidate = Path(str(configured)).expanduser()
            if candidate.exists():
                return str(candidate)
            return str(configured)
        if self.name == "windows":
            return "codex.exe"
        return "codex"

    def fd_count(self, pid: int) -> Optional[int]:
        """Best-effort count of open descriptors for the app-server."""
        return None

    def child_env(self, codex_home: Path) -> Dict[str, str]:
        import os

        return {**os.environ, "CODEX_HOME": str(codex_home)}


def current_platform() -> Platform:
    import os

    if os.name == "nt":
        from .windows import WindowsPlatform

        return WindowsPlatform()
    from .macos import MacOSPlatform

    return MacOSPlatform()
