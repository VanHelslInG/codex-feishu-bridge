#!/usr/bin/env bash
#
# macOS installer for the Codex Feishu Bridge.
#
# Prepares the virtualenv, the dependencies, the application directory and the
# Feishu credentials. Registering the LaunchAgent is a separate, explicit step
# (macos/install-agent.sh): that one changes launchd state and is meant to be a
# deliberate choice.
#
# NOT YET VERIFIED ON A REAL MAC. Validate on the target Mac before relying on
# this script or on the LaunchAgent template.
#
# Usage:
#   macos/install.sh [--prompt] [--app-id ID --app-secret SECRET]
#                    [--codex-path PATH] [--health-port PORT]
#
# Credentials may also come from the FEISHU_APP_ID / FEISHU_APP_SECRET
# environment variables, which keeps them out of the shell history.
set -euo pipefail

usage() {
    echo "Usage: macos/install.sh [--prompt] [--app-id ID] [--app-secret SECRET] [--codex-path PATH] [--health-port PORT]"
}

PROMPT=false
APP_ID="${FEISHU_APP_ID:-}"
APP_SECRET="${FEISHU_APP_SECRET:-}"
CODEX_PATH=""
HEALTH_PORT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --prompt) PROMPT=true; shift ;;
        --app-id) APP_ID="${2:-}"; shift 2 ;;
        --app-secret) APP_SECRET="${2:-}"; shift 2 ;;
        --codex-path) CODEX_PATH="${2:-}"; shift 2 ;;
        --health-port) HEALTH_PORT="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/macos/lib/appdir.sh"

APP_DIR="$(bridge_app_dir)"
VENV="$ROOT/.venv"
VENV_PYTHON="$(bridge_venv_python "$ROOT")"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"

# /usr/bin/python3 only exists once the Xcode command line tools are installed;
# without this check the failure would surface as a bare "command not found".
if [ ! -x "$PYTHON_BIN" ]; then
    echo "No Python 3 at $PYTHON_BIN." >&2
    echo 'Install the Xcode command line tools (xcode-select --install) or pass PYTHON_BIN=/path/to/python3.' >&2
    exit 1
fi

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Creating virtualenv at $VENV"
    "$PYTHON_BIN" -m venv "$VENV"
fi

echo 'Installing dependencies'
"$VENV_PYTHON" -m pip install --quiet --upgrade pip
"$VENV_PYTHON" -m pip install --quiet -r "$ROOT/requirements.txt"

mkdir -p "$APP_DIR"
chmod 700 "$APP_DIR"
CONFIG="$APP_DIR/config.json"

if [ ! -f "$CONFIG" ]; then
    echo "Writing $CONFIG from config.example.json"
    # config.example.json carries a Windows project path; writing it verbatim
    # on a Mac would leave projects.codex pointing at a directory that cannot
    # exist here, so the placeholder is replaced with a Mac-appropriate one.
    BRIDGE_SOURCE_CONFIG="$ROOT/config.example.json" BRIDGE_CONFIG="$CONFIG" \
        BRIDGE_HEALTH_PORT="$HEALTH_PORT" BRIDGE_CODEX_PATH="$CODEX_PATH" \
        BRIDGE_DEFAULT_PROJECT="$HOME/Documents/Codex" \
        "$VENV_PYTHON" - <<'PY'
import json
import os

config = json.load(open(os.environ["BRIDGE_SOURCE_CONFIG"], encoding="utf-8"))
config["projects"] = {"codex": os.environ["BRIDGE_DEFAULT_PROJECT"]}
if os.environ.get("BRIDGE_HEALTH_PORT"):
    config["health_port"] = int(os.environ["BRIDGE_HEALTH_PORT"])
if os.environ.get("BRIDGE_CODEX_PATH"):
    config["codex_path"] = os.environ["BRIDGE_CODEX_PATH"]
with open(os.environ["BRIDGE_CONFIG"], "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
    chmod 600 "$CONFIG"
    echo "Edit projects.codex in $CONFIG before starting: it ships pointing at $HOME/Documents/Codex"
else
    echo "Keeping the existing $CONFIG"
    if [ -n "$HEALTH_PORT" ] || [ -n "$CODEX_PATH" ]; then
        BRIDGE_CONFIG="$CONFIG" BRIDGE_HEALTH_PORT="$HEALTH_PORT" BRIDGE_CODEX_PATH="$CODEX_PATH" \
            "$VENV_PYTHON" - <<'PY'
import json
import os

path = os.environ["BRIDGE_CONFIG"]
config = json.load(open(path, encoding="utf-8"))
if os.environ.get("BRIDGE_HEALTH_PORT"):
    config["health_port"] = int(os.environ["BRIDGE_HEALTH_PORT"])
if os.environ.get("BRIDGE_CODEX_PATH"):
    config["codex_path"] = os.environ["BRIDGE_CODEX_PATH"]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
    fi
fi

if [ "$PROMPT" = true ] && [ -z "$APP_ID" ]; then
    printf 'Feishu App ID: ' >&2
    read -r APP_ID
    printf 'Feishu App Secret: ' >&2
    read -rs APP_SECRET
    printf '\n' >&2
fi

if [ -n "$APP_ID" ] && [ -n "$APP_SECRET" ]; then
    echo 'Storing credentials in the login Keychain'
    # Read the locator from the config instead of hard-coding it, so renaming
    # credential_service cannot silently desync this script from the bridge.
    LOCATOR="$(BRIDGE_CONFIG="$CONFIG" "$VENV_PYTHON" - <<'PY'
import json
import os

config = json.load(open(os.environ["BRIDGE_CONFIG"], encoding="utf-8"))
accounts = config["credential_accounts"]
print(config["credential_service"], accounts["app_id"], accounts["app_secret"])
PY
)"
    SERVICE="${LOCATOR%% *}"
    REST="${LOCATOR#* }"
    ACCOUNT_ID="${REST%% *}"
    ACCOUNT_SECRET="${REST#* }"
    /usr/bin/security add-generic-password -U -s "$SERVICE" -a "$ACCOUNT_ID" -w "$APP_ID"
    /usr/bin/security add-generic-password -U -s "$SERVICE" -a "$ACCOUNT_SECRET" -w "$APP_SECRET"
    unset APP_SECRET
fi

echo
echo "Installed. Config: $CONFIG"
echo "Next: $ROOT/macos/start.sh   (then send /bind <pair-code> in Feishu)"
echo "Pairing code: $ROOT/macos/pair-code.sh"
echo "Autostart while Codex is open: $ROOT/macos/install-agent.sh"
