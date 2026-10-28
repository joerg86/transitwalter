"""Offline-Tests der reinen Logik (ohne Netz).

    uv run python tests/unit.py
"""

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from transitwalter import itinerary as I
from transitwalter.export import Exports, decode_polyline, gpx, slug
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

check("Dateiname", slug("Fußweg Holzwickede, Bahnhof → Dortmund Airport") == "fussweg-holzwickede-bahnhof-dortmund-airport")

# Exporte: Pfade
import tempfile
ex = Exports(tempfile.mkdtemp(), "https://example.org")
walk = leg("WALK", "2026-10-29T12:00:00Z", "2026-10-29T12:20:00Z", "Bahnhof", "Terminal", duration=1200, distance=1664,
           legGeometry={"points": "ggrfaBybapMfJfb@", "precision": 6})
for side, (la, lo) in (("from", (51.50477, 7.61965)), ("to", (51.51461, 7.61264))):
    walk[side].update(lat=la, lon=lo)
bus = leg("BUS", "2026-10-29T11:00:00Z", "2026-10-29T12:00:00Z", "Ort", "Bahnhof", duration=3600, displayName="RE7")
for side in ("from", "to"):
    bus[side].update(lat=51.5, lon=7.6)
short = {**walk, "duration": 60}
r = ex.save("Ort → Terminal", [bus, short, walk])
eid = r["map_url"].rsplit("/", 1)[1]
check("Ein GPX pro (nicht zu kurzem) Fußweg", len(r["walks"]) == 1 and r["walks"][0]["gpx_url"].endswith(f"{eid}.w1.gpx"), r["walks"])
body, ctype, name = ex.load(f"{eid}.w1.gpx")
check("Einzel-GPX mit Wegpunkten und Dateiname", b"<wpt" in body and name == "fussweg-bahnhof-terminal.gpx", name)
check("Karte verlinkt Einzel-GPX", f"{eid}.w1.gpx".encode() in ex.load(eid)[0])
check("Ungültige Pfade abgelehnt", ex.load("../etc/passwd") is None and ex.load(f"{eid}.w1") is None and ex.load(f"{eid}.w9.gpx") is None)

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

# DB-Preise und Echtzeit (Aufbau wie Antworten des DB Navigators)
from transitwalter import db as D
check("BahnCard im Reisendenprofil", D.traveller("25")["reisendenProfil"]["reisende"][0]["ermaessigungen"] == ["BAHNCARD25 KLASSE_2"])
check("D-Ticket im Reisendenprofil", D.traveller(deutschland_ticket=True)["fahrverguenstigungen"]["deutschlandTicketVorhanden"])
check("Ab-Preis", D.price_from({"preise": {"istTeilpreis": True, "gesamt": {"ab": {"waehrung": "EUR", "betrag": 25.8}}}})
      == {"amount": 25.8, "currency": "EUR", "partial": True})
check("Kein Preis", D.price_from({"verbundCode": "555", "angebotsCluster": []}) is None)
pos = lambda name, eur, cls="KLASSE_2": {"reisePosition": {"reisePosition": {
    "name": name, "preis": {"waehrung": "EUR", "betrag": eur}, "nutzungsInformationen": [{"klasse": cls}]}}}
recon = {"angebote": {"angebotsCluster": [{"angebotsSubCluster": [{"angebotsPositionen": [
    {"einfacheFahrt": {"standard": pos("Super Sparpreis", 18.99)}},
    {"einfacheFahrt": {"standard": pos("Flexpreis", 84.4, "KLASSE_1")}},
    {"einfacheFahrt": {"standard": pos("Flexpreis", 45.6)}},
]}]}, {"angebotsSubCluster": [{"angebotsPositionen": [
    {"verbundAngebot": pos("EinzelTicket", 25.7)}, {"verbundAngebot": pos("KinderTicket", 8.1)}, {"aboAngebot": {}},
]}]}]}}
f = D.fares(recon)
check("Tarife 2. Klasse, je Verbund nur das erste Angebot", [x["name"] for x in f] == ["Super Sparpreis", "Flexpreis", "EinzelTicket"], f)
ride = {"typ": "FAHRZEUG", "mitteltext": "RE 2", "kurztext": "RE", "abschnittsDauer": 2520,
        "halte": [{"abgangsDatum": "2026-11-05T09:41:00+01:00", "ezAbgangsDatum": "2026-11-05T09:48:00+01:00",
                   "gleis": "3", "ezGleis": "4", "ort": {"name": "Osnabrück Hbf"}},
                  {"ankunftsDatum": "2026-11-05T10:23:00+01:00", "ort": {"name": "Münster(Westf)Hbf"},
                   "echtzeitNotizen": [{"text": "Halt entfällt"}]}],
        "himNotizen": [{"prioritaet": "HOCH", "text": "Bauarbeiten"}]}
c = D.compact({"verbindung": {"reiseDauer": 2520, "umstiegeAnzahl": 0, "verbindungsAbschnitte": [ride]}, "angebote": {}})
l = c["legs"][0]
check("Echtzeit: Verspätung, Gleis, Ausfall, Meldung", l.get("departure_delay_min") == 7 and l.get("platform_change") == "Gleis 4 statt 3"
      and c.get("cancelled") and l.get("notes") == ["Bauarbeiten"], l)

w = D.connection_warnings([
    {"line": "RE 2", "from": "A", "to": "B", "departure": "2026-11-05 09:00", "arrival": "2026-11-05 10:00", "arrival_delay_min": 12},
    {"line": "Fußweg", "from": "B", "to": "B Gl. 11", "minutes": 5},
    {"line": "ICE 106", "from": "B Gl. 11", "to": "C", "departure": "2026-11-05 10:10", "arrival": "2026-11-05 12:00", "cancelled": True},
])
check("Anschluss gefährdet und Ausfall", len(w) == 2 and "gefährdet: -2 min statt 5 min" in w[1], w)

print(f"\n{'Alles ok' if not failed else f'{failed} Fehler'}")
sys.exit(1 if failed else 0)
