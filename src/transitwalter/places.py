"""Orte auflösen: Koordinaten, Haltestellen-IDs, IATA-Codes oder freier Text.

Flughäfen sind der Sonderfall: Ihre offiziellen Koordinaten liegen meist mitten auf
dem Gelände (Rollfeld, Sicherheitsbereich), von dort findet MOTIS oft keinen Fußweg.
Deshalb wird für einen IATA-Code die Haltestelle am Terminal gesucht und verwendet.
"""

import csv
import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path

from . import errors as E
from .errors import TransitError
from .transitous import Transitous

_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_IATA = re.compile(r"^(?:airport:)?([A-Z]{3})$")

# Wörter, an denen man eine Flughafen-Haltestelle erkennt (ohne Akzente, klein)
AIRPORT_WORDS = (
    "airport", "aeroport", "flughafen", "aeroporto", "aeropuerto", "aeroporti", "lennujaam", "lotnisko",
    "letiste", "lufthavn", "flygplats", "lentoasema", "havalimani", "luchthaven", "repuloter", "aerodrom",
    "zracna luka", "aerogara", "letalisce", "lidosta", "oro uostas", "terminal", "airside", "aerodromio",
)


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    ref: str                  # für MOTIS: Haltestellen-ID oder "lat,lon"
    tz: str | None = None
    kind: str = "coordinates"  # coordinates | stop | address | place | airport
    country: str | None = None
    note: str | None = None

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(a))


@cache
def airports() -> dict[str, dict]:
    with open(Path(__file__).parent / "data/airports.csv", encoding="utf-8") as f:
        return {r["iata"]: {**r, "lat": float(r["lat"]), "lon": float(r["lon"])} for r in csv.DictReader(f)}


def area(item: dict) -> str | None:
    """Kurze Ortsangabe aus den Verwaltungsebenen eines Geocoding-Treffers."""
    areas = item.get("areas") or []
    default = next((a["name"] for a in areas if a.get("default")), None)
    country = areas[0]["name"] if areas else None
    parts = [p for p in (default, country) if p and p != item.get("name")]
    return ", ".join(dict.fromkeys(parts)) or None


def from_geocode(item: dict) -> Place:
    kind = {"STOP": "stop", "ADDRESS": "address"}.get(item.get("type"), "place")
    name = item["name"]
    if item.get("houseNumber") and item.get("street") and item["houseNumber"] not in name:
        name = f"{item['street']} {item['houseNumber']}"
    # Adressen und Orte als Koordinaten übergeben, Haltestellen per ID
    ref = item["id"] if kind == "stop" and item.get("id") else f"{item['lat']},{item['lon']}"
    return Place(name=name, lat=item["lat"], lon=item["lon"], ref=ref, tz=item.get("tz"), kind=kind,
                 country=item.get("country"), note=area(item))


class Places:
    def __init__(self, api: Transitous):
        self.api = api

    async def resolve(self, spec: str, want_stop: bool = False) -> Place:
        """'lat,lon', IATA-Code (z. B. 'NCE' oder 'airport:NCE'), Haltestellen-ID ('stop:…') oder Text."""
        spec = spec.strip()
        if not spec:
            raise TransitError(E.INVALID_ARGUMENT, "Leerer Ort")
        if m := _COORDS.match(spec):
            lat, lon = float(m[1]), float(m[2])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise TransitError(E.INVALID_ARGUMENT, f"Koordinaten außerhalb des gültigen Bereichs: {spec}")
            if want_stop:
                return await self.nearest_stop(lat, lon)
            near = await self.api.reverse_geocode(lat, lon)
            name = near[0]["name"] if near else spec
            return Place(name=name, lat=lat, lon=lon, ref=f"{lat},{lon}", tz=near[0].get("tz") if near else None,
                         country=near[0].get("country") if near else None)
        if spec.startswith("stop:"):
            return await self.stop_by_id(spec[5:])
        if m := _IATA.match(spec):
            return (await self.airport(m[1]))["place"]
        found = await self.api.geocode(spec, type="STOP" if want_stop else None)
        if not found:
            raise TransitError(E.PLACE_NOT_FOUND, f"Kein Ort gefunden für: {spec}")
        return from_geocode(found[0])

    async def stop_by_id(self, stop_id: str) -> Place:
        data = await self.api.stoptimes(stopId=stop_id, n=1)
        p = data.get("place") or {}
        if not p:
            raise TransitError(E.PLACE_NOT_FOUND, f"Haltestelle unbekannt: {stop_id}")
        return Place(name=p["name"], lat=p["lat"], lon=p["lon"], ref=stop_id, tz=p.get("tz"), kind="stop")

    async def nearest_stop(self, lat: float, lon: float) -> Place:
        for delta in (0.005, 0.02, 0.06):
            stops = await self.api.stops_in_box(lat, lon, delta)
            if stops:
                s = min(stops, key=lambda s: distance_m(lat, lon, s["lat"], s["lon"]))
                return Place(name=s["name"], lat=s["lat"], lon=s["lon"], ref=s["stopId"], tz=s.get("tz"), kind="stop")
        raise TransitError(E.PLACE_NOT_FOUND, f"Keine Haltestelle in der Nähe von {lat},{lon}")

    async def airport(self, code: str) -> dict:
        """Flughafen mit den Haltestellen am Terminal; 'place' ist der beste Start-/Zielpunkt."""
        code = code.strip().upper().removeprefix("AIRPORT:")
        ap = airports().get(code)
        if not ap:
            raise TransitError(E.AIRPORT_NOT_FOUND, f"Unbekannter IATA-Code: {code}")
        stops = await self.api.stops_in_box(ap["lat"], ap["lon"], 0.035)
        by_name: dict[str, dict] = {}
        for s in stops:
            modes = set(s.get("modes", [])) - {"ODM", "AIRPLANE"}
            if not modes:
                continue  # Flughafen selbst als "Haltestelle" der Flugdaten
            s["distance_m"] = round(distance_m(ap["lat"], ap["lon"], s["lat"], s["lon"]))
            # Fernbus-Haltestellen nur nachrangig: Bahn und Stadtbus sind meist der bessere Startpunkt
            s["importance"] = s.get("importance", 0) * (0.3 if modes == {"COACH"} else 1)
            s["is_airport"] = any(w in plain(s["name"]) for w in AIRPORT_WORDS)
            best = by_name.get(s["name"])
            if not best or s.get("importance", 0) > best.get("importance", 0):
                by_name[s["name"]] = s
        candidates = sorted(by_name.values(), key=lambda s: (not s["is_airport"], -s.get("importance", 0)))
        candidates = [s for s in candidates if s["is_airport"]] or sorted(candidates, key=lambda s: s["distance_m"])
        info = {
            "iata": code, "name": ap["name"], "city": ap["city"] or None, "country": ap["country"],
            "lat": ap["lat"], "lon": ap["lon"],
            "stops": [{"name": s["name"], "ref": f"stop:{s['stopId']}", "lat": s["lat"], "lon": s["lon"],
                       "modes": [m for m in s.get("modes", []) if m != "ODM"], "distance_m": s["distance_m"]}
                      for s in candidates[:6]],
        }
        if candidates and candidates[0]["is_airport"]:
            s = candidates[0]
            info["place"] = Place(name=f"{ap['name']} ({s['name']})", lat=s["lat"], lon=s["lon"],
                                  ref=f"{s['lat']},{s['lon']}", tz=s.get("tz"), kind="airport", country=ap["country"])
        else:
            near = await self.api.reverse_geocode(ap["lat"], ap["lon"])
            info["place"] = Place(name=ap["name"], lat=ap["lat"], lon=ap["lon"], ref=f"{ap['lat']},{ap['lon']}",
                                  tz=near[0].get("tz") if near else None, kind="airport", country=ap["country"],
                                  note="Keine Haltestelle am Terminal gefunden – Mittelpunkt des Flughafens verwendet")
        return info
