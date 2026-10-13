#!/usr/bin/env bash
# Compatibility entry point; use scripts/deploy.sh for CPA and GPT-Load.
set -Eeuo pipefail
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/deploy.sh" "$@"
