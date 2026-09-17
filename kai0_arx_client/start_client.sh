#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${CLIENT_PYTHON:-${BASE_DIR}/.venv-inference/bin/python}" "${BASE_DIR}/client.py" "$@"
