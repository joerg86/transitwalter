"""Smoke-Test gegen einen laufenden Server: ruft jedes Tool über HTTP auf und prüft Fehlercodes.

    uv run --env-file .env python tests/smoke.py [URL]     (Standard: http://localhost:8000/mcp)

Liest MCP_TOKEN aus der Umgebung. Lädt zusätzlich die erzeugte Karte und GPX-Datei.
"""

import asyncio
import json
import os
import sys
from datetime import date, timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"
SOON = (date.today() + timedelta(days=2)).isoformat()

CALLS = [
    ("transit_place_search", {"query": "Purekkari neem"}),
    ("transit_place_search", {"query": "Aegviidu", "type": "stop"}),
    ("transit_airport", {"code": "NCE"}),
    ("transit_journey_plan", {"origin": "Osnabrück Hbf", "destination": "DTM",
                              "time": f"{SOON}T14:40", "arrive_by": True, "transport": "regional", "results": 2}),
    ("transit_journey_plan", {"origin": "LTN", "destination": "Ivinghoe Beacon", "time": f"{SOON}T09:00", "results": 1}),
    ("transit_walk_route", {"origin": "Holzwickede Bahnhof", "destination": "DTM"}),
    ("transit_departures", {"stop": "Aegviidu", "time": f"{SOON}T16:00", "count": 3}),
    ("transit_db_prices", {"origin": "Osnabrück Hbf", "destination": "Berlin Hbf", "time": f"{SOON}T09:00", "results": 2}),
    ("transit_export", None),  # itinerary_id kommt aus transit_journey_plan
    ("transit_gpx", None),     # gpx_url des ersten Fußwegs aus transit_export
]

# (Tool, Argumente, erwarteter Code)
ERRORS = [
    ("transit_airport", {"code": "XQZ"}, "AIRPORT_NOT_FOUND"),
    ("transit_journey_plan", {"origin": "Berlin", "destination": "Hamburg", "time": "morgen"}, "INVALID_ARGUMENT"),
    ("transit_journey_plan", {"origin": "Berlin", "destination": "Hamburg", "max_walk_minutes": 500}, "INVALID_ARGUMENT"),
    ("transit_export", {"itinerary_id": "gibtsnicht"}, "EXPORT_NOT_FOUND"),
    ("transit_gpx", {"file": "../../etc/passwd"}, "EXPORT_NOT_FOUND"),
    ("transit_db_prices", {"origin": "Osnabrück Hbf", "destination": "Berlin Hbf", "results": 9}, "INVALID_ARGUMENT"),
]


async def main() -> int:
    token = os.environ["MCP_TOKEN"]
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    failed, itinerary_id, walk_gpx, urls = 0, None, None, []
    async with http, streamable_http_client(URL, http_client=http) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            print(f"{len(tools)} Tools: {', '.join(sorted(tools))}\n")
            for name, args in CALLS:
                if args is None and name == "transit_export":
                    args = {"itinerary_id": itinerary_id}
                elif args is None:
                    args = {"file": walk_gpx}
                r = await session.call_tool(name, args)
                data = r.structured_content or (json.loads(r.content[0].text) if not r.is_error else {})
                if name == "transit_journey_plan" and data.get("itineraries") and not itinerary_id:
                    itinerary_id = data["itineraries"][0]["id"]
                if data.get("map_url"):
                    urls += [data["map_url"], data["gpx_url"]]
                if name == "transit_export" and data.get("walks"):
                    walk_gpx = data["walks"][0]["gpx_url"]
                    urls.append(walk_gpx)
                if name == "transit_gpx":
                    data = data if "<gpx" in data.get("gpx", "") else {}
                ok = not r.is_error and bool(data)
                failed += not ok
                text = r.content[0].text if r.content else ""
                print(f"{'ok    ' if ok else 'FEHLER'} {name:22} {len(text):6} Zeichen | {text[:90]!r}")
            for name, args, expected in ERRORS:
                r = await session.call_tool(name, args)
                code = (r.structured_content or {}).get("error", {}).get("code")
                ok = r.is_error and code == expected
                failed += not ok
                print(f"{'ok    ' if ok else 'FEHLER'} {name:22} erwartet {expected:18} bekommen {code}")
    base = URL.rsplit("/mcp", 1)[0]
    async with httpx.AsyncClient() as c:
        for url in urls:
            # Exporte sind ohne Token erreichbar; lokal zeigt PUBLIC_BASE_URL evtl. woanders hin
            r = await c.get(base + url[url.index("/x/"):])
            ok = r.status_code == 200 and (b"<gpx" in r.content or b"leaflet" in r.content)
            failed += not ok
            print(f"{'ok    ' if ok else 'FEHLER'} Export {url[url.index('/x/'):]:40} HTTP {r.status_code}, {len(r.content)} Bytes")
        r = await c.post(URL, json={})
        ok = r.status_code == 401
        failed += not ok
        print(f"{'ok    ' if ok else 'FEHLER'} ohne Token → HTTP {r.status_code}")
    print(f"\n{'Alles ok' if not failed else f'{failed} Fehler'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
