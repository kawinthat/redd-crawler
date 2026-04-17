#!/usr/bin/env bash
# RE:DD Crawler — Run a full scan
# Usage: ./scripts/run_scan.sh [--dry-run] [--url URL]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Load .env if present
if [ -f "$ROOT_DIR/.env" ]; then
  export $(grep -v '^#' "$ROOT_DIR/.env" | xargs)
fi

DRY_RUN=""
URL="${TARGET_URL:-https://led.go.th/assets}"

for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN="--dry-run" ;;
    --url=*) URL="${arg#*=}" ;;
  esac
done

echo "🚀 RE:DD Crawler starting..."
echo "   Target: $URL"
[ -n "$DRY_RUN" ] && echo "   Mode: DRY RUN (no DB writes)"

cd "$ROOT_DIR"
python -m crawler.orchestrator $DRY_RUN --url "$URL"
