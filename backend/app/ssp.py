"""SSP pathways.

The live IIASA query is attempted by the store. When it cannot be turned into a
cumulative-CO2 series, this module fills the IPCC AR6 WG1 Table SPM.1 best-estimate
warming anchors. 2021–2040 is 1.5 °C on every pathway. 2081–2100 is the pathway
anchor. Years between 2040 and 2081 are linear. Years before 2040, including the
satellite era, hold the near-term 1.5 °C anchor — AR6 does not give a pre-2021
pathway value to interpolate. That choice is noted in backend/README.md.
"""

from __future__ import annotations

SCENARIOS = ("ssp119", "ssp126", "ssp245", "ssp370", "ssp585")

# 2081–2100 best estimates, °C relative to 1850–1900.
END_ANCHOR = {
    "ssp119": 1.4,
    "ssp126": 1.8,
    "ssp245": 2.7,
    "ssp370": 3.6,
    "ssp585": 4.4,
}
NEAR_TERM_C = 1.5
NEAR_TERM_LAST_YEAR = 2040
END_FIRST_YEAR = 2081


def warming_c(scenario: str, year: int) -> float:
    end = END_ANCHOR[scenario]
    if year <= NEAR_TERM_LAST_YEAR:
        return NEAR_TERM_C
    if year >= END_FIRST_YEAR:
        return end
    span = END_FIRST_YEAR - NEAR_TERM_LAST_YEAR
    return NEAR_TERM_C + (year - NEAR_TERM_LAST_YEAR) * (end - NEAR_TERM_C) / span


def warming_table(year_min: int = 1979, year_max: int = 2100) -> dict[str, dict[int, float]]:
    return {
        scenario: {year: warming_c(scenario, year) for year in range(year_min, year_max + 1)}
        for scenario in SCENARIOS
    }


def blended_warming(
    table: dict[str, dict[int, float]],
    year: int,
    scenario: str,
    blend: float | None,
) -> float:
    """blend 0..4 indexes ssp119..ssp585. Absent blend uses the named scenario."""
    if blend is None:
        key = scenario if scenario in table else "ssp245"
        return float(table[key][year])
    i = int(blend // 1)
    if i >= 4:
        return float(table[SCENARIOS[4]][year])
    if i < 0:
        i = 0
    frac = blend - i
    left = table[SCENARIOS[i]][year]
    right = table[SCENARIOS[min(i + 1, 4)]][year]
    return float((1.0 - frac) * left + frac * right)
