"""Offline-Tests der reinen Logik (ohne Netz).

    uv run python tests/unit.py
"""

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from transitwalter import itinerary as I
from transitwalter.export import decode_polyline, gpx
from transitwalter.places import _COORDS, _IATA, airports, plain

failed = 0


def check(name: str, ok: bool, detail="") -> None:
    global failed
    failed += not ok
    print(f"{'ok    ' if ok else 'FEHLER'} {name} {detail}")


def leg(mode, start, end, frm="A", to="B", **extra):
    return {"mode": mode, "startTime": start, "endTime": end, "from": {"name": frm, "tz": "Europe/Berlin"},
            "to": {"name": to, "tz": "Europe/Berlin"}, **extra}


# Polyline mit Genauigkeit 6 (Beispiel aus einer MOTIS-Antwort, Holzwickede)
pts = decode_polyline("ggrfaBybapMfJfb@i@`@{@_E", 6)
check("Polyline dekodieren", len(pts) == 4 and abs(pts[0][0] - 51.5048) < 0.001 and abs(pts[0][1] - 7.6197) < 0.001, pts[0])

g = gpx("Test & <Weg>", [leg("WALK", "2026-10-29T12:00:00Z", "2026-10-29T12:20:00Z", "Bahnhof", "Terminal",
                             duration=1200, distance=1664, legGeometry={"points": "ggrfaBybapMfJfb@", "precision": 6})])
check("GPX escaped und mit Trackpunkten", "Test &amp; &lt;Weg&gt;" in g and g.count("<trkpt") == 2)

# Warnungen
it = {"legs": [
    leg("BUS", "2026-10-29T10:00:00Z", "2026-10-29T10:30:00Z", to="Loksa", duration=1800),
    leg("BUS", "2026-10-29T11:30:00Z", "2026-10-29T12:00:00Z", frm="Loksa", to="Ormea", duration=1800),
    leg("BUS", "2026-10-29T20:00:00Z", "2026-10-29T20:30:00Z", frm="Ormea", duration=1800),
    leg("BUS", "2026-10-29T20:10:00Z", "2026-10-29T20:40:00Z", duration=1800),
    leg("WALK", "2026-10-29T20:40:00Z", "2026-10-29T21:20:00Z", duration=2400, distance=3000),
]}
w = I.warnings(it)
check("Wartezeit erkannt", any("60 min Wartezeit in Loksa" in x for x in w), w)
check("Übernachtung erkannt", any(x.startswith("Übernachtung") and "Ormea" in x for x in w))
check("Widerspruch erkannt", any("widersprüchlich" in x for x in w))
check("Langer Fußweg erkannt", any("Langer Fußweg: 40 min" in x for x in w))

# Zeiten
tz = "Europe/Tallinn"
t = I.parse_time("2026-11-07T10:00", tz)
check("Ortszeit ohne Offset", t.utcoffset().total_seconds() == 7200, t)
check("Ausgabe in Ortszeit", I.local("2026-11-07T08:00:00Z", tz) == "2026-11-07 10:00")
today = datetime(2026, 10, 28, 12, tzinfo=ZoneInfo(tz))
ref = I.reference_date(t, today)
check("Referenztag gleicher Wochentag", ref and ref.date().isoformat() == "2026-10-31", ref)
check("Kein Referenztag für bald", I.reference_date(datetime(2026, 10, 30, tzinfo=ZoneInfo(tz)), today) is None)

# Ortsangaben
check("Koordinaten erkannt", bool(_COORDS.match("59.67184, 25.69492")))
check("IATA erkannt", _IATA.match("NCE")[1] == "NCE" and _IATA.match("airport:TLL")[1] == "TLL" and not _IATA.match("Ulm"))
check("Flughafenliste geladen", {"DTM", "NCE", "LTN", "TLL", "BSL"} <= set(airports()), len(airports()))
check("Akzente entfernt", plain("Aéroport Lennujaam") == "aeroport lennujaam")

print(f"\n{'Alles ok' if not failed else f'{failed} Fehler'}")
sys.exit(1 if failed else 0)
