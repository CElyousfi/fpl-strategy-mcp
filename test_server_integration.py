"""
Exercises the REAL MCP tool-call path (server.mcp.call_tool) with a mocked FPL API,
so we catch wiring bugs between server.py and fpl_client.py/analysis.py that the
isolated analysis.py unit tests can't see. Run: python test_server_integration.py
"""
import asyncio
import json
import sys

import fpl_client
import server

failures = []


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


MOCK_BOOTSTRAP = {
    "teams": [
        {"id": 1, "name": "Arsenal"},
        {"id": 2, "name": "Man City"},
    ],
    "elements": [
        {
            "id": 501, "web_name": "Gabriel", "team": 1, "element_type": 2,
            "now_cost": 80, "selected_by_percent": "45.2", "form": "5.0",
            "total_points": 180, "status": "a", "news": "",
        },
        {
            "id": 502, "web_name": "Haaland", "team": 2, "element_type": 4,
            "now_cost": 155, "selected_by_percent": "74.1", "form": "8.5",
            "total_points": 250, "status": "a", "news": "",
        },
    ],
    "events": [
        {"id": 1, "is_current": True, "is_next": False},
        {"id": 2, "is_current": False, "is_next": True},
    ],
}

MOCK_FIXTURES = [
    # GW1 is the *current* (played) gameweek in MOCK_BOOTSTRAP; GW2 is next. A fixture
    # window must therefore start at GW2 — GW1 is history and must be excluded.
    {"event": 1, "team_h": 1, "team_a": 2, "team_h_difficulty": 2, "team_a_difficulty": 4,
     "kickoff_time": "2026-08-21T19:00:00Z", "finished": True, "started": True},
    {"event": 2, "team_h": 2, "team_a": 1, "team_h_difficulty": 3, "team_a_difficulty": 3,
     "kickoff_time": "2026-08-29T14:00:00Z", "finished": False, "started": False},
    {"event": 3, "team_h": 1, "team_a": 2, "team_h_difficulty": 4, "team_a_difficulty": 2,
     "kickoff_time": "2026-09-05T14:00:00Z", "finished": False, "started": False},
]


async def mock_bootstrap(self):
    return MOCK_BOOTSTRAP


async def mock_fixtures(self, event=None):
    if event is None:
        return MOCK_FIXTURES
    return [f for f in MOCK_FIXTURES if f["event"] == event]


async def mock_player_summary(self, player_id):
    return {"history": [
        {"element": player_id, "round": 1, "minutes": 90, "defensive_contribution": 2},
        {"element": player_id, "round": 2, "minutes": 90, "defensive_contribution": 0},
    ]}


async def mock_entry(self, team_id):
    if team_id == 99:
        raise fpl_client.FPLAPIError("Not found: /entry/99/. Check that any IDs used are correct.")
    return {"player_first_name": "Test", "player_last_name": "Manager", "name": "Tier FC",
            "summary_overall_rank": 123456}


async def mock_entry_picks(self, team_id, event):
    return {"active_chip": None,
            "entry_history": {"points": 85, "rank": 3923457, "bank": 0, "value": 1000,
                              "event_transfers": 0, "event_transfers_cost": 0, "points_on_bench": 0},
            "picks": [{"element": 502, "is_captain": True, "is_vice_captain": False, "multiplier": 2},
                      {"element": 501, "is_captain": False, "is_vice_captain": True, "multiplier": 1}]}


async def mock_entry_history(self, team_id):
    return {"chips": []}


async def main():
    # Monkey-patch the network layer only — everything else (server.py routing,
    # analysis.py logic, JSON shaping) runs for real.
    fpl_client.FPLClient.bootstrap = mock_bootstrap
    fpl_client.FPLClient.fixtures = mock_fixtures
    fpl_client.FPLClient.player_summary = mock_player_summary
    fpl_client.FPLClient.entry = mock_entry
    fpl_client.FPLClient.entry_picks = mock_entry_picks
    fpl_client.FPLClient.entry_history = mock_entry_history

    # --- fpl_search_players ---
    r = await server.mcp.call_tool("fpl_search_players", {"params": {"position": "FWD", "limit": 10}})
    data = json.loads(r.structured_content["result"])
    check("search_players: finds Haaland under FWD filter", any(p["name"] == "Haaland" for p in data["players"]))
    check("search_players: excludes Gabriel (DEF) from FWD filter", not any(p["name"] == "Gabriel" for p in data["players"]))

    # --- fpl_defcon_profile ---
    r = await server.mcp.call_tool("fpl_defcon_profile", {"params": {"player_id": 501}})
    data = json.loads(r.structured_content["result"])
    check("defcon_profile: correct player resolved", data["name"] == "Gabriel")
    check("defcon_profile: 1/2 hit rate = 50.0%", data["season_hit_rate_pct"] == 50.0)

    # --- fpl_fixture_outlook ---
    r = await server.mcp.call_tool("fpl_fixture_outlook", {"params": {"team_name_contains": "Arsenal", "num_gameweeks": 2}})
    data = json.loads(r.structured_content["result"])
    check("fixture_outlook: resolves Arsenal by substring", data["team"] == "Arsenal")
    check("fixture_outlook: window starts at the NEXT gameweek, played GW1 excluded",
          data["gameweeks"] == [2, 3])
    check("fixture_outlook: reads away fixture difficulty correctly for GW2 (A, diff=3)",
          data["home_away"][0] == "A" and data["raw_difficulties"][0] == 3)
    check("fixture_outlook: reads home fixture difficulty correctly for GW3 (H, diff=4)",
          data["home_away"][1] == "H" and data["raw_difficulties"][1] == 4)

    # --- fpl_tier_classifier ---
    r = await server.mcp.call_tool("fpl_tier_classifier", {"params": {"player_id": 502}})
    data = json.loads(r.structured_content["result"])
    check("tier_classifier: Haaland (74% owned) -> Core Anchor", data["suggested_tier"] == "Core Anchor")

    # --- fpl_blank_double_gameweeks ---
    r = await server.mcp.call_tool("fpl_blank_double_gameweeks", {"params": {"from_gameweek": 1, "to_gameweek": 2}})
    data = json.loads(r.structured_content["result"])
    check("blank_double: no blanks/doubles in this clean 2-fixture mock", data["blanks"] == [] and data["doubles"] == [])

    # --- fpl_price_ownership_trends (add transfer fields to mock elements on the fly) ---
    MOCK_BOOTSTRAP["elements"][0]["transfers_in_event"] = 50000
    MOCK_BOOTSTRAP["elements"][0]["transfers_out_event"] = 1000
    MOCK_BOOTSTRAP["elements"][1]["transfers_in_event"] = 1000
    MOCK_BOOTSTRAP["elements"][1]["transfers_out_event"] = 60000
    r = await server.mcp.call_tool("fpl_price_ownership_trends", {"params": {}})
    data = json.loads(r.structured_content["result"])
    check("price_trends: Gabriel is the top riser", data["likely_price_risers"][0]["name"] == "Gabriel")
    check("price_trends: Haaland is the top faller", data["likely_price_fallers"][0]["name"] == "Haaland")

    # --- fpl_get_team (happy path + clean error path) ---
    r = await server.mcp.call_tool("fpl_get_team", {"params": {"team_id": 4605183, "event": 2}})
    data = json.loads(r.structured_content["result"])
    check("get_team: manager name resolved", data["manager_name"] == "Test Manager")
    check("get_team: captain flagged on Haaland", any(p["name"] == "Haaland" and p["is_captain"] for p in data["picks"]))
    check("get_team: chip log present and empty", data["chips_used_this_season"] == [])
    check("get_team: falls back to current gameweek when event omitted",
          json.loads((await server.mcp.call_tool("fpl_get_team", {"params": {"team_id": 4605183}})).structured_content["result"])["gameweek"] == 1)
    r = await server.mcp.call_tool("fpl_get_team", {"params": {"team_id": 99}})
    data = json.loads(r.structured_content["result"])
    check("get_team: invalid id returns a clean JSON error, not a crash", "error" in data and "Not found" in data["error"])

    # --- fpl_ping ---
    r = await server.mcp.call_tool("fpl_ping", {"params": {}})
    data = json.loads(r.structured_content["result"])
    check("ping: ok with mocked API", data["ok"] is True and data["current_gameweek"] == 1)

    # --- shared cache: two clients share one cache ---
    fpl_client.SHARED_CACHE.set("/probe/", {"x": 1})
    check("cache: a fresh FPLClient sees entries written by another",
          fpl_client.FPLClient()._cache.get("/probe/") == {"x": 1})
    fpl_client.SHARED_CACHE.clear()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("ALL INTEGRATION CHECKS PASSED")


asyncio.run(main())
