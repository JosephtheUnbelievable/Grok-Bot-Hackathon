"""Project a ship forward and turn concentration samples into an ice-risk level.

``concentration_at(lat, lon)`` is injected by the host. It returns percent
(0–100) or ``None`` when the cell is masked or the grid is missing. 255 is
treated as masked. This module never opens a raster.
"""

from __future__ import annotations

import math
from collections.abc import Callable

ConcentrationAt = Callable[[float, float], float | None]

MARGINAL = 15.0
HEAVY = 50.0
# One degree of latitude is 60 nautical miles.
NM_PER_DEGREE = 60.0
AHEAD_HOURS = (6.0, 12.0)


def project(lat: float, lon: float, course_deg: float, distance_nm: float) -> tuple[float, float]:
    """Move ``distance_nm`` along ``course_deg`` (0 = north, 90 = east).

    Longitude is scaled by the cosine of the mid-latitude so a due-north
    course changes only latitude: 12 kn for 6 h is 72 nm, which is 1.2°.
    """
    distance_deg = distance_nm / NM_PER_DEGREE
    course = math.radians(course_deg % 360.0)
    dlat = distance_deg * math.cos(course)
    lat2 = max(-90.0, min(90.0, lat + dlat))
    mean = math.radians((lat + lat2) / 2.0)
    cos_lat = math.cos(mean)
    if abs(cos_lat) < 1e-8:
        dlon = 0.0
    else:
        dlon = distance_deg * math.sin(course) / cos_lat
    lon2 = lon + dlon
    lon2 = (lon2 + 180.0) % 360.0 - 180.0
    return lat2, lon2


def normalize_concentration(value) -> float | None:
    """Percent, or None for masked / missing / non-numeric samples.

    Land and the NSIDC missing class arrive as 255. Those are not ice.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    if number < 0 or number > 100:
        return None
    return number


def _sample(concentration_at: ConcentrationAt | None, lat: float, lon: float, on_error) -> float | None:
    if concentration_at is None:
        return None
    try:
        return normalize_concentration(concentration_at(lat, lon))
    except Exception:
        if on_error is not None:
            on_error()
        return None


def assess(
    lat: float,
    lon: float,
    sog_kn: float | None,
    cog_deg: float | None,
    heading: float | None,
    concentration_at: ConcentrationAt | None,
    on_error=None,
) -> dict:
    """Ice risk at the ship and along the next 6 h and 12 h of travel.

    No projection when speed is unknown (ahead is null) or zero (ahead equals
    the sample at the ship). Course is COG, then heading.
    """
    here = _sample(concentration_at, lat, lon, on_error)
    ahead = _ahead(lat, lon, sog_kn, cog_deg, heading, here, concentration_at, on_error)
    return {
        "level": level_for(here, ahead),
        "concentration_here": here,
        "concentration_ahead": ahead,
        "entering": is_entering(here, ahead),
    }


def _ahead(lat, lon, sog_kn, cog_deg, heading, here, concentration_at, on_error):
    if sog_kn is None:
        return None
    if sog_kn == 0:
        return here
    course = cog_deg if cog_deg is not None else heading
    if course is None:
        return None
    samples = []
    for hours in AHEAD_HOURS:
        plat, plon = project(lat, lon, course, sog_kn * hours)
        samples.append(_sample(concentration_at, plat, plon, on_error))
    numbers = [sample for sample in samples if sample is not None]
    if not numbers:
        return None
    return max(numbers)


def level_for(here: float | None, ahead: float | None) -> str:
    numbers = [sample for sample in (here, ahead) if sample is not None]
    if not numbers:
        return "unknown"
    if any(sample >= HEAVY for sample in numbers):
        return "heavy"
    if any(sample >= MARGINAL for sample in numbers):
        return "marginal"
    return "clear"


def is_entering(here: float | None, ahead: float | None) -> bool:
    """True only when both samples are numbers and the ship crosses 15% or 50% upward."""
    if here is None or ahead is None:
        return False
    crosses_marginal = ahead >= MARGINAL and here < MARGINAL
    crosses_heavy = ahead >= HEAVY and here < HEAVY
    return crosses_marginal or crosses_heavy


def icebreaker_name(name: str | None) -> bool:
    """AIS has no ice-class field. Only an explicit name token counts."""
    if not name:
        return False
    return "ICEBREAKER" in name.upper()
