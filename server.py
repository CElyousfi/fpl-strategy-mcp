"""
fpl_strategy_mcp — a custom Fantasy Premier League MCP server built for one specific
purpose: operationalizing our own tiered, data-driven squad-building strategy, not
generic "look up a player" convenience.

Every tool here maps to a named section of our strategy documents. If a tool doesn't
serve one of those sections, it doesn't belong in this file.

Run locally (stdio, for Claude Desktop / Claude Code):
    python server.py

No FPL account, email, or password is ever required — everything here reads public,
unauthenticated endpoints only.
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from mcp.server.mcpserver import MCPServer

from fpl_client import SHARED_CACHE, FPLClient, FPLAPIError, position_name, price
from analysis import (
    upcoming_fixtures,
    DefconFieldsNotFound,
    classify_tier,
    defcon_hit_rate,
    find_blank_double_gameweeks,
    hit_math_check,
    rolling_fixture_score,
)

mcp = MCPServer("fpl_strategy_mcp")

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,  # hits a live external API
}
CALCULATOR = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,  # pure computation, no network call
}


def _err(e: Exception) -> str:
    """Consistent, actionable error formatting across every tool."""
    if isinstance(e, FPLAPIError):
        return json.dumps({"error": str(e)}, indent=2)
    if isinstance(e, DefconFieldsNotFound):
        return json.dumps({"error": str(e), "available_keys": e.available_keys}, indent=2)
    return json.dumps({"error": f"Unexpected error: {type(e).__name__}: {e}"}, indent=2)


# ---------------------------------------------------------------------------
# 1. fpl_search_players — foundational data access with real filtering
# ---------------------------------------------------------------------------

class SearchPlayersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name_contains: Optional[str] = Field(
        default=None, description="Case-insensitive substring match on web_name, e.g. 'Haaland'"
    )
    team_name_contains: Optional[str] = Field(
        default=None, description="Case-insensitive substring match on team name, e.g. 'Arsenal'"
    )
    position: Optional[str] = Field(
        default=None, description="One of GK, DEF, MID, FWD. Omit for all positions."
    )
    max_price: Optional[float] = Field(
        default=None, description="Maximum price in £m, e.g. 6.5", ge=3.5, le=16.0
    )
    min_price: Optional[float] = Field(
        default=None, description="Minimum price in £m, e.g. 4.0", ge=3.5, le=16.0
    )
    min_ownership_pct: Optional[float] = Field(
        default=None, description="Minimum selected_by_percent, e.g. 20.0", ge=0, le=100
    )
    max_ownership_pct: Optional[float] = Field(
        default=None,
        description="Maximum selected_by_percent — set low (e.g. 10) to surface differentials",
        ge=0, le=100,
    )
    limit: int = Field(default=25, description="Max rows to return", ge=1, le=100)


@mcp.tool(
    name="fpl_search_players",
    annotations={"title": "Search FPL Players", **READ_ONLY},
)
async def fpl_search_players(params: SearchPlayersInput) -> str:
    """Search and filter the full FPL player pool by name, team, position, price and
    ownership. This is the foundational lookup tool — use it first to find player IDs
    for the more specialized tools below (defcon profile, fixture score, tier).

    Returns JSON: {"count": int, "players": [{id, name, team, position, price,
    ownership_pct, form, total_points, status, news}]}
    """
    try:
        async with FPLClient() as client:
            boot = await client.bootstrap()
            teams = {t["id"]: t["name"] for t in boot["teams"]}
            results = []
            for el in boot["elements"]:
                pos = position_name(el["element_type"])
                if params.position and pos != params.position.upper():
                    continue
                if params.name_contains and params.name_contains.lower() not in el["web_name"].lower():
                    continue
                team_name = teams.get(el["team"], "")
                if params.team_name_contains and params.team_name_contains.lower() not in team_name.lower():
                    continue
                p = price(el["now_cost"])
                if params.max_price is not None and p > params.max_price:
                    continue
                if params.min_price is not None and p < params.min_price:
                    continue
                own = float(el.get("selected_by_percent", 0) or 0)
                if params.min_ownership_pct is not None and own < params.min_ownership_pct:
                    continue
                if params.max_ownership_pct is not None and own > params.max_ownership_pct:
                    continue
                results.append({
                    "id": el["id"],
                    "name": el["web_name"],
                    "team": team_name,
                    "position": pos,
                    "price": p,
                    "ownership_pct": own,
                    "form": el.get("form"),
                    "total_points": el.get("total_points"),
                    "status": el.get("status"),  # 'a'=available, 'd'=doubtful, 'i'=injured, 's'=suspended, 'u'=unavailable
                    "news": el.get("news") or None,
                })
            results.sort(key=lambda r: r["total_points"] or 0, reverse=True)
            return json.dumps({"count": len(results), "players": results[:params.limit]}, indent=2)
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 2. fpl_defcon_profile — Framework Layer 1.1
# ---------------------------------------------------------------------------

class DefconProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    player_id: int = Field(..., description="FPL element id, from fpl_search_players", ge=1)


@mcp.tool(
    name="fpl_defcon_profile",
    annotations={"title": "DEFCON Threshold-Hit-Rate Profile", **READ_ONLY},
)
async def fpl_defcon_profile(params: DefconProfileInput) -> str:
    """Compute a player's defensive-contribution THRESHOLD-HIT-RATE (Framework Layer
    1.1) — the percentage of played matches where they crossed the CBIT (defenders,
    10+) or CBIRT (mid/fwd, 12+) line, not their raw season total. This is the metric
    that actually matters, since DEFCON is a capped threshold bonus, not a rate.

    Returns JSON with season hit-rate, last-5-match hit-rate, and per-match detail.
    If the underlying API field can't be found, returns a diagnostic error listing
    the actual fields available, rather than a silently wrong number.
    """
    try:
        async with FPLClient() as client:
            boot = await client.bootstrap()
            element = next((e for e in boot["elements"] if e["id"] == params.player_id), None)
            if element is None:
                return json.dumps({"error": f"No player with id {params.player_id}."}, indent=2)
            pos = position_name(element["element_type"])
            summary = await client.player_summary(params.player_id)
            history = summary.get("history", [])
            profile = defcon_hit_rate(history, pos)
            return json.dumps({
                "player_id": params.player_id,
                "name": element["web_name"],
                "position": pos,
                "threshold_required": profile.threshold,
                "matches_considered": profile.matches_considered,
                "matches_hit": profile.matches_hit,
                "season_hit_rate_pct": profile.hit_rate_pct,
                "last5_hit_rate_pct": profile.hit_rate_recent5_pct,
                "data_source": profile.data_source,
                "interpretation": (
                    f"{element['web_name']} hits the DEFCON threshold in "
                    f"{profile.hit_rate_pct}% of matches played this season."
                ),
            }, indent=2)
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 3. fpl_fixture_outlook — Framework Layer 3.3
# ---------------------------------------------------------------------------

class FixtureOutlookInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_name_contains: str = Field(..., description="Team name substring, e.g. 'Arsenal'")
    num_gameweeks: int = Field(default=6, description="Rolling window size", ge=1, le=15)


@mcp.tool(
    name="fpl_fixture_outlook",
    annotations={"title": "Rolling Fixture Difficulty Outlook", **READ_ONLY},
)
async def fpl_fixture_outlook(params: FixtureOutlookInput) -> str:
    """Weighted rolling-window fixture difficulty for a team (Framework Layer 3.3) —
    nearer fixtures weighted more heavily, home/away read correctly, plus a short-rest
    flag as a partial congestion proxy. Lower weighted_score = easier run.

    IMPORTANT: short_rest_flags only sees Premier League fixtures through this API.
    It cannot see domestic cup or European fixtures — cross-check those manually per
    the External Shock Monitoring section before concluding a team has a clean run.
    """
    try:
        async with FPLClient() as client:
            boot = await client.bootstrap()
            team = next(
                (t for t in boot["teams"]
                 if params.team_name_contains.lower() in t["name"].lower()), None
            )
            if team is None:
                return json.dumps({"error": f"No team matching '{params.team_name_contains}'."}, indent=2)
            fixtures = await client.fixtures()
            team_fixtures = [f for f in fixtures if f.get("team_h") == team["id"] or f.get("team_a") == team["id"]]
            next_event = await client.next_event_id()
            result = rolling_fixture_score(team_fixtures, team["id"], params.num_gameweeks, from_event=next_event)
            teams_by_id = {t["id"]: t["name"] for t in boot["teams"]}
            opponents = []
            for f in upcoming_fixtures(team_fixtures, next_event)[:params.num_gameweeks]:
                opp_id = f["team_a"] if f["team_h"] == team["id"] else f["team_h"]
                opponents.append(teams_by_id.get(opp_id, "?"))
            return json.dumps({
                "team": team["name"],
                "gameweeks": result.gameweeks_considered,
                "opponents": opponents,
                "home_away": result.home_away,
                "raw_difficulties": result.raw_difficulties,
                "weighted_score": result.weighted_score,
                "short_rest_flags": result.short_rest_flags,
                "note": result.note,
            }, indent=2)
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 4. fpl_tier_classifier — Build Strategy Section 2
# ---------------------------------------------------------------------------

class TierClassifierInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    player_id: int = Field(..., description="FPL element id", ge=1)


@mcp.tool(
    name="fpl_tier_classifier",
    annotations={"title": "Core/Value-Floor/Edge Tier Suggestion", **READ_ONLY},
)
async def fpl_tier_classifier(params: TierClassifierInput) -> str:
    """Suggest which of our three squad tiers (Core Anchor / Value Floor Candidate /
    Calculated Edge Candidate — Build Strategy Section 2) a player numerically
    resembles, based on price percentile within their position, ownership, and form.

    This is a starting point, NOT a verdict — a Calculated Edge Candidate result
    still has to independently pass the Edge Test (2 of 4 qualitative conditions)
    before it earns one of the reserved differential slots.
    """
    try:
        async with FPLClient() as client:
            boot = await client.bootstrap()
            element = next((e for e in boot["elements"] if e["id"] == params.player_id), None)
            if element is None:
                return json.dumps({"error": f"No player with id {params.player_id}."}, indent=2)
            same_pos = [e for e in boot["elements"] if e["element_type"] == element["element_type"]]
            suggestion = classify_tier(element, same_pos)
            return json.dumps({
                "player_id": params.player_id,
                "name": element["web_name"],
                "position": position_name(element["element_type"]),
                "suggested_tier": suggestion.suggested_tier,
                "reasoning": suggestion.reasoning,
                "price": suggestion.price,
                "ownership_pct": suggestion.ownership_pct,
                "form": suggestion.form,
                "next_step": (
                    "If 'Calculated Edge Candidate': run it through the Edge Test manually — "
                    "BPS-rule fit, verified role signal, new-manager information edge, or "
                    "data/price divergence. Needs 2 of 4 to qualify."
                ),
            }, indent=2)
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 5. fpl_hit_math — Build Strategy Section 4.3 (pure calculator, no API call)
# ---------------------------------------------------------------------------

class HitMathInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incoming_projected_points: float = Field(..., description="Your projected points for the player coming IN, over the horizon")
    outgoing_projected_points: float = Field(..., description="Your projected points for the player going OUT, over the same horizon")
    horizon_gameweeks: int = Field(..., description="How many gameweeks this projection covers", ge=1, le=10)
    hit_cost: int = Field(default=4, description="Points cost of the hit (4 for one extra transfer, 8 for two)", ge=4, le=16)


@mcp.tool(
    name="fpl_hit_math",
    annotations={"title": "Points-Hit Justification Check", **CALCULATOR},
)
async def fpl_hit_math(params: HitMathInput) -> str:
    """Mechanical test for whether a points hit is justified (Build Strategy Section
    4.3): the incoming player must beat the outgoing player's projection by MORE than
    the hit cost over the stated horizon. No network call — pure calculation, so you
    supply the projections (from fpl_fixture_outlook, fpl_defcon_profile, form, etc.).
    """
    result = hit_math_check(
        params.incoming_projected_points,
        params.outgoing_projected_points,
        params.horizon_gameweeks,
        params.hit_cost,
    )
    return json.dumps({
        "cleared": result.cleared,
        "point_differential": result.point_differential,
        "hit_cost": result.hit_cost,
        "margin": result.margin,
        "verdict": result.verdict,
    }, indent=2)


# ---------------------------------------------------------------------------
# 6. fpl_blank_double_gameweeks — chip timing support
# ---------------------------------------------------------------------------

class BlankDoubleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_gameweek: int = Field(..., description="Start of range to scan (inclusive)", ge=1, le=38)
    to_gameweek: int = Field(..., description="End of range to scan (inclusive)", ge=1, le=38)
    team_name_contains: Optional[str] = Field(
        default=None, description="Restrict to teams matching this substring; omit to scan all 20 teams"
    )


@mcp.tool(
    name="fpl_blank_double_gameweeks",
    annotations={"title": "Blank & Double Gameweek Finder", **READ_ONLY},
)
async def fpl_blank_double_gameweeks(params: BlankDoubleInput) -> str:
    """Scan a gameweek range for blanks (0 fixtures) and doubles (2+ fixtures) per
    team — critical input for Bench Boost / Free Hit / Triple Captain timing
    (Build Strategy Section 8). Early in a season this will mostly be empty; blanks
    and doubles are usually driven by cup-round scheduling that firms up over time.
    """
    try:
        async with FPLClient() as client:
            boot = await client.bootstrap()
            teams = boot["teams"]
            if params.team_name_contains:
                teams = [t for t in teams if params.team_name_contains.lower() in t["name"].lower()]
            team_ids = [t["id"] for t in teams]
            teams_by_id = {t["id"]: t["name"] for t in boot["teams"]}
            fixtures = await client.fixtures()
            event_range = list(range(params.from_gameweek, params.to_gameweek + 1))
            result = find_blank_double_gameweeks(fixtures, team_ids, event_range)
            return json.dumps({
                "blanks": [
                    {"gameweek": b.event, "team": teams_by_id.get(b.team_id, "?")}
                    for b in result["blanks"]
                ],
                "doubles": [
                    {"gameweek": d.event, "team": teams_by_id.get(d.team_id, "?"), "fixture_count": d.fixture_count}
                    for d in result["doubles"]
                ],
            }, indent=2)
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 7. fpl_get_team — public read of any manager's squad for a gameweek
# ---------------------------------------------------------------------------

class GetTeamInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_id: int = Field(..., description="FPL team/entry ID, visible in the URL on the FPL site once your team exists", ge=1)
    event: Optional[int] = Field(default=None, description="Gameweek number; omit for the current gameweek")


@mcp.tool(
    name="fpl_get_team",
    annotations={"title": "Get Manager's Team (public, no login)", **READ_ONLY},
)
async def fpl_get_team(params: GetTeamInput) -> str:
    """Read any manager's public squad, captaincy, and points for a gameweek — including
    ours, once we have a team ID. This uses FPL's public entry endpoints; no email or
    password is ever required or accepted by this tool.
    """
    try:
        async with FPLClient() as client:
            event = params.event
            if event is None:
                event = await client.current_event_id()
                if event is None:
                    return json.dumps({"error": "Could not determine the current gameweek."}, indent=2)
            entry = await client.entry(params.team_id)
            picks_data = await client.entry_picks(params.team_id, event)
            boot = await client.bootstrap()
            players_by_id = {e["id"]: e for e in boot["elements"]}

            # Chip log — needed for "which chips do we still have" without logging in.
            chips_used = []
            try:
                hist_data = await client.entry_history(params.team_id)
                chips_used = [
                    {"chip": c.get("name"), "gameweek": c.get("event")}
                    for c in hist_data.get("chips", [])
                ]
            except FPLAPIError:
                chips_used = ["unavailable"]

            picks = []
            for p in picks_data.get("picks", []):
                el = players_by_id.get(p["element"], {})
                picks.append({
                    "name": el.get("web_name", "?"),
                    "position": position_name(el.get("element_type", 0)),
                    "is_captain": p.get("is_captain", False),
                    "is_vice_captain": p.get("is_vice_captain", False),
                    "multiplier": p.get("multiplier"),
                    "on_bench": p.get("multiplier", 1) == 0,
                })
            hist = picks_data.get("entry_history", {})
            return json.dumps({
                "manager_name": f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip(),
                "team_name": entry.get("name"),
                "overall_rank": entry.get("summary_overall_rank"),
                "gameweek": event,
                "gameweek_points": hist.get("points"),
                "gameweek_rank": hist.get("rank"),
                "bank": hist.get("bank"),
                "team_value": hist.get("value"),
                "transfers_made": hist.get("event_transfers"),
                "transfer_cost": hist.get("event_transfers_cost"),
                "points_on_bench": hist.get("points_on_bench"),
                "active_chip": picks_data.get("active_chip"),
                "chips_used_this_season": chips_used,
                "picks": picks,
            }, indent=2)
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 8. fpl_price_ownership_trends — Framework Layers 1.3 + 2.1
# ---------------------------------------------------------------------------

class TrendsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: Optional[str] = Field(default=None, description="Filter to GK, DEF, MID, or FWD")
    limit: int = Field(default=15, description="How many rows per direction (risers/fallers)", ge=1, le=50)


@mcp.tool(
    name="fpl_price_ownership_trends",
    annotations={"title": "Price & Ownership Momentum", **READ_ONLY},
)
async def fpl_price_ownership_trends(params: TrendsInput) -> str:
    """Surface transfer momentum (transfers_in/out this event) and ownership levels —
    the raw inputs for Effective Ownership reasoning (Layer 2.1) and price-change
    timing (Layer 1.3). High transfers_in_event = a price rise is more likely soon;
    the reverse for transfers_out_event.
    """
    try:
        async with FPLClient() as client:
            boot = await client.bootstrap()
            elements = boot["elements"]
            if params.position:
                elements = [e for e in elements if position_name(e["element_type"]) == params.position.upper()]
            teams = {t["id"]: t["name"] for t in boot["teams"]}

            def row(e: dict) -> dict:
                return {
                    "name": e["web_name"],
                    "team": teams.get(e["team"], "?"),
                    "position": position_name(e["element_type"]),
                    "price": price(e["now_cost"]),
                    "ownership_pct": float(e.get("selected_by_percent", 0) or 0),
                    "transfers_in_event": e.get("transfers_in_event", 0),
                    "transfers_out_event": e.get("transfers_out_event", 0),
                    "net_transfers_event": (e.get("transfers_in_event", 0) or 0) - (e.get("transfers_out_event", 0) or 0),
                }

            rows = [row(e) for e in elements]
            risers = sorted(rows, key=lambda r: r["net_transfers_event"], reverse=True)[:params.limit]
            fallers = sorted(rows, key=lambda r: r["net_transfers_event"])[:params.limit]
            return json.dumps({"likely_price_risers": risers, "likely_price_fallers": fallers}, indent=2)
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 9. fpl_ping — diagnostics: is it the host, the API, or us?
# ---------------------------------------------------------------------------

class PingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="fpl_ping",
    annotations={"title": "Server & FPL API Diagnostics", **READ_ONLY},
)
async def fpl_ping(params: PingInput) -> str:
    """One call that answers 'why is it broken?': fetches bootstrap-static, reports
    round-trip time, current gameweek, whether the process cache was warm, and any
    error verbatim. If this works and other tools don't, the problem is in a tool;
    if this fails, the problem is the host or the FPL API.
    """
    import time as _t
    started = _t.time()
    warm = "/bootstrap-static/" in SHARED_CACHE.stats()
    try:
        async with FPLClient() as client:
            boot = await client.bootstrap()
            current = await client.current_event_id()
            return json.dumps({
                "ok": True,
                "fpl_api_ms": round((_t.time() - started) * 1000),
                "cache_was_warm": warm,
                "current_gameweek": current,
                "player_count": len(boot.get("elements", [])),
                "cached_paths_ttl_seconds": SHARED_CACHE.stats(),
            }, indent=2)
    except Exception as e:
        return json.dumps({"ok": False, "elapsed_ms": round((_t.time() - started) * 1000),
                           "error": str(e)}, indent=2)


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    """Plain HTTP endpoint for Render's health check and for an external keep-alive
    pinger (e.g. a free cron hitting this every 10 min stops the free tier sleeping)."""
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "cached": list(SHARED_CACHE.stats().keys())})


if __name__ == "__main__":
    import os

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        port = int(os.environ.get("PORT", 8000))
        # stateless_http=True is the important fix for a free-tier host: the default
        # mode keeps MCP sessions in process memory, so every time Render spins the
        # container down and back up, claude.ai's stored session id is gone and every
        # tool call fails with a generic execution error until the connector is
        # re-initialised. Stateless mode makes each call self-contained.
        # json_response=True returns plain JSON instead of an SSE stream — simpler for
        # proxies and load balancers to pass through unchanged.
        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=port,
            stateless_http=True,
            json_response=True,
        )
    else:
        mcp.run()  # stdio — for local Claude Desktop / Claude Code use
