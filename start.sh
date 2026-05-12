#!/bin/bash
set -e

BRIDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BRIDGE_DIR"

echo "[bridge] Activating venv..."
source .venv/bin/activate

echo "[bridge] Checking OpenCode server..."
if ! curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4096/global/health 2>/dev/null | grep -q "200"; then
    echo "[bridge] ERROR: OpenCode server not running at http://127.0.0.1:4096"
    echo "[bridge] Start it with: opencode serve --port 4096"
    exit 1
fi
echo "[bridge] OpenCode server OK"

echo "[bridge] Starting Telegram bridge..."
exec python -m bot