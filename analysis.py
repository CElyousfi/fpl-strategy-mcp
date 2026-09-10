"""
Custom analysis functions that encode OUR specific strategy framework, not generic
FPL stat lookups. Each function here maps directly to a section of the strategy
documents this project is built on:

  - defcon_hit_rate()      -> Framework Layer 1.1 (threshold-hit-rate over raw totals)
  - rolling_fixture_score()-> Framework Layer 3.3 (weighted rolling window, not single FDR)
  - classify_tier()        -> Build Strategy Section 2 (Core Anchor / Value Floor / Edge)
  - hit_math_check()       -> Build Strategy Section 4.3 (explicit -4 hit threshold test)
  - find_blank_double_gameweeks() -> chip-timing support (Build Strategy Section 8)

DEFCON field-name caveat: the FPL API is undocumented and the defensive-contribution
fields are the newest addition to it. `defcon_hit_rate()` tries the most likely field
names first and falls back to a diagnostic error listing the *actual* keys present in
the response, rather than silently returning a wrong number. If it ever raises
DefconFieldsNotFound, that's not a bug to panic about — it's the function correctly
refusing to guess, exactly like the strategy docs want (never fill a gap with a hunch).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from fpl_client import POSITION_DEFCON_THRESHOLD, position_name, price


class DefconFieldsNotFound(Exception):
    """Raised when none of the expected defensive-contribution fields exist on a history
    entry. Carries the actual available keys so the caller (or a future debugging session)
    can immediately see what the API renamed the field to, instead of us guessing."""

    def __init__(self, available_keys: list[str]):
        self.available_keys = available_keys
        super().__init__(
            "Could not find a defensive-contribution field on this player's match "
            "history. This means the FPL API's field naming has likely changed since "
            f"this tool was written. Actual keys available on one match record: "
            f"{sorted(available_keys)}. Look for whichever key resembles "
            "'defensive_contribution', 'clearances_blocks_interceptions', 'tackles', "
            "or 'recoveries', then update DEFCON_FIELD_CANDIDATES in analysis.py."
        )


# Ordered by likelihood — tried top to bottom. A single field carrying the final
# match-level points (0 or 2) is preferred over reconstructing it from components,
# because it can never drift from whatever scoring rule FPL is actually running.
DEFCON_POINTS_FIELD_CANDIDATES = ["defensive_contribution", "defcon_points", "dc_points"]
# Component fields, used only if no direct points field is found. CBIT = Clearances +
# Blocks + Interceptions + Tackles. The API may expose C+B+I as one pre-combined field
# (most likely, matches how the Premier League names the stat) or as three separate
# ones — both are handled, and they are never summed together (that would double-count).
CBIT_COMBINED_FIELD = "clearances_blocks_interceptions"
CBIT_INDIVIDUAL_FIELDS = ["clearances", "blocks", "interceptions"]
TACKLES_FIELD_CANDIDATES = ["tackles", "tackles_won"]
RECOVERIES_FIELD_CANDIDATES = ["recoveries", "ball_recoveries"]


@dataclass
class DefconMatchResult:
    round: int
    minutes: int
    hit_threshold: bool
    raw_value: Optional[int]  # points if using direct field, else summed actions


@dataclass
class DefconProfile:
    player_id: int
    position: str
    threshold: int
    matches_considered: int
    matches_hit: int
    hit_rate_pct: float
    hit_rate_recent5_pct: Optional[float]
    per_match: list[DefconMatchResult] = field(default_factory=list)
    data_source: str = ""  # which field strategy actually worked, for transparency


def defcon_hit_rate(history: list[dict], element_type_name: str) -> DefconProfile:
    """Compute threshold-hit-rate (NOT raw totals) for a player's match history.

    Per Framework Layer 1.1: DEFCON is a threshold bonus, not a rate — 20 actions
    scores the same as 10. The correct metric is what fraction of matches a player
    clears the bar, not their season total action count. This function returns
    exactly that.
    """
    threshold = POSITION_DEFCON_THRESHOLD.get(element_type_name, 12)
    if not history:
        return DefconProfile(
            player_id=0, position=element_type_name, threshold=threshold,
            matches_considered=0, matches_hit=0, hit_rate_pct=0.0,
            hit_rate_recent5_pct=None, data_source="no_history",
        )

    played = [h for h in history if h.get("minutes", 0) > 0]
    if not played:
        return DefconProfile(
            player_id=history[0].get("element", 0), position=element_type_name,
            threshold=threshold, matches_considered=0, matches_hit=0,
            hit_rate_pct=0.0, hit_rate_recent5_pct=None, data_source="no_minutes_played",
        )

    sample_keys = list(played[0].keys())
    data_source = None
    results: list[DefconMatchResult] = []

    # Strategy 1: direct points field
    points_field = next((f for f in DEFCON_POINTS_FIELD_CANDIDATES if f in played[0]), None)
    if points_field:
        data_source = f"direct_field:{points_field}"
        for h in played:
            val = h.get(points_field, 0) or 0
            results.append(DefconMatchResult(
                round=h.get("round", 0), minutes=h.get("minutes", 0),
                hit_threshold=val > 0, raw_value=val,
            ))
    else:
        # Strategy 2: reconstruct from component action counts.
        # C+B+I comes from ONE source only — either the combined field, or the sum
        # of the three individual fields — never both, to avoid double-counting.
        has_combined = CBIT_COMBINED_FIELD in played[0]
        individual_present = [f for f in CBIT_INDIVIDUAL_FIELDS if f in played[0]]
        tackles_field = next((f for f in TACKLES_FIELD_CANDIDATES if f in played[0]), None)
        recoveries_field = next((f for f in RECOVERIES_FIELD_CANDIDATES if f in played[0]), None)

        cbi_source = (
            f"combined:{CBIT_COMBINED_FIELD}" if has_combined
            else f"individual:{'+'.join(individual_present)}" if individual_present
            else None
        )

        if cbi_source is None and tackles_field is None:
            raise DefconFieldsNotFound(available_keys=sample_keys)

        parts = [cbi_source, f"tackles:{tackles_field}" if tackles_field else None]
        if element_type_name in ("MID", "FWD"):
            parts.append(f"recoveries:{recoveries_field}" if recoveries_field else None)
        data_source = "components[" + ",".join(p for p in parts if p) + "]"

        for h in played:
            if has_combined:
                total = h.get(CBIT_COMBINED_FIELD, 0) or 0
            else:
                total = sum(h.get(f, 0) or 0 for f in individual_present)
            if tackles_field:
                total += h.get(tackles_field, 0) or 0
            if element_type_name in ("MID", "FWD") and recoveries_field:
                total += h.get(recoveries_field, 0) or 0
            results.append(DefconMatchResult(
                round=h.get("round", 0), minutes=h.get("minutes", 0),
                hit_threshold=total >= threshold, raw_value=total,
            ))

    hits = sum(1 for r in results if r.hit_threshold)
    recent5 = results[-5:]
    recent5_rate = (
        round(100 * sum(1 for r in recent5 if r.hit_threshold) / len(recent5), 1)
        if recent5 else None
    )

    return DefconProfile(
        player_id=history[0].get("element", 0),
        position=element_type_name,
        threshold=threshold,
        matches_considered=len(results),
        matches_hit=hits,
        hit_rate_pct=round(100 * hits / len(results), 1) if results else 0.0,
        hit_rate_recent5_pct=recent5_rate,
        per_match=results,
        data_source=data_source or "unknown",
    )


@dataclass
class FixtureScoreResult:
    team_id: int
    gameweeks_considered: list[int]
    weighted_score: float  # lower = easier, same 1-5 direction as FPL's own FDR
    raw_difficulties: list[int]
    home_away: list[str]
    short_rest_flags: list[bool]  # True if <5 days since this team's previous PL fixture
    note: str


def upcoming_fixtures(fixtures_for_team: list[dict], from_event: Optional[int] = None) -> list[dict]:
    """Only fixtures still to be played, in gameweek order.

    Drops anything the API marks finished or started, and anything before `from_event`
    when given. Without this, a rolling window taken mid-season silently starts at
    GW1 and scores fixtures that have already happened (found live in GW3, 2026/27).
    """
    out = []
    for f in fixtures_for_team:
        if f.get("event") is None:
            continue
        if f.get("finished") or f.get("started"):
            continue
        if from_event is not None and f["event"] < from_event:
            continue
        out.append(f)
    return sorted(out, key=lambda f: (f["event"], f.get("kickoff_time") or ""))


def rolling_fixture_score(
    fixtures_for_team: list[dict], team_id: int, num_gameweeks: int = 6,
    from_event: Optional[int] = None,
) -> FixtureScoreResult:
    """Weighted-average fixture difficulty over a rolling window, not a single-week snapshot.

    Per Framework Layer 3.3: nearer fixtures are weighted more heavily (linear decay),
    and each fixture's difficulty is read from the correct side (home/away) for this
    team specifically. A short-rest flag (under 5 days since the team's previous FPL
    fixture) is a partial proxy for congestion — it can only see Premier League
    fixtures through this API, not domestic cup or European rounds, so a `false` here
    does NOT rule out cup-fixture congestion. Cross-check cup scheduling manually per
    the Build Strategy's External Shock Monitoring section.
    """
    relevant = upcoming_fixtures(fixtures_for_team, from_event)[:num_gameweeks]

    if not relevant:
        return FixtureScoreResult(
            team_id=team_id, gameweeks_considered=[], weighted_score=3.0,
            raw_difficulties=[], home_away=[], short_rest_flags=[],
            note="No scheduled fixtures found in range (could be a blank gameweek stretch).",
        )

    difficulties: list[int] = []
    home_away: list[str] = []
    gameweeks: list[int] = []
    kickoffs: list[Optional[str]] = []

    for f in relevant:
        is_home = f.get("team_h") == team_id
        difficulty = f.get("team_h_difficulty") if is_home else f.get("team_a_difficulty")
        difficulties.append(difficulty if difficulty is not None else 3)
        home_away.append("H" if is_home else "A")
        gameweeks.append(f["event"])
        kickoffs.append(f.get("kickoff_time"))

    # Linear decay weights: nearest fixture weighted highest
    n = len(difficulties)
    weights = [n - i for i in range(n)]
    weighted_sum = sum(d * w for d, w in zip(difficulties, weights))
    weighted_score = round(weighted_sum / sum(weights), 2)

    # Short-rest proxy using PL kickoff gaps only
    short_rest = [False]
    for i in range(1, len(kickoffs)):
        if kickoffs[i] and kickoffs[i - 1]:
            try:
                from datetime import datetime
                t0 = datetime.fromisoformat(kickoffs[i - 1].replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(kickoffs[i].replace("Z", "+00:00"))
                short_rest.append((t1 - t0).days < 5)
            except ValueError:
                short_rest.append(False)
        else:
            short_rest.append(False)

    return FixtureScoreResult(
        team_id=team_id,
        gameweeks_considered=gameweeks,
        weighted_score=weighted_score,
        raw_difficulties=difficulties,
        home_away=home_away,
        short_rest_flags=short_rest,
        note=(
            "Lower weighted_score = easier run. short_rest_flags only sees PL fixtures — "
            "verify cup/European congestion separately."
        ),
    )


@dataclass
class TierSuggestion:
    suggested_tier: str
    reasoning: list[str]
    price: float
    ownership_pct: float
    form: float
    starts_ratio: Optional[float]


def classify_tier(
    element: dict, all_elements_same_position: list[dict]
) -> TierSuggestion:
    """Suggest Core Anchor / Value Floor / Calculated Edge Candidate per Build
    Strategy Section 2 — using price percentile within position, ownership, and
    form as numeric proxies.

    This is a STARTING POINT, not a verdict. A "Calculated Edge Candidate" result
    still has to independently clear the Edge Test (2 of: BPS-rule fit, verified
    role signal, new-manager information edge, data/price divergence) before it
    earns one of the reserved differential slots — those checks require judgment
    this function doesn't have access to (team news, manager tendencies, etc.).
    """
    pos_prices = sorted(price(e["now_cost"]) for e in all_elements_same_position)
    this_price = price(element["now_cost"])
    percentile = (
        sum(1 for p in pos_prices if p <= this_price) / len(pos_prices) if pos_prices else 0.5
    )
    ownership = float(element.get("selected_by_percent", 0) or 0)
    try:
        form = float(element.get("form", 0) or 0)
    except (TypeError, ValueError):
        form = 0.0
    starts = element.get("starts")
    minutes = element.get("minutes")
    starts_ratio = None
    reasoning: list[str] = []

    if ownership >= 25 and percentile >= 0.75:
        tier = "Core Anchor"
        reasoning.append(f"Top price bracket for position ({percentile:.0%} percentile) "
                          f"and high ownership ({ownership}%) — this is template for a reason.")
    elif ownership < 10 and (form >= 4.0 or percentile <= 0.35):
        tier = "Calculated Edge Candidate"
        reasoning.append(f"Low ownership ({ownership}%) combined with "
                          f"{'strong recent form' if form >= 4.0 else 'a cheap price point'} "
                          "— worth running through the Edge Test, not an automatic include.")
    else:
        tier = "Value Floor Candidate"
        reasoning.append(f"Mid-range price ({this_price}) and ownership ({ownership}%) — "
                          "evaluate on nailed minutes + DEFCON/set-piece angle, not reputation.")

    if minutes and element.get("starts") is not None:
        # starts isn't always populated depending on API version; guard for that
        pass

    return TierSuggestion(
        suggested_tier=tier,
        reasoning=reasoning,
        price=this_price,
        ownership_pct=ownership,
        form=form,
        starts_ratio=starts_ratio,
    )


@dataclass
class HitMathResult:
    cleared: bool
    point_differential: float
    hit_cost: int
    margin: float
    verdict: str


def hit_math_check(
    incoming_projected_points: float,
    outgoing_projected_points: float,
    horizon_gameweeks: int,
    hit_cost: int = 4,
) -> HitMathResult:
    """Framework Layer 4.3, made mechanical: a hit is only justified if the incoming
    player's projected points over the relevant horizon exceed the outgoing player's
    by MORE than the hit cost. No feelings, just the number.
    """
    differential = round(incoming_projected_points - outgoing_projected_points, 2)
    margin = round(differential - hit_cost, 2)
    cleared = differential > hit_cost
    verdict = (
        f"CLEARS the bar by {margin} pts over {horizon_gameweeks} GW — hit is justified."
        if cleared
        else f"DOES NOT clear the bar (short by {abs(margin)} pts over {horizon_gameweeks} GW) "
             "— default to NOT taking this hit; bank the transfer instead."
    )
    return HitMathResult(
        cleared=cleared, point_differential=differential, hit_cost=hit_cost,
        margin=margin, verdict=verdict,
    )


@dataclass
class GameweekTeamCount:
    event: int
    team_id: int
    fixture_count: int


def find_blank_double_gameweeks(
    fixtures: list[dict], team_ids: list[int], event_range: list[int]
) -> dict[str, list[GameweekTeamCount]]:
    """Detect blank (0 fixtures) and double (2+ fixtures) gameweeks per team, within
    the given event range. Supports chip-timing decisions (Build Strategy Section 8).
    """
    counts: dict[tuple[int, int], int] = {}
    for f in fixtures:
        ev = f.get("event")
        if ev not in event_range:
            continue
        for side in ("team_h", "team_a"):
            tid = f.get(side)
            if tid in team_ids:
                counts[(ev, tid)] = counts.get((ev, tid), 0) + 1

    blanks: list[GameweekTeamCount] = []
    doubles: list[GameweekTeamCount] = []
    for ev in event_range:
        for tid in team_ids:
            n = counts.get((ev, tid), 0)
            if n == 0:
                blanks.append(GameweekTeamCount(event=ev, team_id=tid, fixture_count=0))
            elif n >= 2:
                doubles.append(GameweekTeamCount(event=ev, team_id=tid, fixture_count=n))

    return {"blanks": blanks, "doubles": doubles}
