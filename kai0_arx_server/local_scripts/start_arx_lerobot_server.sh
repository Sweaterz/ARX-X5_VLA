#!/usr/bin/env bash
set -euo pipefail
KAI0_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${ARX_SERVER_PYTHON:-/home/qijun/ARX5_beta/.venv/bin/python}"
mkdir -p "$KAI0_ROOT/logs"
test -f "$KAI0_ROOT/CHECKPOINT_VERIFIED.json" || { echo 'Checkpoint transfer/verification is not complete.' >&2; exit 1; }
exec 9>"$KAI0_ROOT/logs/server.lock"
flock -n 9 || { echo 'This local server is already starting/running.' >&2; exit 1; }
export PYTHONPATH="$KAI0_ROOT:$KAI0_ROOT/vendor_deps${PYTHONPATH:+:${PYTHONPATH}}"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=false
export NO_PROXY="localhost,127.0.0.1,192.168.2.136${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="$NO_PROXY"
cd "$KAI0_ROOT"
exec "$PYTHON_BIN" -u "$KAI0_ROOT/local_scripts/serve_arx_lerobot.py" "$@"
