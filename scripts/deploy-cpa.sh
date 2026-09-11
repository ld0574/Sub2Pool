#!/usr/bin/env bash
# Debian: bash scripts/deploy-cpa.sh [Docker Compose global options]
set -Eeuo pipefail
if ! command -v python3 >/dev/null; then
  printf '需要 Python 3。Debian 可执行 apt-get install python3 后重试。\n' >&2
  exit 1
fi
exec python3 "$(dirname -- "${BASH_SOURCE[0]}")/deploy_cpa.py" "$@"
