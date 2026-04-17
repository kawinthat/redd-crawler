#!/bin/bash
# ─────────────────────────────────────────────────────────
# RE:DD — Push to GitHub (รัน 1 ครั้ง หลัง approve GitHub email)
# ─────────────────────────────────────────────────────────
# 1. ไปที่ Terminal แล้วรัน:  bash scripts/push_to_github.sh
# ─────────────────────────────────────────────────────────

set -e
REPO_URL="https://github.com/kawinthat/redd-crawler.git"
BRANCH="main"

echo "🚀 Pushing redd-crawler to GitHub..."
cd "$(dirname "$0")/.."

# Remove stale git lock if exists
rm -f .git/index.lock

# Stage + commit any new files
git add -A
git diff --cached --quiet || git commit -m "feat: Phase 6 — deploy guide + Make.com scheduler"

# Set remote
git remote get-url origin 2>/dev/null || git remote add origin "$REPO_URL"
git remote set-url origin "$REPO_URL"

# Push (GitHub will ask for username + Personal Access Token as password)
echo ""
echo "⚠️  GitHub จะขอ login:"
echo "   Username: kawinthat"
echo "   Password: Personal Access Token (สร้างที่ https://github.com/settings/tokens/new)"
echo ""
git push -u origin "$BRANCH" && \
  echo "✅ Pushed to https://github.com/kawinthat/redd-crawler" || \
  echo "❌ Push failed — ต้องตั้ง GitHub credentials ก่อน"

echo ""
echo "Next step: ไปที่ https://render.com/new → New Web Service"
echo "  Connect GitHub → kawinthat/redd-crawler"
echo "  Environment: Docker"
echo "  Region: Singapore"
echo "  Add env vars: OPENROUTER_API_KEY, SUPABASE_URL, SUPABASE_KEY, LINE_NOTIFY_TOKEN"
