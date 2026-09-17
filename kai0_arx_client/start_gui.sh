#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec .venv-inference/bin/python gui_server.py "$@"
