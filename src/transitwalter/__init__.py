"""transitwalter – selbst gehosteter ÖPNV-MCP.

Verbindungen, Abfahrten und Fußwege weltweit über die öffentliche MOTIS-API von
Transitous (offene Fahrplandaten + OpenStreetMap). Flughäfen per IATA-Code mit
Haltestelle am Terminal, Warnungen bei unplausiblen Verbindungen und fehlenden
Fahrplandaten, Fußwege als GPX/Karte. Für Deutschland zusätzlich Bahnpreise über die
(inoffizielle) Schnittstelle des DB Navigators.
"""

import functools
import hmac
import logging
import os
import secrets
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Literal

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from . import db as D
from . import errors as E
from . import itinerary as I
from .errors import TransitError
from .export import Exports
from .places import _COORDS, _IATA, Places, from_geocode
from .transitous import ATTRIBUTION, Transitous

logging.getLogger("httpx").setLevel(logging.WARNING)

api = Transitous(
    os.environ.get("TRANSITOUS_BASE_URL", "https://api.transitous.org"),
    os.environ.get("TRANSITOUS_USER_AGENT", ""),
    per_minute=int(os.environ.get("TRANSITOUS_REQUESTS_PER_MINUTE", "20")),
)
places = Places(api)
dbapi = D.DB(os.environ.get("DB_USER_AGENT", "transitwalter/0.1"),
             per_minute=int(os.environ.get("DB_REQUESTS_PER_MINUTE", "10")))
# Persönliche Standards (überschreibbar pro Aufruf): zügiger Fußgänger, lieber laufen als umsteigen
WALK_SPEED_KMH = float(os.environ.get("WALK_SPEED_KMH", "5.5"))
MAX_WALK_MINUTES = int(os.environ.get("MAX_WALK_MINUTES", "30"))
exports = Exports(os.environ.get("DATA_DIR", "data"), os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"))

# Zuletzt geplante Verbindungen (Rohdaten) für transit_export
_itineraries: OrderedDict[str, tuple[str, dict]] = OrderedDict()

mcp = MCPServer(
    name="transitwalter",
    instructions=(
        "ÖPNV-Werkzeuge für Reiseplanung weltweit (Bahn, Bus, Tram, Fähre, Fußwege) über Transitous. "
        "Orte (origin, destination, stop, via) akzeptieren freien Text, 'lat,lon', einen IATA-Code in "
        "Großbuchstaben (z. B. 'NCE' – startet an der Haltestelle am Terminal) oder eine 'ref' aus "
        "transit_place_search/transit_airport. Zeiten sind Ortszeit des Start- bzw. Zielorts "
        "(z. B. '2026-11-07T10:00'), Ausgaben ebenfalls in Ortszeit der jeweiligen Haltestelle. "
        "Wer in Deutschland mit Schwerbehindertenausweis, Deutschlandticket o. Ä. fährt, nutzt "
        "transport='regional' (ohne IC/ICE und Fernbusse; Sonderbusse mit eigenem Tarif wie Flughafen-Expressbusse "
        "erkennt der Filter nicht). Für Wanderungen: max_walk_minutes erhöhen, wenn der Start/das Ziel abseits liegt; "
        "transit_departures zeigt, wann der letzte Bus fährt; transit_walk_route und transit_export "
        "liefern Fußwege als Karte und GPX (z. B. für Komoot) – unter 'walks' jeder Fußweg als eigene "
        "GPX-Datei zum Navigieren; diese Links dem Nutzer einzeln nennen. Antworten mit 'warnings' bzw. "
        "'timetable_hint' unbedingt an den Nutzer weitergeben – Fahrpläne liegen je nach Land oft nur "
        "Tage oder wenige Wochen im Voraus vor. Flüge gibt es hier nicht; Preise nur für die Deutsche Bahn "
        "über transit_db_prices (eigene DB-Suche, nur Deutschland bzw. DB-Tarif). "
        f"Fehler beginnen mit einem festen Code, z. B. 'PLACE_NOT_FOUND: …'. Mögliche Codes: {', '.join(E.ALL)}."
    ),
)


def tool(fn):
    """@mcp.tool() plus feste Fehlercodes – auch für unerwartete Fehler."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except TransitError as e:
            error = e
        except Exception as e:
            logging.exception("Unerwarteter Fehler in %s", fn.__name__)
            error = TransitError(E.UPSTREAM_ERROR, f"Unerwartete Antwort vom Datendienst ({type(e).__name__})")
        return CallToolResult(
            content=[TextContent(type="text", text=str(error))],
            structured_content={"error": {"code": error.code, "message": error.message}},
            is_error=True,
        )

    return mcp.tool()(wrapper)


def _remember(title: str, it: dict) -> str:
    iid = secrets.token_hex(4)
    _itineraries[iid] = (title, it)
    while len(_itineraries) > 300:
        _itineraries.popitem(last=False)
    return iid


def _name_ends(it: dict, start: str, end: str) -> None:
    """MOTIS nennt Start und Ziel 'START'/'END' – durch die aufgelösten Namen ersetzen."""
    for leg in it["legs"]:
        for side, name in (("from", start), ("to", end)):
            if leg[side].get("name") in ("START", "END", "", None):
                leg[side]["name"] = name


def _walk_seconds(minutes: int | None) -> int:
    minutes = MAX_WALK_MINUTES if minutes is None else minutes
    if not 0 <= minutes <= 120:
        raise TransitError(E.INVALID_ARGUMENT, "max_walk_minutes muss zwischen 0 und 120 liegen")
    return minutes * 60


# ---------- Orte ----------

@tool
async def transit_place_search(
    query: str,
    type: Literal["any", "stop", "address", "place"] = "any",
    near: str | None = None,
    limit: int = 8,
) -> dict:
    """Orte, Adressen, Haltestellen und Sehenswürdigkeiten (auch Trailheads, Pässe, Hütten) suchen.

    near: optional 'lat,lon', um Treffer in der Nähe zu bevorzugen.
    Jeder Treffer hat 'ref' – direkt als origin/destination/stop verwendbar.
    """
    bias = None
    if near:
        p = await places.resolve(near)
        bias = (p.lat, p.lon)
    kind = {"any": None, "stop": "STOP", "address": "ADDRESS", "place": "PLACE"}[type]
    found = await api.geocode(query, type=kind, near=bias)
    items = []
    for item in found[: max(1, min(limit, 20))]:
        p = from_geocode(item)
        items.append({**p.public(), "ref": f"stop:{p.ref}" if p.kind == "stop" else p.ref})
    if not items:
        raise TransitError(E.PLACE_NOT_FOUND, f"Nichts gefunden für: {query}")
    return {"items": items}


@tool
async def transit_airport(code: str) -> dict:
    """Flughafen per IATA-Code (z. B. NCE, LTN, TLL): Name, Lage und die ÖPNV-Haltestellen am Terminal.

    In anderen Tools reicht der Code selbst als Ort – dort wird automatisch die beste Haltestelle genommen.
    """
    info = await places.airport(code)
    return {**info, "place": info["place"].public()}


# ---------- Verbindungen ----------

def _speed(kmh: float | None) -> float:
    kmh = WALK_SPEED_KMH if kmh is None else kmh
    if not 2 <= kmh <= 8:
        raise TransitError(E.INVALID_ARGUMENT, "walk_speed_kmh muss zwischen 2 und 8 liegen")
    return round(kmh / 3.6, 2)


async def _plan(origin, destination, when: datetime, arrive_by, transport, walk_s, speed, via, via_stay,
                max_transfers, results) -> dict:
    return await api.plan(
        fromPlace=origin.ref, toPlace=destination.ref,
        time=when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        arriveBy=arrive_by, transitModes=I.TRANSPORT[transport], numItineraries=results,
        maxPreTransitTime=walk_s, maxPostTransitTime=walk_s, maxDirectTime=max(walk_s, 1800),
        directModes="WALK", pedestrianSpeed=speed, via=via.ref if via else None, viaMinimumStay=via_stay if via else None,
        maxTransfers=max_transfers, detailedTransfers=False,
    )


@tool
async def transit_journey_plan(
    origin: str,
    destination: str,
    time: str | None = None,
    arrive_by: bool = False,
    transport: Literal["all", "regional", "rail", "no_rail"] = "all",
    max_walk_minutes: int | None = None,
    walk_speed_kmh: float | None = None,
    via: str | None = None,
    via_stay_minutes: int = 0,
    max_transfers: int | None = None,
    results: int = 3,
) -> dict:
    """Verbindungen von A nach B mit Bahn, Bus, Tram, Fähre – inklusive Fußwegen von/zur Haltestelle.

    time: Ortszeit, z. B. '2026-11-07T10:00'; Abfahrtszeit oder mit arrive_by=true späteste Ankunft. Leer = jetzt.
    transport: all; regional = Nahverkehr ohne IC/ICE und Fernbusse (Schwerbehindertenausweis,
    Deutschlandticket); rail = nur Schiene; no_rail = Bus, Tram, Fähre.
    max_walk_minutes: längster Fußweg zur ersten bzw. von der letzten Haltestelle (bis 120; Standard
    vom Server, meist 30) – lieber laufen als umsteigen oder einen Zubringerbus nehmen.
    walk_speed_kmh: Gehtempo (Standard vom Server, meist zügige 5.5 km/h).
    via: Zwischenhalt (Haltestelle), via_stay_minutes: Mindestaufenthalt dort.
    Jede Verbindung hat eine 'id' für transit_export (Fußwege als Karte/GPX).
    """
    if not 1 <= results <= 6:
        raise TransitError(E.INVALID_ARGUMENT, "results muss zwischen 1 und 6 liegen")
    walk_s = _walk_seconds(max_walk_minutes)
    o, d = await places.resolve(origin), await places.resolve(destination)
    v = await places.resolve(via, want_stop=True) if via else None
    when = I.parse_time(time, (d if arrive_by else o).tz)
    args = (o, d, when, arrive_by, transport, walk_s, _speed(walk_speed_kmh), v, via_stay_minutes, max_transfers, results)
    data = await _plan(*args)

    title = f"{o.name} → {d.name}"
    tz = o.tz or d.tz
    found = data.get("itineraries", [])[:results]
    if not found and data.get("direct"):
        found = data["direct"][:1]
    for it in found:
        _name_ends(it, o.name, d.name)
    items = [{"id": _remember(title, it), **I.compact(it, tz)} for it in found]
    out = {
        "origin": o.public(), "destination": d.public(),
        "time": when.strftime("%Y-%m-%d %H:%M"), "arrive_by": arrive_by,
        "itineraries": items,
    }
    if v:
        out["via"] = v.public()
    direct = data.get("direct") or []
    if direct and data.get("itineraries"):
        out["walk_instead"] = {"minutes": round(direct[0]["duration"] / 60),
                               "distance_km": round((direct[0]["legs"][0].get("distance") or 0) / 1000, 1)}
    if hint := await _timetable_hint(args, when, items):
        out["timetable_hint"] = hint
    if not items:
        out["note"] = ("Keine Verbindung gefunden. Mögliche Gründe: Ort zu weit von einer Haltestelle "
                       "(max_walk_minutes erhöhen), keine Daten für die Region oder das Datum.")
    out["attribution"] = ATTRIBUTION
    return out


async def _timetable_hint(args, when: datetime, items: list[dict]) -> str | None:
    """Vergleicht mit demselben Wochentag in den nächsten Tagen: viel besser → Fahrplan fehlt vermutlich."""
    ref = I.reference_date(when, datetime.now(when.tzinfo))
    if not ref:
        return None
    try:
        data = await _plan(*args[:2], ref, *args[3:])
    except TransitError:
        return None
    ref_items = [I.compact(it, args[0].tz) for it in data.get("itineraries", [])]
    if not ref_items:
        return None
    best = min(c["minutes"] for c in ref_items)
    fastest = next(c for c in ref_items if c["minutes"] == best)
    if items and min(c["minutes"] for c in items) <= best * 1.33:
        return None
    day, ref_day = I.day(when), I.day(ref)
    return (f"Für {day} liefert Transitous {'keine' if not items else 'deutlich schlechtere'} Verbindungen als "
            f"für {ref_day} (gleicher Wochentag): {I.summary(fastest)}. Vermutlich ist der Fahrplan für "
            f"{day} noch nicht (vollständig) veröffentlicht – die Verbindung vom {ref_day} ist der bessere "
            "Anhaltspunkt; kurz vor der Reise erneut prüfen.")


@tool
async def transit_walk_route(origin: str, destination: str, max_minutes: int = 120,
                             walk_speed_kmh: float | None = None) -> dict:
    """Reiner Fußweg (OpenStreetMap) mit Dauer, Strecke, Wegbeschreibung sowie Karte und GPX für Komoot & Co.

    Gut für den Weg von der Haltestelle zum Trailhead oder vom Bahnhof zum Terminal.
    """
    if not 1 <= max_minutes <= 240:
        raise TransitError(E.INVALID_ARGUMENT, "max_minutes muss zwischen 1 und 240 liegen")
    o, d = await places.resolve(origin), await places.resolve(destination)
    data = await api.plan(fromPlace=o.ref, toPlace=d.ref, directModes="WALK", transitModes="WALK",
                          maxDirectTime=max_minutes * 60, pedestrianSpeed=_speed(walk_speed_kmh))
    direct = data.get("direct") or []
    if not direct:
        raise TransitError(E.PLACE_NOT_FOUND, f"Kein Fußweg unter {max_minutes} min gefunden – Punkt evtl. nicht "
                                              "zu Fuß erreichbar (z. B. Sicherheitsbereich) oder zu weit")
    leg = direct[0]["legs"][0]
    leg["from"]["name"], leg["to"]["name"] = o.name, d.name
    streets = []
    for s in leg.get("steps") or []:
        name = s.get("streetName")
        if name and (not streets or streets[-1]["street"] != name):
            streets.append({"street": name, "m": 0})
        if streets and name == streets[-1]["street"]:
            streets[-1]["m"] += round(s.get("distance") or 0)
    return {
        "origin": o.public(), "destination": d.public(),
        "minutes": round(leg["duration"] / 60), "distance_km": round((leg.get("distance") or 0) / 1000, 2),
        "via_streets": [s for s in streets if s["m"] >= 50],
        **exports.save(f"Fußweg {o.name} → {d.name}", [leg]),
        "attribution": ATTRIBUTION,
    }


@tool
async def transit_export(itinerary_id: str, part: Literal["walks", "all"] = "walks") -> dict:
    """Karte und GPX zu einer Verbindung aus transit_journey_plan: nur die Fußwege (Standard) oder alles.

    Die GPX-Datei lässt sich in Komoot (Planen → GPX importieren), OsmAnd oder Outdooractive öffnen.
    """
    entry = _itineraries.get(itinerary_id)
    if not entry:
        raise TransitError(E.EXPORT_NOT_FOUND, "Verbindung unbekannt oder zu alt – transit_journey_plan neu aufrufen")
    title, it = entry
    legs = it["legs"] if part == "all" else [l for l in it["legs"] if l["mode"] == "WALK" and l["duration"] >= 180]
    if not legs:
        raise TransitError(E.INVALID_ARGUMENT, "Diese Verbindung hat keine nennenswerten Fußwege – part='all' nehmen")
    return {
        "title": title,
        "legs": [{"mode": l["mode"], "from": l["from"].get("name"), "to": l["to"].get("name"),
                  "minutes": round(l["duration"] / 60)} for l in legs],
        **exports.save(f"{title} ({'Fußwege' if part == 'walks' else 'gesamt'})", legs),
    }


@tool
async def transit_gpx(file: str) -> dict:
    """Inhalt einer GPX-Datei aus transit_export/transit_walk_route – zum Weiterverarbeiten
    (z. B. an einen Routenplaner geben, Punkte auslesen, als Datei speichern).

    file: eine gpx_url (auch aus 'walks') oder nur der Dateiname, z. B. 'abc….w1.gpx'.
    """
    name = file.strip().rsplit("/x/", 1)[-1]
    found = exports.load(name) if name.endswith(".gpx") else None
    if not found:
        raise TransitError(E.EXPORT_NOT_FOUND, "GPX-Datei unbekannt oder abgelaufen – transit_export neu aufrufen")
    body, _, filename = found
    text = body.decode("utf-8")
    return {"filename": filename, "track_points": text.count("<trkpt"), "waypoints": text.count("<wpt"), "gpx": text}


# ---------- Abfahrten ----------

@tool
async def transit_departures(
    stop: str,
    time: str | None = None,
    arrivals: bool = False,
    count: int = 10,
    transport: Literal["all", "regional", "rail", "no_rail"] = "all",
) -> dict:
    """Abfahrten (oder Ankünfte) an einer Haltestelle ab einer Uhrzeit – z. B. 'wann fährt der letzte Bus vom Trail?'.

    stop: Haltestellenname, 'ref' aus transit_place_search oder 'lat,lon' (nächste Haltestelle).
    time: Ortszeit wie '2026-11-07T17:00', leer = jetzt.
    """
    if not 1 <= count <= 50:
        raise TransitError(E.INVALID_ARGUMENT, "count muss zwischen 1 und 50 liegen")
    p = await places.resolve(stop, want_stop=True)
    if p.kind != "stop":
        p = await places.nearest_stop(p.lat, p.lon)
    when = I.parse_time(time, p.tz)
    modes = None if transport == "all" else I.TRANSPORT[transport]
    data = await api.stoptimes(stopId=p.ref, time=when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               arriveBy=arrivals, n=count, mode=modes, withAlerts=False)
    items = []
    for st in data.get("stopTimes", []):
        sp = st["place"]
        t = sp.get("arrival" if arrivals else "departure")
        sched = sp.get("scheduledArrival" if arrivals else "scheduledDeparture")
        row = {
            "time": I.local(t, sp.get("tz") or p.tz),
            "line": st.get("displayName") or st.get("routeShortName"),
            "mode": st["mode"],
            "headsign": st.get("headsign"),
            "operator": st.get("agencyName"),
            "track": sp.get("track"),
        }
        if st.get("realTime") and sched and t != sched:
            row["delay_min"] = round((I._t(t) - I._t(sched)).total_seconds() / 60)
        if st.get("cancelled") or st.get("tripCancelled"):
            row["cancelled"] = True
        items.append({k: v for k, v in row.items() if v not in (None, "")})
    out = {"stop": p.public(), "time": when.strftime("%Y-%m-%d %H:%M"), "arrivals": arrivals, "items": items}
    if not items:
        out["note"] = "Keine Fahrten gefunden – evtl. fährt dort zu der Zeit nichts oder es fehlen Fahrplandaten"
    out["attribution"] = ATTRIBUTION
    return out


# ---------- Preise (Deutsche Bahn) ----------

async def _db_station(spec: str) -> dict:
    """Bahnhofsname über die DB-Suche; Koordinaten, IATA-Codes und 'stop:'-refs über die nächste DB-Haltestelle."""
    spec = spec.strip()
    if _COORDS.match(spec) or _IATA.match(spec) or spec.startswith("stop:"):
        p = await places.resolve(spec)
        found = await dbapi.nearby(p.lat, p.lon)
    else:
        found = await dbapi.stations(spec)
    if not found:
        raise TransitError(E.PLACE_NOT_FOUND, f"Kein Bahnhof/keine Haltestelle bei der DB gefunden für: {spec}")
    return found[0]


@tool
async def transit_db_prices(
    origin: str,
    destination: str,
    time: str | None = None,
    arrive_by: bool = False,
    transport: Literal["all", "regional", "no_ice"] = "all",
    deutschland_ticket: bool = False,
    bahncard: Literal["none", "25", "50"] = "none",
    first_class: bool = False,
    fares: bool = False,
    max_transfers: int | None = None,
    results: int = 4,
) -> dict:
    """Bahnpreise der Deutschen Bahn für Verbindungen (vor allem innerhalb Deutschlands) – eigene Suche bei der DB.

    origin/destination: Bahnhofsname (z. B. 'Osnabrück Hbf'), 'lat,lon' oder IATA-Code (nächste Haltestelle).
    time: Ortszeit Deutschland, z. B. '2026-11-07T10:00', leer = jetzt; arrive_by=true = späteste Ankunft.
    transport: all; regional = nur Nahverkehr; no_ice = ohne ICE.
    deutschland_ticket=true: Nahverkehrsabschnitte gelten als bezahlt, der Preis ist dann nur der Fernverkehrsteil.
    Mit Schwerbehindertenausweis und Wertmarke ist Nahverkehr kostenlos – dafür ebenfalls deutschland_ticket=true.
    price_from ist der günstigste Preis (meist Super Sparpreis mit Zugbindung, kann sich ändern);
    fares=true liefert pro Verbindung zusätzlich Super Sparpreis/Sparpreis/Flexpreis bzw. den Verbundtarif.
    Für Fahrten der nächsten Stunden auch Echtzeit der DB: departure_delay_min, arrival_delay_min, cancelled,
    platform_change, notes (Störungsmeldungen) je Abschnitt.
    Die DB-Ortssuche ist unscharf – die aufgelösten Namen in origin/destination prüfen.
    Preise ohne Gewähr; inoffizielle Schnittstelle, die DB blockt/drosselt gelegentlich (sparsam aufrufen).
    """
    if not 1 <= results <= 6:
        raise TransitError(E.INVALID_ARGUMENT, "results muss zwischen 1 und 6 liegen")
    o, d = await _db_station(origin), await _db_station(destination)
    when = I.parse_time(time, "Europe/Berlin")
    tv = D.traveller(bahncard, first_class, deutschland_ticket)
    data = await dbapi.journeys(o["locationId"], d["locationId"], when, arrive_by, tv, D.PRODUCTS[transport], max_transfers)
    items = []
    for j in (data.get("verbindungen") or [])[:results]:
        item = D.compact(j)
        # Verbund-Verbindungen haben in der Suche keinen Preis – dann die Tarife einzeln abfragen
        if fares or "price_from" not in item:
            try:
                found = D.fares(await dbapi.offers(j["verbindung"]["kontext"], tv), first_class)
            except TransitError:
                found = []
            if found:
                item["fares"] = found
                item.setdefault("price_from", {k: v for k, v in min(found, key=lambda f: f["amount"]).items()
                                               if k != "name"})
        items.append(item)
    out = {
        "origin": o["name"], "destination": d["name"], "time": when.strftime("%Y-%m-%d %H:%M"), "arrive_by": arrive_by,
        "traveller": {"bahncard": bahncard, "class": 1 if first_class else 2, "deutschland_ticket": deutschland_ticket},
        "journeys": items,
    }
    if not items:
        out["note"] = "Keine Verbindung bei der DB gefunden"
    elif any("price_from" not in i for i in items):
        out["note"] = "Für manche Verbindungen nennt die DB keinen Preis (z. B. Auslandsstrecken) – auf bahn.de prüfen"
    out["attribution"] = D.ATTRIBUTION
    return out


# ---------- HTTP-Server mit Token ----------

class BearerAuth:
    """Lässt nur Requests mit 'Authorization: Bearer <MCP_TOKEN>' durch – außer /health und Exporten (/x/…)."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            if path == "/health":
                await _respond(send, 200, b"ok")
                return
            if path.startswith("/x/"):
                found = exports.load(path[3:])
                if not found:
                    await _respond(send, 404, b"not found")
                    return
                body, ctype, filename = found
                extra = [(b"content-disposition", f'attachment; filename="{filename}"'.encode())] if filename else []
                await _respond(send, 200, body, ctype.encode(), extra)
                return
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(auth, self.expected):
                await _respond(send, 401, b"unauthorized")
                return
        await self.app(scope, receive, send)


async def _respond(send, status: int, body: bytes, ctype: bytes = b"text/plain", extra: list | None = None):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype), (b"x-robots-tag", b"noindex"), *(extra or [])]})
    await send({"type": "http.response.body", "body": body})


def main() -> None:
    token = os.environ.get("MCP_TOKEN")
    if not token:
        raise SystemExit("MCP_TOKEN fehlt – ohne Token starte ich nicht.")
    if not api.user_agent:
        raise SystemExit("TRANSITOUS_USER_AGENT fehlt – Transitous verlangt App-Name, Version und Kontakt.")
    # stateless + JSON-Antworten: kein SSE, das vertragen Cloudflare und Caddy am besten
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    uvicorn.run(BearerAuth(app, token), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
