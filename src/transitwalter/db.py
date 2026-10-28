"""Preise und Verbindungen der Deutschen Bahn über die Schnittstelle des DB Navigators.

Inoffiziell (wie db-vendo-client, https://github.com/public-transport/db-vendo-client): keine Keys,
aber die DB blockt gelegentlich – je nach IP und User-Agent ("OPS_BLOCKED"). Deshalb wenig Last:
Cache und ein eigenes Anfragelimit pro Minute.
"""

import asyncio
import json
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime

import httpx

from . import errors as E
from .errors import TransitError

BASE = "https://app.services-bahn.de/mob"
CT_JOURNEY = "application/x.db.vendo.mob.verbindungssuche.v9+json"
CT_LOCATION = "application/x.db.vendo.mob.location.v3+json"
ATTRIBUTION = "Preise und Verbindungen: Deutsche Bahn (DB Navigator, inoffizielle Schnittstelle) – ohne Gewähr"

PRODUCTS = {
    "all": ["ALL"],
    "regional": ["NAHVERKEHRSONSTIGEZUEGE", "SBAHNEN", "BUSSE", "SCHIFFE", "UBAHN", "STRASSENBAHN",
                 "ANRUFPFLICHTIGEVERKEHRE"],
    "no_ice": ["INTERCITYUNDEUROCITYZUEGE", "INTERREGIOUNDSCHNELLZUEGE", "NAHVERKEHRSONSTIGEZUEGE", "SBAHNEN",
               "BUSSE", "SCHIFFE", "UBAHN", "STRASSENBAHN", "ANRUFPFLICHTIGEVERKEHRE"],
}


class DB:
    def __init__(self, user_agent: str, per_minute: int = 20, cache_size: int = 300):
        self.user_agent = user_agent
        self.per_minute = per_minute
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._cache_size = cache_size
        self._http: httpx.AsyncClient | None = None

    async def post(self, path: str, body: dict, content_type: str, ttl: int = 600):
        key = path + json.dumps(body, sort_keys=True)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            self._cache.move_to_end(key)
            return hit[1]
        await self._throttle()
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30, headers={"User-Agent": self.user_agent, "Accept-Language": "de"})
        headers = {"Accept": content_type, "Content-Type": content_type,
                   "X-Correlation-ID": f"{uuid.uuid4()}_{uuid.uuid4()}"}
        try:
            resp = await self._http.post(f"{BASE}{path}", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise TransitError(E.UPSTREAM_ERROR, f"DB nicht erreichbar ({type(e).__name__})")
        if resp.status_code == 452 or "OPS_BLOCKED" in resp.text[:200]:
            raise TransitError(E.UPSTREAM_ERROR, "Die DB blockt Anfragen gerade (OPS_BLOCKED) – später erneut "
                                                 "versuchen oder Preis auf bahn.de nachsehen")
        if resp.status_code == 429:
            raise TransitError(E.RATE_LIMITED, "Die DB drosselt gerade – später erneut versuchen")
        if resp.status_code in (400, 422):
            raise TransitError(E.INVALID_ARGUMENT, f"DB lehnt die Anfrage ab: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise TransitError(E.UPSTREAM_ERROR, f"DB antwortet mit HTTP {resp.status_code}")
        data = resp.json()
        self._cache[key] = (time.monotonic() + ttl, data)
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return data

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._sent and now - self._sent[0] > 60:
                self._sent.popleft()
            if len(self._sent) >= self.per_minute:
                raise TransitError(E.RATE_LIMITED, f"Eigenes DB-Limit von {self.per_minute} Anfragen pro Minute erreicht")
            self._sent.append(now)

    # ---------- Endpunkte ----------

    async def stations(self, text: str, limit: int = 5) -> list[dict]:
        return await self.post("/location/search", {"locationTypes": ["ST"], "searchTerm": text, "maxResults": limit},
                               CT_LOCATION, ttl=86400)

    async def nearby(self, lat: float, lon: float, radius_m: int = 3000, limit: int = 5) -> list[dict]:
        return await self.post("/location/nearby", {
            "area": {"coordinates": {"latitude": lat, "longitude": lon}, "radius": radius_m},
            "maxResults": limit, "products": ["ALL"],
        }, CT_LOCATION, ttl=86400)

    async def journeys(self, origin_id: str, destination_id: str, when: datetime, arrive_by: bool,
                       traveller: dict, products: list[str], max_transfers: int | None = None) -> dict:
        wish = {
            "abgangsLocationId": origin_id, "zielLocationId": destination_id, "verkehrsmittel": products,
            "alternativeHalteBerechnung": True,
            "zeitWunsch": {"reiseDatum": when.isoformat(timespec="seconds"),
                           "zeitPunktArt": "ANKUNFT" if arrive_by else "ABFAHRT"},
        }
        if max_transfers is not None:  # null lehnt die DB ab
            wish["maxUmstiege"] = max_transfers
        body = {**traveller, "reiseHin": {"wunsch": wish}}
        return await self.post("/angebote/fahrplan", body, CT_JOURNEY)

    async def offers(self, context: str, traveller: dict) -> dict:
        """Alle Tarife einer Verbindung (Super Sparpreis, Sparpreis, Flexpreis bzw. Verbundtarif)."""
        return await self.post("/angebote/recon", {**traveller, "verbindungHin": {"kontext": context}}, CT_JOURNEY)


def traveller(bahncard: str = "none", first_class: bool = False, deutschland_ticket: bool = False,
              age: int | None = None) -> dict:
    """Reisendenprofil im Format der DB (ein Erwachsener, optional mit BahnCard/Deutschlandticket)."""
    cls = "KLASSE_1" if first_class else "KLASSE_2"
    card = "KEINE_ERMAESSIGUNG KLASSENLOS" if bahncard == "none" else f"BAHNCARD{bahncard} {cls}"
    person = {"ermaessigungen": [card], "reisendenTyp": _age_group(age)}
    if age is not None:
        person["alter"] = age
    return {
        "autonomeReservierung": False, "einstiegsTypList": ["STANDARD"], "klasse": cls,
        "fahrverguenstigungen": {"deutschlandTicketVorhanden": deutschland_ticket,
                                 "nurDeutschlandTicketVerbindungen": False},
        "reisendenProfil": {"reisende": [person]}, "reservierungsKontingenteVorhanden": False,
    }


def _age_group(age: int | None) -> str:
    if age is None:
        return "ERWACHSENER"
    return "KLEINKIND" if age < 6 else "FAMILIENKIND" if age < 15 else "JUGENDLICHER" if age < 27 \
        else "ERWACHSENER" if age < 65 else "SENIOR"


# ---------- Antworten aufbereiten ----------

def _hhmm(ts: str | None) -> str | None:
    return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M") if ts else None


def price_from(offer: dict | None) -> dict | None:
    """'ab'-Preis aus der Verbindungssuche (meist der günstigste Sparpreis)."""
    p = (offer or {}).get("preise") or {}
    ab = (p.get("gesamt") or {}).get("ab") or {}
    if ab.get("betrag") is None:
        return None
    out = {"amount": ab["betrag"], "currency": ab.get("waehrung", "EUR")}
    if p.get("istTeilpreis"):
        out["partial"] = True
    return out


def _position(pos: dict | None) -> dict | None:
    while pos and pos.get("reisePosition"):
        pos = pos["reisePosition"]
    return pos


def fares(recon: dict, first_class: bool = False) -> list[dict]:
    """Tarife aus /angebote/recon: Flächentarif (Super Sparpreis …) und je Verbund das erste Angebot (EinzelTicket)."""
    want = "KLASSE_1" if first_class else "KLASSE_2"
    out = []
    for cluster in (recon.get("angebote") or {}).get("angebotsCluster") or []:
        for sub in cluster.get("angebotsSubCluster") or []:
            verbund_seen = False
            for p in sub.get("angebotsPositionen") or []:
                if "einfacheFahrt" in p:
                    pos = _position((p["einfacheFahrt"].get("standard") or {}).get("reisePosition"))
                elif "verbundAngebot" in p and not verbund_seen:
                    pos = _position(p["verbundAngebot"].get("reisePosition"))
                    verbund_seen = True
                else:
                    continue
                if not pos or (pos.get("preis") or {}).get("betrag") is None:
                    continue
                classes = {n.get("klasse") for n in pos.get("nutzungsInformationen") or []}
                if classes and want not in classes:
                    continue
                fare = {"name": pos.get("name"), "amount": pos["preis"]["betrag"],
                        "currency": pos["preis"].get("waehrung", "EUR")}
                if pos.get("teilpreis"):
                    fare["partial"] = True
                if fare not in out:
                    out.append(fare)
    return out


def compact(journey: dict) -> dict:
    """Verbindung aus der DB-Suche → kurze Übersicht mit Abschnitten und 'ab'-Preis."""
    v = journey["verbindung"]
    legs = []
    for a in v["verbindungsAbschnitte"]:
        halte = a.get("halte") or []
        dep = a.get("abgangsDatum") or (halte[0].get("abgangsDatum") if halte else None)
        arr = a.get("ankunftsDatum") or (halte[-1].get("ankunftsDatum") if halte else None)
        frm = (a.get("abgangsOrt") or (halte[0]["ort"] if halte else {})).get("name")
        to = (a.get("ankunftsOrt") or (halte[-1]["ort"] if halte else {})).get("name")
        if a["typ"] == "FAHRZEUG":
            legs.append({"line": a.get("mitteltext") or a.get("kurztext"), "product": a.get("kurztext"),
                         "from": frm, "to": to, "departure": _hhmm(dep), "arrival": _hhmm(arr)})
        elif a["typ"] in ("FUSSWEG", "TRANSFER") and a.get("abschnittsDauer", 0) >= 60 and frm != to:
            legs.append({"line": "Fußweg", "from": frm, "to": to, "minutes": round(a["abschnittsDauer"] / 60)})
    rides = [l for l in legs if l["line"] != "Fußweg"]
    first, last = v["verbindungsAbschnitte"][0], v["verbindungsAbschnitte"][-1]
    out = {
        "departure": _hhmm(first.get("abgangsDatum")) or (rides[0]["departure"] if rides else None),
        "arrival": _hhmm(last.get("ankunftsDatum")) or (rides[-1]["arrival"] if rides else None),
        "minutes": round(v.get("reiseDauer", 0) / 60),
        "transfers": v.get("umstiegeAnzahl", max(len(rides) - 1, 0)),
        "products": sorted({l["product"] for l in rides if l.get("product")}),
        "legs": legs,
    }
    if p := price_from(journey.get("angebote")):
        out["price_from"] = p
    if (journey.get("angebote") or {}).get("verbundCode"):
        out["verbund"] = True
    return out
