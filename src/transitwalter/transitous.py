"""Client für die öffentliche MOTIS-API von Transitous (api.transitous.org).

Nutzungsregeln: https://transitous.org/api/ – User-Agent mit Kontakt, wenig Last,
nicht-kommerziell. Deshalb Cache und ein eigenes Anfragelimit pro Minute.
"""

import asyncio
import json
import time
from collections import OrderedDict, deque

import httpx

from . import errors as E
from .errors import TransitError

ATTRIBUTION = "Fahrplandaten: Transitous (transitous.org) und deren Quellen, Karten/Wege: © OpenStreetMap-Mitwirkende"


class Transitous:
    def __init__(self, base_url: str, user_agent: str, per_minute: int = 20, cache_size: int = 500):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.per_minute = per_minute
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._cache_size = cache_size
        self._http: httpx.AsyncClient | None = None
        self.requests = 0

    async def get(self, path: str, params: dict, ttl: int = 600):
        """GET mit Cache. Listen in params werden kommagetrennt, None-Werte weggelassen."""
        if not self.user_agent:
            raise TransitError(E.CONFIG_ERROR, "TRANSITOUS_USER_AGENT fehlt (App-Name, Version und Kontakt)")
        query = {k: ",".join(map(str, v)) if isinstance(v, (list, tuple)) else _str(v)
                 for k, v in params.items() if v is not None}
        key = path + "?" + json.dumps(query, sort_keys=True)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            self._cache.move_to_end(key)
            return hit[1]
        await self._throttle()
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=60, headers={"User-Agent": self.user_agent})
        try:
            resp = await self._http.get(f"{self.base_url}{path}", params=query)
        except httpx.HTTPError as e:
            raise TransitError(E.UPSTREAM_ERROR, f"Transitous nicht erreichbar ({type(e).__name__})")
        self.requests += 1
        if resp.status_code == 429:
            raise TransitError(E.RATE_LIMITED, "Transitous drosselt gerade – später erneut versuchen")
        if resp.status_code in (400, 422):
            raise TransitError(E.INVALID_ARGUMENT, f"Transitous lehnt die Anfrage ab: {_detail(resp)}")
        if resp.status_code == 404:
            raise TransitError(E.PLACE_NOT_FOUND, f"Nicht gefunden: {_detail(resp)}")
        if resp.status_code >= 400:
            raise TransitError(E.UPSTREAM_ERROR, f"Transitous antwortet mit HTTP {resp.status_code}")
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
                raise TransitError(E.RATE_LIMITED, f"Eigenes Limit von {self.per_minute} Anfragen pro Minute erreicht")
            self._sent.append(now)

    # ---------- Endpunkte ----------

    async def geocode(self, text: str, type: str | None = None, near: tuple[float, float] | None = None,
                      language: str = "de") -> list[dict]:
        return await self.get("/api/v1/geocode", {
            "text": text, "type": type, "language": language,
            "place": f"{near[0]},{near[1]}" if near else None,
        }, ttl=86400)

    async def reverse_geocode(self, lat: float, lon: float) -> list[dict]:
        return await self.get("/api/v1/reverse-geocode", {"place": f"{lat},{lon}"}, ttl=86400)

    async def stops_in_box(self, lat: float, lon: float, delta: float) -> list[dict]:
        return await self.get("/api/v6/map/stops", {
            "min": f"{lat - delta},{lon - delta}", "max": f"{lat + delta},{lon + delta}",
        }, ttl=86400)

    async def plan(self, **params) -> dict:
        return await self.get("/api/v6/plan", params)

    async def stoptimes(self, **params) -> dict:
        return await self.get("/api/v6/stoptimes", params, ttl=300)


def _str(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("error") or body.get("message") or body)[:200]
    except ValueError:
        return resp.text[:200] or f"HTTP {resp.status_code}"
