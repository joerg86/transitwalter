"""GPX-Dateien und Kartenseiten für Fußwege (und ganze Verbindungen).

Dateien liegen unter DATA_DIR/exports und werden über /x/<id> (Karte), /x/<id>.gpx (alles) und
/x/<id>.w<n>.gpx (einzelner Fußweg, zum Navigieren) ausgeliefert.
Die IDs sind lang und zufällig (wie ein geteilter Link), deshalb ohne Bearer-Token erreichbar.
GPX lässt sich in Komoot (Planen → GPX importieren), OsmAnd, Outdooractive usw. öffnen.
"""

import html
import json
import secrets
import re
import time
import unicodedata
from pathlib import Path
from xml.sax.saxutils import escape

KEEP_DAYS = 90
_FILE = re.compile(r"^([A-Za-z0-9_-]{16,32})(?:\.(w\d{1,2}))?(\.gpx)?$")
MIN_WALK_S = 180  # kürzere Fußwege (Umstieg am Bahnsteig) bekommen keine eigene Datei


def slug(text: str) -> str:
    """'Fußweg Holzwickede, Bahnhof → Dortmund Airport' → 'fussweg-holzwickede-bahnhof-dortmund-airport'."""
    text = unicodedata.normalize("NFKD", text.replace("ß", "ss")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:80] or "route"


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


def walk_name(leg: dict) -> str:
    return f"Fußweg {leg['from'].get('name') or 'Start'} → {leg['to'].get('name') or 'Ziel'}"


def gpx(title: str, legs: list[dict]) -> str:
    """Ein Track pro Abschnitt; Fußwege bekommen Start und Ziel zusätzlich als Wegpunkte."""
    tracks, wpts = [], []
    for leg in legs:
        if leg["mode"] == "WALK":
            for side in ("from", "to"):
                p = leg[side]
                if p.get("lat") is None:
                    continue
                wpts.append(f'<wpt lat="{p["lat"]:.6f}" lon="{p["lon"]:.6f}"><name>{escape(p.get("name") or side)}</name></wpt>')
        pts = "".join(f'<trkpt lat="{la:.6f}" lon="{lo:.6f}"/>' for la, lo in leg_points(leg))
        tracks.append(f"<trk><name>{escape(leg_title(leg))}</name><type>{escape(leg['mode'].lower())}</type>"
                      f"<trkseg>{pts}</trkseg></trk>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="transitwalter" xmlns="http://www.topografix.com/GPX/1/1">'
            f"<metadata><name>{escape(title)}</name>"
            "<copyright author=\"OpenStreetMap-Mitwirkende\"><license>https://opendatacommons.org/licenses/odbl/</license></copyright>"
            f"</metadata>{''.join(dict.fromkeys(wpts))}{''.join(tracks)}</gpx>\n")


def geojson(legs: list[dict], walk_files: dict[int, str]) -> dict:
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"i": i, "title": leg_title(leg), "mode": leg["mode"], "gpx": walk_files.get(i)},
         "geometry": {"type": "LineString", "coordinates": [[lo, la] for la, lo in leg_points(leg)]}}
        for i, leg in enumerate(legs)
    ]}


MAP_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body{{margin:0;height:100%;font:14px system-ui,sans-serif}}#map{{position:absolute;inset:0}}
#box{{position:absolute;z-index:1000;top:10px;left:50px;right:10px;max-width:520px;background:#fff;padding:8px 12px;
border-radius:8px;box-shadow:0 1px 4px #0004}}#box a{{margin-right:12px}}#box ol{{margin:6px 0;padding-left:20px}}
#box li{{margin:3px 0}}#box li a.zoom{{margin:0;color:inherit}}</style></head><body>
<div id="map"></div><div id="box"><b>{title}</b>{walks}<a href="{gpx}" download>Alles als GPX</a>
<a href="#" id="all">Gesamtansicht</a><small>für Komoot: Planen → GPX importieren</small></div>
<script>
const data = {data};
const map = L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-Mitwirkende'}}).addTo(map);
// erst den Ausschnitt setzen, dann die Linien einfügen – sonst zeichnet Leaflet sie nicht
const style = f => f.properties.mode === 'WALK' ? {{color: '#d62728', weight: 5, dashArray: '6 6'}} : {{color: '#1f77b4', weight: 4}};
const parts = {{}};
const hits = L.layerGroup();
const layer = L.geoJSON(data, {{
  style,
  onEachFeature: (f, l) => {{
    parts[f.properties.i] = l;
    l.bindPopup(f.properties.title +
      (f.properties.gpx ? `<br><a href="${{f.properties.gpx}}" download>GPX für diesen Fußweg</a>` : ''));
    // dünne, gestrichelte Linien sind schwer zu treffen (in den Lücken gar nicht) –
    // darunter liegt eine unsichtbare, breite Klickfläche
    const hit = L.polyline(l.getLatLngs(), {{weight: 24, opacity: 0}}).addTo(hits);
    for (const t of [l, hit]) {{
      t.on('click', () => zoomTo(f.properties.i, true));
      t.on('mouseover', () => l.setStyle({{weight: 9}}));
      t.on('mouseout', () => l.setStyle(style(f)));
    }}
  }}
}});
// Abstand oben so groß wie der Infokasten, damit nichts darunter verschwindet
const pad = () => ({{paddingTopLeft: [40, document.getElementById('box').offsetHeight + 30], paddingBottomRight: [40, 40]}});
// Klick auf Linie oder Listeneintrag: auf diesen Abschnitt zoomen
function zoomTo(i, popup) {{
  touched = true;
  map.fitBounds(parts[i].getBounds(), {{...pad(), maxZoom: 18}});
  if (popup) map.once('moveend', () => parts[i].openPopup());
}}
document.querySelectorAll('a.zoom').forEach(a => a.addEventListener('click', e => {{
  e.preventDefault(); zoomTo(+a.dataset.leg, true);
}}));
document.getElementById('all').addEventListener('click', e => {{ e.preventDefault(); touched = false; fit(); }});
// Ausschnitt neu berechnen, solange niemand selbst zoomt – beim ersten Aufruf ist das Fenster evtl. noch 0×0
let touched = false;
const fit = () => {{ if (touched) return; map.invalidateSize(); map.fitBounds(layer.getBounds(), {{...pad(), maxZoom: 17}}); }};
fit();
layer.addTo(map);
hits.addTo(map);
map.on('mousedown touchstart wheel', () => {{ touched = true; }});
window.addEventListener('load', fit);
window.addEventListener('resize', fit);
setTimeout(fit, 300);
</script></body></html>
"""


class Exports:
    def __init__(self, data_dir: str, base_url: str):
        self.dir = Path(data_dir) / "exports"
        self.base_url = base_url.rstrip("/")

    def save(self, title: str, legs: list[dict]) -> dict:
        """Karte + GPX für alles + ein GPX pro Fußweg. Liefert die URLs."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cleanup()
        eid = secrets.token_urlsafe(16)
        names = {f"{eid}.gpx": f"{slug(title)}.gpx"}
        (self.dir / f"{eid}.gpx").write_text(gpx(title, legs), encoding="utf-8")
        walks, walk_files, items = [], {}, []
        for i, leg in enumerate(legs):
            if leg["mode"] != "WALK" or (leg["duration"] < MIN_WALK_S and len(legs) > 1):
                continue
            n = len(walks) + 1
            file, name = f"{eid}.w{n}.gpx", walk_name(leg)
            (self.dir / file).write_text(gpx(name, [leg]), encoding="utf-8")
            names[file] = f"{slug(name)}.gpx"
            walk_files[i] = file
            km, minutes = round((leg.get("distance") or 0) / 1000, 1), round(leg["duration"] / 60)
            walks.append({"name": name, "distance_km": km, "minutes": minutes, "gpx_url": f"{self.base_url}/x/{file}"})
            items.append(f'<li><a href="#" class="zoom" data-leg="{i}" title="Auf diesen Fußweg zoomen">'
                         f'{html.escape(name)}</a> ({km} km, {minutes} min) – <a href="{file}" download>GPX</a></li>')
        (self.dir / f"{eid}.json").write_text(json.dumps(names), encoding="utf-8")
        page = MAP_PAGE.format(title=html.escape(title), gpx=f"{eid}.gpx",
                               walks=f"<ol>{''.join(items)}</ol>" if items else "<br>",
                               data=json.dumps(geojson(legs, walk_files)).replace("</", "<\\/"))
        (self.dir / f"{eid}.html").write_text(page, encoding="utf-8")
        return {"map_url": f"{self.base_url}/x/{eid}", "gpx_url": f"{self.base_url}/x/{eid}.gpx",
                "walks": walks, "expires_days": KEEP_DAYS}

    def load(self, name: str) -> tuple[bytes, str, str | None] | None:
        """'/x/<id>', '/x/<id>.gpx' oder '/x/<id>.w<n>.gpx' → (Inhalt, Content-Type, Dateiname zum Download)."""
        m = _FILE.match(name)
        if not m or (m[2] and not m[3]):
            return None
        eid, file = m[1], name if m[3] else f"{m[1]}.html"
        path = self.dir / file
        if not path.is_file():
            return None
        if not m[3]:
            return path.read_bytes(), "text/html; charset=utf-8", None
        meta = self.dir / f"{eid}.json"
        names = json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}
        return path.read_bytes(), "application/gpx+xml", names.get(file, "transitwalter.gpx")

    def _cleanup(self) -> None:
        limit = time.time() - KEEP_DAYS * 86400
        for p in self.dir.iterdir():
            if p.stat().st_mtime < limit:
                p.unlink(missing_ok=True)
