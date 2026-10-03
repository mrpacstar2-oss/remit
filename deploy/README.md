# Self-hosting Remit

Everything here runs the website, the `llms.txt` index and the hosted MCP server for AI agents from one small Linux
server. Two options:

## Option A: HTTPS on a domain (ports 80 and 443 free)

Point a DNS A record for your domain at the server, then:

```bash
git clone https://github.com/mrpacstar2-oss/remit.git /opt/remit && cd /opt/remit/deploy
DOMAIN=yourdomain.example bash setup.sh
```

`setup.sh` installs Docker if needed and starts two containers: the Remit server and Caddy, which obtains and renews
the HTTPS certificate. The site is at `https://yourdomain.example`, the MCP endpoint at `/mcp`, the demo at
`/try.html` and a health check at `/health`. No domain? A free address derived from the server's IP works too, for
example `DOMAIN=203-0-113-7.sslip.io` for the IP `203.0.113.7`.

## Option B: one port, plain HTTP (when 80/443 are taken)

```bash
cd /opt/remit/deploy && PORT=8787 bash run-simple.sh
```

Serves the website and `/mcp` from a single container on that port.

## Keeping it current

```bash
SITE_URL=https://yourdomain.example bash deploy/enable-auto-update.sh
```

Records the server's public address, rebuilds the site files with it, and checks GitHub every 10 minutes
(`update.sh`), rebuilding the server only when code changed.

## API keys and usage

The server allows a number of anonymous requests per month per client address (`REMIT_FREE_MONTHLY`, default
2000). To issue a key with its own monthly limit:

```bash
docker compose exec mcp python /app/deploy/new_key.py <name> <monthly-limit>   # then restart the mcp service
```

Usage per key is kept in `usage.sqlite` on the `remit-data` volume.

## Files

| File | Purpose |
|---|---|
| `Dockerfile`, `docker-compose.yml`, `Caddyfile` | the HTTPS deployment (option A) |
| `run-simple.sh` | the single-port deployment (option B) |
| `setup.sh`, `update.sh`, `enable-auto-update.sh` | install and keep updated |
| `new_key.py` | create API keys |
| `server.json` | the MCP Registry listing; published by `.github/workflows/registry.yml` |
