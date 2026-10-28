"""MOTIS-Verbindungen kompakt machen und auf Auffälligkeiten prüfen.

Die Rohantwort ist groß (Liniengeometrien, Zwischenhalte); für den Assistenten zählen
Zeiten, Linien, Umstiege, Fußwege – und Warnungen, wenn etwas nicht plausibel ist.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import errors as E
from .errors import TransitError

# Verkehrsmittel-Auswahl → MOTIS transitModes
TRANSPORT = {
    "all": ["TRANSIT"],
    # wie Deutschlandticket: kein Fernverkehr, kein Fernbus
    "regional": ["BUS", "TRAM", "SUBWAY", "METRO", "SUBURBAN", "REGIONAL_RAIL", "REGIONAL_FAST_RAIL", "FERRY",
                 "FUNICULAR", "AERIAL_LIFT", "CABLE_CAR", "OTHER"],
    "rail": ["RAIL", "HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_FAST_RAIL", "REGIONAL_RAIL",
             "SUBURBAN", "SUBWAY", "METRO", "TRAM"],
    "no_rail": ["BUS", "COACH", "TRAM", "FERRY", "FUNICULAR", "AERIAL_LIFT", "CABLE_CAR", "OTHER"],
}

LONG_WAIT_MIN = 45        # Hinweis ab dieser Wartezeit zwischen zwei Fahrten
OVERNIGHT_WAIT_MIN = 240  # ab hier vermutlich Übernachtung nötig


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def parse_time(value: str | None, tz: str | None) -> datetime:
    """'2026-11-07T10:00' (Ortszeit von tz), mit Offset oder 'Z'; leer = jetzt."""
    if not value:
        return datetime.now(zone(tz))
    try:
        t = datetime.fromisoformat(value.strip().replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        raise TransitError(E.INVALID_ARGUMENT, f"Zeit nicht lesbar: {value} (Format z. B. 2026-11-07T10:00)")
    return t if t.tzinfo else t.replace(tzinfo=zone(tz))


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def local(iso: str | None, tz: str | None) -> str | None:
    """Ortszeit als 'YYYY-MM-DD HH:MM' in der Zeitzone der Haltestelle."""
    return _t(iso).astimezone(zone(tz)).strftime("%Y-%m-%d %H:%M") if iso else None


def _delay(leg: dict, key: str, sched: str) -> int | None:
    if not leg.get("realTime") or not leg.get(sched):
        return None
    d = round((_t(leg[key]) - _t(leg[sched])).total_seconds() / 60)
    return d or None


def compact_leg(leg: dict, tz_fallback: str | None) -> dict:
    f, t = leg["from"], leg["to"]
    ftz, ttz = f.get("tz") or tz_fallback, t.get("tz") or tz_fallback
    out = {
        "mode": leg["mode"],
        "from": f.get("name") or "Start",
        "to": t.get("name") or "Ziel",
        "departure": local(leg["startTime"], ftz),
        "arrival": local(leg["endTime"], ttz),
        "minutes": round(leg["duration"] / 60),
    }
    if leg["mode"] in ("WALK", "BIKE", "CAR"):
        if leg.get("distance"):
            out["distance_km"] = round(leg["distance"] / 1000, 1)
        return out
    out.update({
        "line": leg.get("displayName") or leg.get("routeShortName") or leg.get("tripShortName"),
        "headsign": leg.get("headsign"),
        "operator": leg.get("agencyName"),
        "stops": len(leg.get("intermediateStops") or []) + 1,
    })
    if f.get("track"):
        out["track"] = f["track"]
    if (d := _delay(leg, "startTime", "scheduledStartTime")) is not None:
        out["delay_min"] = d
    if leg.get("realTime"):
        out["realtime"] = True
    if leg.get("cancelled"):
        out["cancelled"] = True
    if leg.get("reservation") not in (None, "NONE"):
        out["reservation"] = leg["reservation"]
    return {k: v for k, v in out.items() if v not in (None, "")}


def warnings(it: dict) -> list[str]:
    """Plausibilitäts-Checks: lange Wartezeiten, Übernachtungen, widersprüchliche Daten."""
    out = []
    legs = it["legs"]
    transit = [l for l in legs if l["mode"] not in ("WALK", "BIKE", "CAR")]
    for prev, nxt in zip(transit, transit[1:]):
        wait = (_t(nxt["startTime"]) - _t(prev["endTime"])).total_seconds() / 60
        where = prev["to"].get("name", "?")
        tz = prev["to"].get("tz")
        if wait < -1:
            out.append(f"Daten widersprüchlich: Anschluss in {where} fährt vor der Ankunft ab – Verbindung prüfen")
        elif wait >= OVERNIGHT_WAIT_MIN:
            out.append(f"Übernachtung/sehr lange Pause in {where}: {local(prev['endTime'], tz)} bis "
                       f"{local(nxt['startTime'], tz)} ({wait / 60:.1f} h)")
        elif wait >= LONG_WAIT_MIN:
            out.append(f"{round(wait)} min Wartezeit in {where}")
    for l in legs:
        if l["mode"] == "WALK" and l["duration"] >= 30 * 60:
            out.append(f"Langer Fußweg: {round(l['duration'] / 60)} min / {round((l.get('distance') or 0) / 1000, 1)} km "
                       f"({l['from'].get('name') or 'Start'} → {l['to'].get('name') or 'Ziel'})")
        if l.get("cancelled"):
            out.append(f"Fahrt fällt aus: {l.get('displayName') or l['mode']} ab {l['from'].get('name')}")
    return out


def compact(it: dict, tz_fallback: str | None = None) -> dict:
    legs = it["legs"]
    first, last = legs[0], legs[-1]
    out = {
        "departure": local(it["startTime"], first["from"].get("tz") or tz_fallback),
        "arrival": local(it["endTime"], last["to"].get("tz") or tz_fallback),
        "minutes": round(it["duration"] / 60),
        "transfers": it.get("transfers", 0),
        "walk_km": round(sum((l.get("distance") or 0) for l in legs if l["mode"] == "WALK") / 1000, 1),
        "legs": [compact_leg(l, tz_fallback) for l in legs],
    }
    if w := warnings(it):
        out["warnings"] = w
    return out


def summary(c: dict) -> str:
    """Einzeiler, z. B. für die Fahrplan-Warnung."""
    lines = [l.get("line") or l["mode"] for l in c["legs"] if l["mode"] != "WALK"]
    n = c["transfers"]
    return (f"{c['departure'][11:]}–{c['arrival'][11:]} ({c['minutes']} min, {n} Umstieg{'' if n == 1 else 'e'}: "
            f"{' → '.join(lines)})")


WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def day(t: datetime) -> str:
    """'Sa 07.11.'"""
    return f"{WEEKDAYS[t.weekday()]} {t:%d.%m.}"


def reference_date(when: datetime, today: datetime) -> datetime | None:
    """Gleicher Wochentag in den nächsten 7 Tagen – nur wenn when mindestens 5 Tage entfernt ist."""
    days = (when.date() - today.date()).days
    if days < 5:
        return None
    ahead = (when.weekday() - today.weekday()) % 7 or 7
    ref = today.date() + timedelta(days=ahead)
    if ref >= when.date():
        return None
    return when.replace(year=ref.year, month=ref.month, day=ref.day)
