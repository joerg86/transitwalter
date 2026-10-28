"""GPX-Dateien und Kartenseiten für Fußwege (und ganze Verbindungen).

Dateien liegen unter DATA_DIR/exports und werden über /x/<id>.gpx bzw. /x/<id> ausgeliefert.
Die IDs sind lang und zufällig (wie ein geteilter Link), deshalb ohne Bearer-Token erreichbar.
GPX lässt sich in Komoot (Planen → GPX importieren), OsmAnd, Outdooractive usw. öffnen.
"""

import html
import json
import secrets
import time
from pathlib import Path
from xml.sax.saxutils import escape

KEEP_DAYS = 90
_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def decode_polyline(points: str, precision: int = 6) -> list[tuple[float, float]]:
    """Google-Polyline (MOTIS nutzt Genauigkeit 6) → [(lat, lon), …]."""
    coords, index, lat, lon, factor = [], 0, 0, 0, 10 ** precision
    while index < len(points):
        for is_lon in (False, True):
            result, shift = 0, 0
            while True:
                b = ord(points[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lat / factor, lon / factor))
    return coords


def leg_points(leg: dict) -> list[tuple[float, float]]:
    geo = leg.get("legGeometry") or {}
    if geo.get("points"):
        return decode_polyline(geo["points"], int(geo.get("precision", 6)))
    return [(leg["from"]["lat"], leg["from"]["lon"]), (leg["to"]["lat"], leg["to"]["lon"])]


def leg_title(leg: dict) -> str:
    f, t = leg["from"].get("name") or "Start", leg["to"].get("name") or "Ziel"
    if leg["mode"] == "WALK":
        return f"Fußweg {f} → {t} ({round((leg.get('distance') or 0) / 1000, 1)} km, {round(leg['duration'] / 60)} min)"
    return f"{leg.get('displayName') or leg['mode']} {f} → {t}"


def gpx(title: str, legs: list[dict]) -> str:
    tracks = []
    for leg in legs:
        pts = "".join(f'<trkpt lat="{la:.6f}" lon="{lo:.6f}"/>' for la, lo in leg_points(leg))
        tracks.append(f"<trk><name>{escape(leg_title(leg))}</name><type>{escape(leg['mode'].lower())}</type>"
                      f"<trkseg>{pts}</trkseg></trk>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="transitwalter" xmlns="http://www.topografix.com/GPX/1/1">'
            f"<metadata><name>{escape(title)}</name>"
            "<copyright author=\"OpenStreetMap-Mitwirkende\"><license>https://opendatacommons.org/licenses/odbl/</license></copyright>"
            f"</metadata>{''.join(tracks)}</gpx>\n")


def geojson(legs: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"title": leg_title(leg), "mode": leg["mode"]},
         "geometry": {"type": "LineString", "coordinates": [[lo, la] for la, lo in leg_points(leg)]}}
        for leg in legs
    ]}


MAP_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body{{margin:0;height:100%;font:14px system-ui,sans-serif}}#map{{position:absolute;inset:0}}
#box{{position:absolute;z-index:1000;top:10px;left:50px;right:10px;max-width:520px;background:#fff;padding:8px 12px;
border-radius:8px;box-shadow:0 1px 4px #0004}}#box a{{margin-right:12px}}</style></head><body>
<div id="map"></div><div id="box"><b>{title}</b><br><a href="{gpx}" download>GPX herunterladen</a>
<small>für Komoot: Planen → GPX importieren</small></div>
<script>
const data = {data};
const map = L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-Mitwirkende'}}).addTo(map);
// erst den Ausschnitt setzen, dann die Linien einfügen – sonst zeichnet Leaflet sie nicht
const layer = L.geoJSON(data, {{
  style: f => f.properties.mode === 'WALK' ? {{color: '#d62728', weight: 5, dashArray: '6 6'}} : {{color: '#1f77b4', weight: 4}},
  onEachFeature: (f, l) => l.bindPopup(f.properties.title)
}});
map.fitBounds(layer.getBounds(), {{padding: [40, 40], maxZoom: 17}});
layer.addTo(map);
</script></body></html>
"""


class Exports:
    def __init__(self, data_dir: str, base_url: str):
        self.dir = Path(data_dir) / "exports"
        self.base_url = base_url.rstrip("/")

    def save(self, title: str, legs: list[dict]) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cleanup()
        eid = secrets.token_urlsafe(16)
        (self.dir / f"{eid}.gpx").write_text(gpx(title, legs), encoding="utf-8")
        page = MAP_PAGE.format(title=html.escape(title), gpx=f"{eid}.gpx",
                               data=json.dumps(geojson(legs)).replace("</", "<\\/"))
        (self.dir / f"{eid}.html").write_text(page, encoding="utf-8")
        return {"map_url": f"{self.base_url}/x/{eid}", "gpx_url": f"{self.base_url}/x/{eid}.gpx",
                "expires_days": KEEP_DAYS}

    def load(self, name: str) -> tuple[bytes, str] | None:
        """Pfad '/x/<id>' oder '/x/<id>.gpx' → (Inhalt, Content-Type)."""
        eid, gpx_file = (name[:-4], True) if name.endswith(".gpx") else (name, False)
        if not eid or not set(eid) <= _ID_CHARS:
            return None
        path = self.dir / f"{eid}.{'gpx' if gpx_file else 'html'}"
        if not path.is_file():
            return None
        if gpx_file:
            return path.read_bytes(), "application/gpx+xml"
        return path.read_bytes(), "text/html; charset=utf-8"

    def _cleanup(self) -> None:
        limit = time.time() - KEEP_DAYS * 86400
        for p in self.dir.iterdir():
            if p.stat().st_mtime < limit:
                p.unlink(missing_ok=True)
