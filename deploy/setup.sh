#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu/Debian VPS. Run as root:  DOMAIN=yourdomain.com bash setup.sh
# Before running: point your domain's DNS "A" record at this server's IP address.
set -euo pipefail
: "${DOMAIN:?set DOMAIN, e.g. DOMAIN=example.com bash setup.sh}"
REPO="${REPO:-https://github.com/mrpacstar2-oss/remit.git}"   # set to the public repository once it exists
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
if [ ! -d /opt/remit ]; then
  git clone "$REPO" /opt/remit
fi
cd /opt/remit/deploy 2>/dev/null || cd /opt/remit/remit/deploy
DOMAIN="$DOMAIN" docker compose up -d --build
echo "Done. Website: https://$DOMAIN   AI tool server: https://$DOMAIN/mcp   Health: https://$DOMAIN/health"
