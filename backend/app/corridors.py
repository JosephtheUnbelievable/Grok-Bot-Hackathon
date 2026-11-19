"""Shipping-season and community winter-ice fallback from concentration grids.

A month contributes ``fraction * days`` where fraction is the share of unmasked
corridor cells below 15% and days are 30 (28 in February), matching the day
length in API.md. A month counts as open for ``opening_date`` when that share
is at least one half.

Forecast months are the latest observed grid for that calendar month, thinned
to the scenario's trended extent before the corridor is measured.
"""

from __future__ import annotations

import numpy as np

from app.warp import PROJECTIONS, _forward

NSR = (66.0, 80.0, 30.0, 180.0)  # lat0, lat1, lon0, lon1
NWP = (68.0, 75.0, -140.0, -60.0)
COMMUNITIES = {
    "Utqiagvik": (71.2906, -156.7886),
    "Kivalina": (67.7269, -164.5333),
    "Shishmaref": (66.2567, -166.0719),
    "Tuktoyaktuk": (69.4453, -133.0342),
}
METHOD = (
    "linear-trend-fallback: share of unmasked corridor cells below 15% "
    "times 30 days (28 in February). Observed months use the NSIDC grid; "
    "forecast months thin that grid to the scenario extent trend."
)
WINTER_METHOD = (
    "linear-trend-fallback: share of Utqiagvik, Kivalina, Shishmaref, and "
    "Tuktoyaktuk whose nearby February ocean cells are below 15%. "
    "1981-2010 February at these sites is treated as ice-covered when "
    "climatology grids are not loaded."
)


def month_days(month: int) -> int:
    return 28 if month == 2 else 30


def season_from_fractions(year: int, fractions: dict[int, float]) -> tuple[int, str | None, int]:
    """fractions: month -> share of corridor cells below 15%."""
    total = 0.0
    open_months = []
    for month, frac in sorted(fractions.items()):
        frac = min(1.0, max(0.0, float(frac)))
        total += frac * month_days(month)
        if frac >= 0.5:
            open_months.append(month)
    opening = None
    for month in open_months:
        if month >= 6:
            opening = f"{year:04d}-{month:02d}-01"
            break
    return int(round(total)), opening, len(open_months)


def open_fraction(values: np.ndarray, mask: np.ndarray) -> float | None:
    if int(mask.sum()) == 0:
        return None
    open_cells = mask & (values < 15)
    return float(open_cells.sum() / mask.sum())


def _spec(grid):
    return PROJECTIONS.get(grid.epsg or 3411, PROJECTIONS[3411])


def corridor_mask(grid, box: tuple[float, float, float, float]) -> np.ndarray:
    lat, lon = grid_latlon(grid)
    lat0, lat1, lon0, lon1 = box
    return (lat >= lat0) & (lat <= lat1) & (lon >= lon0) & (lon <= lon1)


_LATLON_CACHE: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}


def grid_latlon(grid):
    key = (
        grid.epsg,
        grid.width,
        grid.height,
        grid.origin_x,
        grid.origin_y,
        grid.pixel_size_x,
        grid.pixel_size_y,
    )
    cached = _LATLON_CACHE.get(key)
    if cached is not None:
        return cached
    if grid.origin_x is None or not grid.pixel_size_x:
        lat = np.full((grid.height, grid.width), np.nan)
        lon = np.full_like(lat, np.nan)
        _LATLON_CACHE[key] = (lat, lon)
        return lat, lon
    spec = _spec(grid)
    cols = np.arange(grid.width)
    rows = np.arange(grid.height)
    cc, rr = np.meshgrid(cols, rows)
    x = grid.origin_x + cc * grid.pixel_size_x
    y = grid.origin_y - rr * grid.pixel_size_y
    lat, lon = _inverse(x, y, spec)
    _LATLON_CACHE[key] = (lat, lon)
    return lat, lon


def _inverse(x, y, spec):
    a, b = spec["ellipsoid"]
    e2 = 1.0 - (b * b) / (a * a)
    e = np.sqrt(e2)
    north = spec["north"]
    lat_ts = np.radians(spec["lat_ts"])

    def t_of(phi):
        s = np.sin(phi)
        if north:
            return np.tan(np.pi / 4 - phi / 2) / ((1 - e * s) / (1 + e * s)) ** (e / 2)
        return np.tan(np.pi / 4 + phi / 2) / ((1 + e * s) / (1 - e * s)) ** (e / 2)

    t_c = float(t_of(np.array(spec["lat_ts"])))
    m_c = np.cos(lat_ts) / np.sqrt(1.0 - e2 * np.sin(lat_ts) ** 2)
    rho = np.hypot(x, y)
    t = rho * t_c / (a * float(m_c))
    if north:
        lon = spec["lon0"] + np.degrees(np.arctan2(x, -y))
        phi = np.pi / 2 - 2 * np.arctan(np.maximum(t, 0))
        for _ in range(6):
            s = np.sin(phi)
            phi = np.pi / 2 - 2 * np.arctan(t * ((1 - e * s) / (1 + e * s)) ** (e / 2))
    else:
        lon = spec["lon0"] + np.degrees(np.arctan2(x, y))
        phi = -np.pi / 2 + 2 * np.arctan(np.maximum(t, 1e-12))
        for _ in range(6):
            s = np.sin(phi)
            phi = -np.pi / 2 + 2 * np.arctan(t * ((1 + e * s) / (1 - e * s)) ** (e / 2))
    lat = np.degrees(phi)
    lon = (lon + 180.0) % 360.0 - 180.0
    return lat, lon


def sample_ocean(grid, values: np.ndarray, lat: float, lon: float, radius: int = 4) -> float | None:
    spec = _spec(grid)
    if grid.origin_x is None or not grid.pixel_size_x:
        return None
    x, y = _forward(lat, lon, spec)
    col = int(np.rint((float(x) - grid.origin_x) / grid.pixel_size_x))
    row = int(np.rint((grid.origin_y - float(y)) / grid.pixel_size_y))
    found = []
    for dr in range(-radius, radius + 1):
        for dc in range(-radius, radius + 1):
            rr, cc = row + dr, col + dc
            if 0 <= rr < grid.height and 0 <= cc < grid.width and values[rr, cc] != 255:
                found.append(int(values[rr, cc]))
    if not found:
        return None
    return float(np.mean(found))
