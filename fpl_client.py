"""
Shared client for the official (unofficial/undocumented) Fantasy Premier League API.

The FPL API has no formal documentation and can change without notice — this is
true of every FPL tool, including the two reference repos this project was
compared against. This client is defensive about that: it never assumes a field
exists without checking, and every function that depends on a field which might
be renamed by FPL is annotated with a comment saying so.

Endpoints used (all read-only, all public, no login/credentials required):
  - /api/bootstrap-static/           -> players, teams, gameweeks, positions
  - /api/fixtures/                   -> full fixture list with FDR
  - /api/element-summary/{id}/       -> one player's per-gameweek history + upcoming fixtures
  - /api/entry/{team_id}/            -> a manager's public profile
  - /api/entry/{team_id}/event/{gw}/picks/  -> a manager's picks for one gameweek (public)
  - /api/entry/{team_id}/history/    -> a manager's season history + chip log

No FPL email/password is ever required for anything in this project. If a tool
ever asks you for your FPL login, that is a different, unrelated feature — we
deliberately do not implement it, because there is no reason our analysis needs
your credentials.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

BASE_URL = "https://fantasy.premierleague.com/api"
# A browser-like UA: the FPL API has at times rejected bare/bot-looking agents with 403.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 fpl-strategy-mcp/1.1"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
DEFAULT_TIMEOUT = 30.0  # was 15s — too tight for a cold Render dyno + a multi-MB bootstrap fetch
RETRIES = 2
CACHE_TTL_SECONDS = 4 * 60 * 60  # 4 hours — bootstrap/fixtures data doesn't change that often

# Position id -> name, from element_types in bootstrap-static (stable for years)
POSITION_NAMES = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
POSITION_DEFCON_THRESHOLD = {
    "DEF": 10,  # CBIT
    "MID": 12,  # CBIRT
    "FWD": 12,  # CBIRT
}


class FPLAPIError(Exception):
    """Raised when the FPL API returns something we can't use, with a hint on what to try next."""


class _TTLCache:
    """Minimal in-memory TTL cache. One process's lifetime only — fine for a local MCP server."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._store[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        self._store.clear()

    def stats(self) -> dict:
        now = time.time()
        return {k: round(exp - now) for k, (exp, _) in self._store.items()}


# ONE cache for the whole process. Previously each tool call built a fresh FPLClient
# with its own empty cache, so the "4-hour cache" never actually cached anything —
# every call re-downloaded the multi-MB bootstrap-static payload. On a free-tier host
# that is both slow and a plausible source of tool timeouts.
SHARED_CACHE = _TTLCache()


class FPLClient:
    """Async client for the public FPL API with basic caching and defensive parsing."""

    def __init__(self, cache: Optional[_TTLCache] = None) -> None:
        self._cache = cache if cache is not None else SHARED_CACHE
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "FPLClient":
        self._http = httpx.AsyncClient(
            headers=HEADERS, timeout=DEFAULT_TIMEOUT
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http is not None:
            await self._http.aclose()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            # Allows use outside the `async with` pattern too (FastMCP tools call per-request)
            self._http = httpx.AsyncClient(headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        return self._http

    async def _get(self, path: str, cache_ttl: Optional[float] = CACHE_TTL_SECONDS) -> Any:
        cache_key = path
        if cache_ttl:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        last_exc: Optional[Exception] = None
        resp = None
        for attempt in range(RETRIES + 1):
            try:
                resp = await self._client().get(f"{BASE_URL}{path}")
            except httpx.TimeoutException as e:
                last_exc = FPLAPIError(
                    f"Timed out reaching the FPL API at {path} after {DEFAULT_TIMEOUT:.0f}s. "
                    "It may be under load around deadline time — retry in a moment."
                )
                continue
            except httpx.ConnectError as e:
                last_exc = FPLAPIError(f"Could not reach the FPL API ({path}). Check network connectivity.")
                continue
            except httpx.HTTPError as e:  # RemoteProtocolError, ReadError, etc.
                last_exc = FPLAPIError(f"HTTP transport error for {path}: {type(e).__name__}: {e}")
                continue
            if resp.status_code >= 500 and attempt < RETRIES:
                last_exc = FPLAPIError(f"FPL API returned HTTP {resp.status_code} for {path}.")
                continue
            break
        if resp is None:
            raise last_exc or FPLAPIError(f"Unknown failure fetching {path}.")

        if resp.status_code == 403:
            raise FPLAPIError(
                f"FPL API returned 403 Forbidden for {path}. This usually means the API is "
                "blocking the host's IP or User-Agent, not that the ID is wrong. Run "
                "check_live_api.py from a home connection to compare."
            )
        if resp.status_code == 404:
            raise FPLAPIError(f"Not found: {path}. Check that any IDs used are correct.")
        if resp.status_code == 429:
            raise FPLAPIError("Rate limited by the FPL API. Wait a minute before retrying.")
        if resp.status_code >= 400:
            raise FPLAPIError(f"FPL API returned HTTP {resp.status_code} for {path}.")

        try:
            data = resp.json()
        except ValueError as e:
            raise FPLAPIError(
                f"FPL API returned non-JSON content for {path} — the endpoint may have changed."
            ) from e

        if cache_ttl:
            self._cache.set(cache_key, data, cache_ttl)
        return data

    # ---- Core endpoints -------------------------------------------------

    async def bootstrap(self) -> dict:
        """Players, teams, gameweeks, positions. The foundation of everything else."""
        return await self._get("/bootstrap-static/")

    async def fixtures(self, event: Optional[int] = None) -> list[dict]:
        """All fixtures, optionally filtered server-side to one gameweek."""
        path = "/fixtures/" if event is None else f"/fixtures/?event={event}"
        data = await self._get(path)
        if not isinstance(data, list):
            raise FPLAPIError("Unexpected fixtures response shape from the FPL API.")
        return data

    async def player_summary(self, player_id: int) -> dict:
        """One player's full gameweek-by-gameweek history + upcoming fixtures."""
        return await self._get(f"/element-summary/{player_id}/")

    async def entry(self, team_id: int) -> dict:
        """A manager's public profile (name, overall rank, overall points, etc.)."""
        return await self._get(f"/entry/{team_id}/", cache_ttl=60 * 30)

    async def entry_picks(self, team_id: int, event: int) -> dict:
        """A manager's squad + captaincy for one specific gameweek. Public, no login needed."""
        return await self._get(f"/entry/{team_id}/event/{event}/picks/", cache_ttl=60 * 30)

    async def entry_history(self, team_id: int) -> dict:
        """A manager's full season history, past seasons, and chip usage log."""
        return await self._get(f"/entry/{team_id}/history/", cache_ttl=60 * 30)

    # ---- Derived lookups --------------------------------------------------

    async def teams_by_id(self) -> dict[int, dict]:
        boot = await self.bootstrap()
        return {t["id"]: t for t in boot.get("teams", [])}

    async def players_by_id(self) -> dict[int, dict]:
        boot = await self.bootstrap()
        return {p["id"]: p for p in boot.get("elements", [])}

    async def current_event_id(self) -> Optional[int]:
        boot = await self.bootstrap()
        for event in boot.get("events", []):
            if event.get("is_current"):
                return event["id"]
        # Fallback: next event that hasn't happened yet
        for event in boot.get("events", []):
            if event.get("is_next"):
                return event["id"]
        return None


def position_name(element_type: int) -> str:
    return POSITION_NAMES.get(element_type, f"UNKNOWN({element_type})")


def price(now_cost: int) -> float:
    """FPL stores price *10 as an integer (e.g. 125 == £12.5m)."""
    return round(now_cost / 10, 1)
