#!/usr/bin/env bash
# One-time: remember this server's public addresses and check GitHub for updates every 10 minutes.
# Usage (as root, from the repository folder):  SITE_URL=https://77-68-52-20.sslip.io bash deploy/enable-auto-update.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
: "${SITE_URL:?set SITE_URL, e.g. SITE_URL=https://77-68-52-20.sslip.io}"
HOSTNAME_ONLY="${SITE_URL#*://}"; HOSTNAME_ONLY="${HOSTNAME_ONLY%%/*}"
cat > "$ROOT/deploy/site.env" <<ENV
SITE_URL=$SITE_URL
SITE_NAME=Remit
MCP_URL=$SITE_URL/mcp
DOMAIN=${DOMAIN:-$HOSTNAME_ONLY}
ENV
chmod +x "$ROOT/deploy/update.sh"
echo "*/10 * * * * root $ROOT/deploy/update.sh >> /var/log/remit-update.log 2>&1" > /etc/cron.d/remit-update
"$ROOT/deploy/update.sh" --force
echo "Auto-update enabled. Log: /var/log/remit-update.log"
