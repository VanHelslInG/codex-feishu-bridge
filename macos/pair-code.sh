#!/usr/bin/env bash
#
# Print the current pairing code.
#
# Safe to run whether or not the bridge is up: the code is created on demand
# and stored in SQLite, so the running bridge and this script always agree.
# Pairing rotates the code immediately, so each code is single use.
#
# NOT YET VERIFIED ON A REAL MAC.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/macos/lib/appdir.sh"

VENV_PYTHON="$(bridge_venv_python "$ROOT")"
if [ ! -x "$VENV_PYTHON" ]; then
    echo 'Virtualenv missing. Run macos/install.sh first.' >&2
    exit 1
fi

APP_DIR="$(bridge_app_dir)"
code="$(PYTHONPATH="$ROOT/src" CODEX_FEISHU_BRIDGE_HOME="$APP_DIR" \
    "$VENV_PYTHON" -m feishu_bridge.bridge pair-code)"

echo
echo "配对码：$code"
echo
echo '在飞书里发送：'
echo "  /bind $code"
echo
echo '应用已开通「获取群组中所有消息」时群里直接发即可；'
echo '没开通该权限时飞书只会把 @机器人 的消息推给 Bridge，那就发：'
echo "  @机器人 /bind $code"
echo
echo '配对成功后这个码会立刻失效。'
