#!/usr/bin/env bash
# Local development server (SQLite, no Docker). Vendor tools are found automatically in
# ~/.local/share/molecular-tools and ~/.config/biosolveit (see scripts/install_tools.sh).
# Optional overrides: environment variables, or a git-ignored .env.local with KEY=VALUE lines
# (variables already set in your shell win; PORT=8010 ./scripts/dev.sh works).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .env.local ]; then
  while IFS='=' read -r key value; do
    case "$key" in ''|'#'*) continue;; esac
    [ -z "${!key+x}" ] && export "$key=$value"
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env.local || true)
fi
export APP_ENV="${APP_ENV:-development}"
export DATA_DIR="${DATA_DIR:-$HOME/mdp-data}"
exec .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port "${PORT:-8008}" "$@"
