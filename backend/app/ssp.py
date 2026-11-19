"""SSP warming pathways.

Historical years use NASA GISTEMP v4 global annual means, rebased from the
1951–1980 anomaly to the 1880–1900 mean of that same series (GISTEMP does not
start in 1850; 1880–1900 is the preindustrial proxy in the file). From the last
observed year the series is spliced linearly to the IPCC AR6 WG1 Table SPM.1
2081–2100 best estimate for each SSP, reached in 2090 and held to 2100.
Pathways therefore share one observed history and diverge after the last
GISTEMP year (about 2025).

Cumulative CO2 is preferred when both a Global Carbon Budget running sum and
marker SSP emissions are available. The IIASA timeseries route returned HTTP
500 and no budget file was joined, so the regressor stays
``ipcc_ar6_warming_c_fallback``.
"""

from __future__ import annotations

from app.gistemp_fallback import GISTEMP_JD

SCENARIOS = ("ssp119", "ssp126", "ssp245", "ssp370", "ssp585")

# 2081–2100 best estimates, °C relative to 1850–1900.
END_ANCHOR = {
    "ssp119": 1.4,
    "ssp126": 1.8,
    "ssp245": 2.7,
    "ssp370": 3.6,
    "ssp585": 4.4,
}
ANCHOR_YEAR = 2090
GISTEMP_URL = "https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.csv"


def parse_gistemp_csv(text: str) -> dict[int, float]:
    """Year -> J-D annual mean, °C vs 1951–1980. Skips incomplete years (***)."""
    series: dict[int, float] = {}
    header = None
    for line in text.splitlines():
        if line.startswith("Year,"):
            header = [cell.strip() for cell in line.split(",")]
            continue
        if header is None or not line or not line[0].isdigit():
            continue
        cells = [cell.strip() for cell in line.split(",")]
        row = dict(zip(header, cells))
        jd = row.get("J-D", "")
        if jd in {"", "***", "NA"}:
            continue
        try:
            series[int(row["Year"])] = float(jd)
        except (KeyError, ValueError):
            continue
    return series


def rebase_to_preindustrial(series: dict[int, float]) -> dict[int, float]:
    """Shift GISTEMP so the 1880–1900 mean is 0 °C."""
    base = [series[year] for year in range(1880, 1901) if year in series]
    if not base:
        base = [series[year] for year in sorted(series)[:21]]
    offset = -sum(base) / len(base)
    return {year: value + offset for year, value in series.items()}


def warming_table_from_observations(
    raw_jd: dict[int, float],
    year_min: int = 1979,
    year_max: int = 2100,
) -> dict[str, dict[int, float]]:
    """Observed warming through the last GISTEMP year, then a per-SSP splice to 2090."""
    rebased = rebase_to_preindustrial(raw_jd)
    if not rebased:
        rebased = rebase_to_preindustrial(GISTEMP_JD)
    last_year = max(year for year in rebased if year <= year_max)
    last_w = rebased[last_year]
    known = sorted(year for year in rebased if year_min <= year <= last_year)
    history: dict[int, float] = {}
    for year in range(year_min, last_year + 1):
        if year in rebased:
            history[year] = rebased[year]
            continue
        prev = max((y for y in known if y < year), default=None)
        nxt = min((y for y in known if y > year), default=None)
        if prev is not None and nxt is not None and nxt != prev:
            span = nxt - prev
            history[year] = rebased[prev] + (year - prev) * (rebased[nxt] - rebased[prev]) / span
        elif prev is not None:
            history[year] = rebased[prev]
        elif nxt is not None:
            history[year] = rebased[nxt]
        else:
            history[year] = last_w
    table: dict[str, dict[int, float]] = {}
    for scenario in SCENARIOS:
        end = END_ANCHOR[scenario]
        column: dict[int, float] = {}
        for year in range(year_min, year_max + 1):
            if year <= last_year:
                column[year] = history[year]
            elif year >= ANCHOR_YEAR:
                column[year] = end
            else:
                t = (year - last_year) / (ANCHOR_YEAR - last_year)
                column[year] = last_w + t * (end - last_w)
        table[scenario] = column
    return table


def warming_table(year_min: int = 1979, year_max: int = 2100) -> dict[str, dict[int, float]]:
    """Embedded GISTEMP table. The store replaces this when the live file arrives."""
    return warming_table_from_observations(GISTEMP_JD, year_min, year_max)


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
