# Deployment

Beispiel für einen Homeserver mit Docker und Cloudflare-Tunnel. Platzhalter: `<server>` (SSH-Host), `<domain>` (eigene Domain), Port `8766` auf dem Host.

## 1. Klonen und konfigurieren

```bash
ssh <server>
git clone https://github.com/joerg86/transitwalter.git /srv/docker/transitwalter
cd /srv/docker/transitwalter
cp .env.example .env && chmod 600 .env
nano .env
```

In `.env` eintragen:

- `MCP_TOKEN` – langes Zufalls-Token, z. B. `openssl rand -hex 32`
- `TRANSITOUS_USER_AGENT` – App-Name, Version und eigener Kontakt (Pflicht laut [transitous.org/api](https://transitous.org/api/))
- `PUBLIC_BASE_URL` – z. B. `https://transitwalter.<domain>` (für Karten- und GPX-Links)

## 2. Starten

```bash
docker compose up -d --build
docker compose ps                       # nach ~1 Minute: healthy
curl -s http://localhost:8766/health    # ok
```

## 3. Von außen erreichbar machen

Cloudflare Zero Trust → Networks → Tunnels → eigener Tunnel → **Public Hostname hinzufügen**:

- Subdomain `transitwalter`, Domain `<domain>`
- Service: `HTTP` → `host.docker.internal:8766` (oder `localhost:8766`, je nachdem, wo `cloudflared` läuft)

Ein Reverse Proxy (Caddy, nginx) geht genauso. `/mcp` ist durch den Bearer-Token geschützt; `/health` und die Karten/GPX unter `/x/<zufällige-id>` sind ohne Token erreichbar, damit sie sich in Komoot & Co. öffnen lassen.

## 4. Im Assistenten eintragen

MCP-URL `https://transitwalter.<domain>/mcp`, Header `Authorization: Bearer <MCP_TOKEN>`.

## Updates

```bash
cd /srv/docker/transitwalter && git pull && docker compose up -d --build
```

## Testen von außen

```bash
MCP_TOKEN=… uv run python tests/smoke.py https://transitwalter.<domain>/mcp
```
