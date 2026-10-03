#!/usr/bin/env bash
# Pull the latest Remit from GitHub and redeploy if anything changed. Safe to run repeatedly (cron does).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[ -f deploy/site.env ] && { set -a; . deploy/site.env; set +a; }
# Generated website files are rebuilt below; drop local copies so the pull never conflicts.
git checkout -q -- site
before="$(git rev-parse HEAD)"
git pull --ff-only -q
after="$(git rev-parse HEAD)"
if [ "$before" = "$after" ] && [ "${1:-}" != "--force" ]; then exit 0; fi
echo "$(date -u +%FT%TZ) updating $before -> $after"
# Website files (AI index, docs, demo) are rebuilt with this server's addresses.
python3 site/build.py
# Rebuild the server only when code or deployment files changed.
if [ "${1:-}" = "--force" ] || ! git diff --quiet "$before" "$after" -- src pyproject.toml deploy; then
  if docker compose -f deploy/docker-compose.yml ps -q mcp 2>/dev/null | grep -q .; then
    (cd deploy && docker compose up -d --build)
  elif docker ps -a --format '{{.Names}}' | grep -qx remit; then
    PORT="${PORT:-$(docker port remit 8000/tcp | head -1 | sed 's/.*://')}" bash deploy/run-simple.sh
  else
    echo "no Remit container found; website files updated only"
  fi
fi
echo "$(date -u +%FT%TZ) done"
