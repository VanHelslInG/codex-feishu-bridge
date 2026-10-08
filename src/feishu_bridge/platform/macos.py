"""macOS host adapter: Keychain secrets, process-group control, CLI lookup."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import CodexNotFound, Platform


class MacOSPlatform(Platform):
    name = "macos"

    def secret_get(self, service: str, account: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, OSError):
            # No Keychain helper on this host (a non-macOS dev box, say). An
            # absent secret is the honest answer here: the caller reports
            # "credentials are missing" instead of dying on a spawn error.
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def secret_set(self, service: str, account: str, value: str) -> None:
        try:
            subprocess.run(
                [
                    "/usr/bin/security", "add-generic-password", "-s", service,
                    "-a", account, "-w", value, "-U",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise OSError(
                "cannot store the credential: /usr/bin/security is not available "
                "on this host, so there is no Keychain to write to."
            ) from exc

    def secret_delete(self, service: str, account: str) -> None:
        try:
            subprocess.run(
                ["/usr/bin/security", "delete-generic-password", "-s", service, "-a", account],
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, OSError):
            return

    def popen_kwargs(self) -> Dict[str, Any]:
        return {"start_new_session": True}

    def terminate_tree(self, proc: subprocess.Popen, timeout: float = 10.0) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # --- environment ----------------------------------------------------
    def resolve_codex_path(self, configured: Optional[str]) -> str:
        """Locate the Codex CLI, or explain why it could not be found.

        Same strategy order as Windows: an explicit config wins, then PATH,
        then the CLI bundled inside the Codex desktop app, then the desktop
        client's own recorded path.
        """
        if configured:
            candidate = Path(str(configured)).expanduser()
            if candidate.exists():
                return str(candidate)
            raise CodexNotFound(
                f"config.json sets codex_path = {configured!r}, but that file does "
                "not exist. Correct the path or remove the key to auto-detect."
            )

        found = shutil.which("codex")
        if found:
            return found
        searched: List[str] = ["PATH (codex)"]

        found = self._app_bundle_codex()
        if found:
            return found
        searched.append(
            "the Codex desktop app bundle ("
            + ", ".join(str(path) for path in self._app_bundle_candidates())
            + ")"
        )

        found = self._config_toml_codex()
        if found:
            return found
        searched.append(f"{self._codex_config_toml()} (key CODEX_CLI_PATH)")

        raise CodexNotFound(
            "could not locate the Codex CLI, so the bridge cannot start its "
            "app-server. Searched: " + "; ".join(searched) + ". "
            "Set codex_path in config.json to the full path of the codex binary."
        )

    # --- Codex CLI discovery --------------------------------------------
    @staticmethod
    def _app_bundle_candidates() -> List[Path]:
        """CLI locations inside the Codex desktop app bundle.

        The desktop app ships the CLI inside a nested helper bundle. Its exact
        layout is not a documented contract, so the bundle names are
        enumerated instead of trusting a single hard-coded path.
        """
        apps = [
            Path("/Applications/ChatGPT.app"),
            Path("/Applications/Codex.app"),
            Path.home() / "Applications" / "ChatGPT.app",
            Path.home() / "Applications" / "Codex.app",
        ]
        relative = [
            Path("Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
            Path("Contents/Resources/codex-cli/codex"),
            Path("Contents/MacOS/codex"),
        ]
        return [app / suffix for app in apps for suffix in relative]

    def _app_bundle_codex(self) -> Optional[str]:
        for candidate in self._app_bundle_candidates():
            if candidate.is_file():
                return str(candidate)
        return None

    def _codex_config_toml(self) -> Path:
        home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        return Path(home) / "config.toml"

    def _config_toml_codex(self) -> Optional[str]:
        """Last resort: the desktop client's own recorded CLI path.

        Read-only and best-effort on purpose: that file belongs to the Codex
        desktop app, so neither its format nor its presence is guaranteed.
        """
        path = self._codex_config_toml()
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        match = re.search(
            r"""^\s*CODEX_CLI_PATH\s*=\s*['"]([^'"]+)['"]""", text, re.MULTILINE
        )
        if not match:
            return None
        candidate = Path(match.group(1)).expanduser()
        return str(candidate) if candidate.is_file() else None

    def fd_count(self, pid: int) -> Optional[int]:
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-a", "-p", str(pid), "-Ff"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return sum(1 for line in result.stdout.splitlines() if re.fullmatch(r"f\d+", line))
