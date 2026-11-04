# Deployment

Beispiel für einen Homeserver mit Docker, Reverse Proxy und Cloudflare-Tunnel. Platzhalter: `<server>` (SSH-Host), `<domain>` (eigene Domain).

transitwalter prüft selbst keine Tokens: Es erwartet den Nutzer im Header `X-Walter-User`, den ein Reverse Proxy nach dem Login setzt – bei Jörg der [Walterpass](https://github.com/joerg86/walterpass) (Caddy mit `forward_auth`, Login per Passkey über Pocket ID). Ohne Header antwortet `/mcp` mit 401 und dem Hinweis auf die Resource-Metadaten (`/.well-known/oauth-protected-resource`, liefert der Proxy), bei `X-Walter-Denied` mit 403. Der Container veröffentlicht deshalb keinen Port und hängt nur im Docker-Netz `walter` des Proxys; der Proxy muss einen von außen mitgeschickten `X-Walter-User` verwerfen.

## 1. Klonen und konfigurieren

```bash
ssh <server>
git clone https://github.com/joerg86/transitwalter.git /srv/docker/transitwalter
cd /srv/docker/transitwalter
cp .env.example .env && chmod 600 .env
nano .env
```

In `.env` eintragen:

- `TRANSITOUS_USER_AGENT` – App-Name, Version und eigener Kontakt (Pflicht laut [transitous.org/api](https://transitous.org/api/))
- `PUBLIC_BASE_URL` – z. B. `https://transit.<domain>` (für Karten- und GPX-Links)

## 2. Starten

```bash
docker compose up -d --build
docker compose ps                       # nach ~1 Minute: healthy
docker exec transitwalter python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read())"   # b'ok'
```

## 3. Von außen erreichbar machen

Der Reverse Proxy leitet `transit.<domain>` an `transitwalter:8000` weiter (im Netz `walter`), der Tunnel zeigt auf den Proxy. `/health` und die Karten/GPX unter `/x/<zufällige-id>` sind ohne Login erreichbar, damit sie sich in Komoot & Co. öffnen lassen.

## 4. Im Assistenten eintragen

MCP-URL `https://transit.<domain>/mcp`; die Anmeldung läuft per OAuth über den Identity Provider des Proxys (bei Claude: „Use Claude's published identity“).

## Updates

```bash
cd /srv/docker/transitwalter && git pull && docker compose up -d --build
```

## Testen von außen

```bash
uv run python tests/smoke.py   # gegen einen lokal gestarteten Server, setzt X-Walter-User selbst
```
