# transitwalter

Selbst gehosteter MCP-Server für ÖPNV-Verbindungen weltweit – vom Flughafen zum Trailhead und zurück.

Daten kommen von [Transitous](https://transitous.org) (öffentliche MOTIS-API, offene Fahrplandaten aus vielen Ländern + OpenStreetMap). Keine Preise, keine Flüge – dafür gibt es eigene MCPs. Läuft als Docker-Container mit Streamable-HTTP und Bearer-Token, z. B. auf einem Homeserver hinter einem Cloudflare-Tunnel.

## Tools

| Tool | |
|---|---|
| `transit_journey_plan` | Verbindungen von A nach B inkl. Fußwegen, Abfahrt oder späteste Ankunft (`arrive_by`), Verkehrsmittel-Auswahl, Zwischenhalt (`via`), max. Fußweg, Gehtempo |
| `transit_departures` | Abfahrten/Ankünfte an einer Haltestelle – „wann fährt der letzte Bus vom Trail?“ |
| `transit_walk_route` | reiner Fußweg mit Dauer, Strecke, Straßen + Karte und GPX |
| `transit_export` | Fußwege (oder alles) einer geplanten Verbindung als Karte und GPX – jeder Fußweg auch als eigene GPX-Datei zum Navigieren |
| `transit_gpx` | Inhalt einer GPX-Datei direkt über MCP, falls der Assistent damit weiterarbeiten will |
| `transit_place_search` | Orte, Adressen, Haltestellen, Gipfel, Kaps … (OSM), mit `ref` für die anderen Tools |
| `transit_airport` | Flughafen per IATA-Code mit den Haltestellen am Terminal |

### Orte

Überall, wo ein Ort erwartet wird, geht:

- freier Text: `Osnabrück Hbf`, `Purekkari neem`, `Ivinghoe Beacon`
- Koordinaten: `59.67184,25.69492`
- IATA-Code in Großbuchstaben: `NCE`, `DTM`, `TLL` – startet/endet an der **Haltestelle am Terminal**, nicht am Mittelpunkt des Flughafengeländes (von dort findet MOTIS oft keinen Fußweg, z. B. bei Nizza)
- eine `ref` aus `transit_place_search` / `transit_airport` (`stop:…`)

Zeiten sind Ortszeit des Start- (bzw. bei `arrive_by` des Ziel-)Orts, z. B. `2026-11-07T10:00`. Ausgaben stehen in der Ortszeit der jeweiligen Haltestelle.

### Verkehrsmittel

`transport`: `all` (Standard), `regional` (Nahverkehr ohne IC/ICE und Fernbusse – passend für Schwerbehindertenausweis oder Deutschlandticket), `rail`, `no_rail`. Sonderbusse mit eigenem Tarif (z. B. Flughafen-Expressbusse) erkennt der Filter nicht.

### Fußwege

Standard ist ein zügiges Tempo (`WALK_SPEED_KMH=5.5`) und bis zu 30 min Fußweg zur ersten/von der letzten Haltestelle (`MAX_WALK_MINUTES`), damit lieber gelaufen als umgestiegen wird – z. B. Holzwickede Bahnhof → Dortmund Airport (18 min) statt AirportExpress. Beides pro Aufruf überschreibbar.

Karten und GPX liegen unter `/x/<zufällige-id>` (ohne Token erreichbar, 90 Tage): die Karte unter `/x/<id>`, alles als GPX unter `/x/<id>.gpx`, jeder Fußweg einzeln unter `/x/<id>.w1.gpx`, `.w2.gpx` … (mit Start und Ziel als Wegpunkten und sprechendem Dateinamen). Die GPX-Datei lässt sich in Komoot (Planen → GPX importieren), OsmAnd, Outdooractive usw. öffnen.

### Warnungen

- **Fahrplan noch nicht veröffentlicht:** Liegt das Datum ≥ 5 Tage in der Zukunft, wird zusätzlich derselbe Wochentag in den nächsten Tagen abgefragt. Ist das Ergebnis dort deutlich besser, steht ein `timetable_hint` in der Antwort. Beispiel Estland: Elron-Züge sind nur wenige Tage im Voraus drin, sonst kommen 3–7 h Busfahrt statt 1 h Zug.
- **Pro Verbindung** (`warnings`): lange Wartezeiten, Übernachtungen, lange Fußwege, ausgefallene Fahrten, widersprüchliche Daten (Anschluss fährt vor der Ankunft ab).

## Fehlercodes

Tool-Fehler haben die Form `CODE: Meldung`, zusätzlich strukturiert als `{"error": {"code": …, "message": …}}`:
`INVALID_ARGUMENT`, `PLACE_NOT_FOUND`, `AIRPORT_NOT_FOUND`, `EXPORT_NOT_FOUND`, `RATE_LIMITED`, `CONFIG_ERROR`, `UPSTREAM_ERROR`.

## Transitous-Nutzungsregeln

Die öffentliche API ist für nicht-kommerzielle Projekte mit wenig Last gedacht ([transitous.org/api](https://transitous.org/api/)): User-Agent mit Kontakt (`TRANSITOUS_USER_AGENT`), Ergebnisse cachen, ab ~720 Routenabfragen am Tag vorher Bescheid sagen, Quellen nennen (steht als `attribution` in jeder Antwort). transitwalter cacht Antworten (Orte 1 Tag, Verbindungen 10 min) und begrenzt sich auf 20 Anfragen pro Minute. Wer transitwalter in einem kommerziellen Zusammenhang nutzen will, sollte das vorher mit den Betreibern klären.

## Lokal entwickeln

```bash
uv sync
cp .env.example .env               # MCP_TOKEN, TRANSITOUS_USER_AGENT eintragen
uv run python tests/unit.py        # Logik offline
uv run --env-file .env -- env PUBLIC_BASE_URL=http://localhost:8000 DATA_DIR=data transitwalter
uv run --env-file .env python tests/smoke.py   # zweites Terminal
```

## Flughafendaten

`src/transitwalter/data/airports.csv` (≈4100 Flughäfen mit Linienverkehr) wird mit `tools/build_airports.py` aus [OurAirports](https://ourairports.com/data/) (gemeinfrei) erzeugt. Die Haltestellen am Terminal sucht transitwalter zur Laufzeit über Transitous.

## Deployment

Siehe [DEPLOY.md](DEPLOY.md).
