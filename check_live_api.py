"""
Run this ONCE, on your own machine, right after setup — before trusting the server
for real decisions.

This project was built and unit-tested against synthetic data shaped like the FPL
API, because the sandbox it was built in cannot reach fantasy.premierleague.com
(network allowlist). Everything about the request/response shapes below is
well-established and stable, EXCEPT the defensive-contribution fields, which are
the newest addition to the API and the one part genuinely unverified live.

This script hits the real API, prints what it actually finds, and tells you plainly
whether analysis.py's field guesses matched reality. If they didn't, it tells you
exactly what to change and where.

Usage:
    python check_live_api.py
"""
import asyncio
import sys

import httpx

from analysis import (
    CBIT_COMBINED_FIELD, CBIT_INDIVIDUAL_FIELDS, DEFCON_POINTS_FIELD_CANDIDATES,
    RECOVERIES_FIELD_CANDIDATES, TACKLES_FIELD_CANDIDATES,
)
from fpl_client import BASE_URL, USER_AGENT


async def main() -> None:
    print(f"Checking live FPL API at {BASE_URL} ...\n")
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=15.0) as client:
        try:
            boot_resp = await client.get(f"{BASE_URL}/bootstrap-static/")
            boot_resp.raise_for_status()
        except Exception as e:
            print(f"FAILED to reach bootstrap-static: {e}")
            print("Check your internet connection and that fantasy.premierleague.com is reachable.")
            sys.exit(1)

        boot = boot_resp.json()
        print(f"OK  bootstrap-static reachable — {len(boot.get('elements', []))} players, "
              f"{len(boot.get('teams', []))} teams found.\n")

        # Find any defender who's actually played this season, to inspect real match history
        defenders = [e for e in boot["elements"] if e["element_type"] == 2 and e.get("minutes", 0) > 0]
        if not defenders:
            print("No defenders with minutes played yet found in bootstrap-static — "
                  "this is expected before Gameweek 1 has been played. Re-run this "
                  "script after the first gameweek completes.")
            return

        sample = sorted(defenders, key=lambda e: e.get("minutes", 0), reverse=True)[0]
        print(f"Inspecting match history for {sample['web_name']} (id={sample['id']}, "
              f"{sample.get('minutes')} minutes played)...\n")

        summary_resp = await client.get(f"{BASE_URL}/element-summary/{sample['id']}/")
        summary_resp.raise_for_status()
        history = summary_resp.json().get("history", [])
        if not history:
            print("No match history returned. Try again after Gameweek 1.")
            return

        one_match = history[0]
        actual_keys = sorted(one_match.keys())
        print("Actual keys present on one match record:")
        for k in actual_keys:
            print(f"    {k}: {one_match[k]}")
        print()

        # Report which of our candidate field names actually exist
        found_direct = [f for f in DEFCON_POINTS_FIELD_CANDIDATES if f in one_match]
        found_combined = CBIT_COMBINED_FIELD if CBIT_COMBINED_FIELD in one_match else None
        found_individual = [f for f in CBIT_INDIVIDUAL_FIELDS if f in one_match]
        found_tackles = [f for f in TACKLES_FIELD_CANDIDATES if f in one_match]
        found_recoveries = [f for f in RECOVERIES_FIELD_CANDIDATES if f in one_match]

        print("=" * 70)
        if found_direct:
            print(f"GOOD: direct points field found -> {found_direct[0]}")
            print("fpl_defcon_profile will use Strategy 1 (direct field) — most reliable path.")
        elif found_combined or found_individual or found_tackles:
            print("OK: no direct points field, but component fields were found:")
            if found_combined:
                print(f"    combined C+B+I field -> {found_combined}")
            elif found_individual:
                print(f"    individual C/B/I fields -> {found_individual}")
            else:
                print("    NO clearances/blocks/interceptions field found at all — "
                      "hit-rate will be computed from tackles (+recoveries) only, which "
                      "UNDERSTATES the true total. Fix analysis.py before trusting this.")
            if found_tackles:
                print(f"    tackles field -> {found_tackles[0]}")
            if found_recoveries:
                print(f"    recoveries field -> {found_recoveries[0]}")
        else:
            print("PROBLEM: none of the expected field names were found.")
            print("fpl_defcon_profile will raise DefconFieldsNotFound (loud failure, not a "
                  "silent wrong number — but it does mean the tool won't work until fixed).")
            print("\nLook at the full key list above, find whichever key holds defensive")
            print("contribution data, and update the *_FIELD_CANDIDATES constants at the")
            print("top of analysis.py to match.")
        print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
