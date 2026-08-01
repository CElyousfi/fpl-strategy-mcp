"""
Validates analysis.py logic against synthetic data shaped like real FPL API
responses (since fantasy.premierleague.com isn't reachable from this sandbox).
Run: python test_analysis.py
"""
import sys
from analysis import (
    classify_tier, defcon_hit_rate, find_blank_double_gameweeks,
    hit_math_check, rolling_fixture_score, DefconFieldsNotFound,
)

failures = []


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


# --- defcon_hit_rate: direct field strategy -------------------------------
history_direct = [
    {"element": 501, "round": 1, "minutes": 90, "defensive_contribution": 2},
    {"element": 501, "round": 2, "minutes": 90, "defensive_contribution": 0},
    {"element": 501, "round": 3, "minutes": 0, "defensive_contribution": 0},   # unused, minutes=0
    {"element": 501, "round": 4, "minutes": 90, "defensive_contribution": 2},
    {"element": 501, "round": 5, "minutes": 90, "defensive_contribution": 2},
]
profile = defcon_hit_rate(history_direct, "DEF")
check("defcon direct-field: 4 played matches counted (not 5, minutes=0 excluded)", profile.matches_considered == 4)
check("defcon direct-field: 3/4 hit rate = 75.0%", profile.hit_rate_pct == 75.0)
check("defcon direct-field: data_source flags direct_field", profile.data_source.startswith("direct_field"))

# --- defcon_hit_rate: component-fallback strategy --------------------------
history_components = [
    {"element": 502, "round": 1, "minutes": 90, "clearances_blocks_interceptions": 8, "tackles": 3, "recoveries": 2},
    {"element": 502, "round": 2, "minutes": 90, "clearances_blocks_interceptions": 6, "tackles": 1, "recoveries": 1},
]
# NOTE: tackles should NOT double count if cbit field already includes tackles in real API;
# this synthetic case treats clearances_blocks_interceptions as CBI-only + tackles separate,
# matching the CBIT acronym's 4 raw components split across 2 candidate fields.
profile2 = defcon_hit_rate(history_components, "DEF")
check("defcon component-fallback: uses components when no direct field exists", profile2.data_source.startswith("components["))
check("defcon component-fallback: match 1 = 8+3=11 >= 10 threshold -> hit", profile2.per_match[0].hit_threshold is True)
check("defcon component-fallback: match 2 = 6+1=7 < 10 threshold -> miss", profile2.per_match[1].hit_threshold is False)

# --- defcon_hit_rate: neither field exists -> loud, diagnostic failure -----
history_broken = [{"element": 503, "round": 1, "minutes": 90, "some_unrelated_field": 5}]
try:
    defcon_hit_rate(history_broken, "MID")
    check("defcon missing-field: raises DefconFieldsNotFound", False)
except DefconFieldsNotFound as e:
    check("defcon missing-field: raises DefconFieldsNotFound with available keys listed", "some_unrelated_field" in e.available_keys)

# --- rolling_fixture_score --------------------------------------------------
fixtures = [
    {"event": 1, "team_h": 1, "team_a": 2, "team_h_difficulty": 2, "team_a_difficulty": 4, "kickoff_time": "2026-08-21T19:00:00Z"},
    {"event": 2, "team_h": 3, "team_a": 1, "team_h_difficulty": 3, "team_a_difficulty": 3, "kickoff_time": "2026-08-29T14:00:00Z"},
    {"event": 3, "team_h": 1, "team_a": 4, "team_h_difficulty": 2, "team_a_difficulty": 4, "kickoff_time": "2026-09-01T19:00:00Z"},  # 3-day gap -> short rest
]
fscore = rolling_fixture_score(fixtures, team_id=1, num_gameweeks=3)
check("fixture score: 3 gameweeks considered", fscore.gameweeks_considered == [1, 2, 3])
check("fixture score: home/away read correctly (H, A, H)", fscore.home_away == ["H", "A", "H"])
check("fixture score: difficulties pulled from correct side (2, 3, 2)", fscore.raw_difficulties == [2, 3, 2])
check("fixture score: short-rest flag catches the 3-day gap between GW2->GW3", fscore.short_rest_flags == [False, False, True])
# weighted: weights [3,2,1] on [2,3,2] = (2*3+3*2+2*1)/6 = (6+6+2)/6 = 14/6 = 2.33
check("fixture score: weighted score matches manual calc (2.33)", abs(fscore.weighted_score - 2.33) < 0.01)

# --- classify_tier -----------------------------------------------------------
position_pool = [
    {"id": 1, "now_cost": 155, "selected_by_percent": "74.1", "form": "8.2", "element_type": 4},
    {"id": 2, "now_cost": 90, "selected_by_percent": "9.5", "form": "4.5", "element_type": 4},
    {"id": 3, "now_cost": 45, "selected_by_percent": "3.0", "form": "2.0", "element_type": 4},
    {"id": 4, "now_cost": 60, "selected_by_percent": "15.0", "form": "3.0", "element_type": 4},
]
core = classify_tier(position_pool[0], position_pool)
check("tier: Haaland-like (74% owned, top price) -> Core Anchor", core.suggested_tier == "Core Anchor")

edge = classify_tier(position_pool[1], position_pool)
check("tier: low ownership + strong form -> Calculated Edge Candidate", edge.suggested_tier == "Calculated Edge Candidate")

value = classify_tier(position_pool[3], position_pool)
check("tier: mid price/ownership -> Value Floor Candidate", value.suggested_tier == "Value Floor Candidate")

# --- hit_math_check ------------------------------------------------------------
hm_pass = hit_math_check(22.0, 15.0, 5, 4)
check("hit math: 7pt diff vs 4pt cost -> clears", hm_pass.cleared is True)
hm_fail = hit_math_check(17.0, 15.0, 5, 4)
check("hit math: 2pt diff vs 4pt cost -> does not clear", hm_fail.cleared is False)

# --- find_blank_double_gameweeks -----------------------------------------------
fixtures2 = [
    {"event": 10, "team_h": 1, "team_a": 2},
    {"event": 11, "team_h": 1, "team_a": 3},
    {"event": 11, "team_h": 4, "team_a": 1},  # team 1 has 2 fixtures in GW11 -> double
    # team 2 has no fixture at all in GW11 -> blank
]
bd = find_blank_double_gameweeks(fixtures2, team_ids=[1, 2], event_range=[10, 11])
check("blank/double: team 1 flagged as double in GW11", any(d.team_id == 1 and d.event == 11 for d in bd["doubles"]))
check("blank/double: team 2 flagged as blank in GW11", any(b.team_id == 2 and b.event == 11 for b in bd["blanks"]))

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print(f"ALL {len([1 for _ in range(1)])} CHECKS PASSED")
