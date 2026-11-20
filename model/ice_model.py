"""Per-cell, per-month sea-ice logistic model.

The driver is the bundle's regressor (cumulative CO2, or the labeled IPCC
warming fallback), never the calendar year. Forecast ``low`` / ``median`` /
``high`` are the 10th / 50th / 90th percentiles of a 40-draw year bootstrap
from ``numpy.random.Generator(0)``.

Georeferencing assumption
-------------------------
Shipping boxes and the winter-ice index need latitudes. They are computed only
when a north grid is the NSIDC 25 km polar-stereographic grid, 304 columns by
448 rows (width 304, height 448), which is the G02135 north GeoTIFF size.

Cell centres use the published grid definition (EPSG:3411 / Hughes 1980):

- upper-left corner of the upper-left pixel at (-3,850,000 m, 5,850,000 m)
- cell size 25,000 m, row 0 at the top
- true-scale latitude 70°N, central meridian -45°
- Hughes 1980: a = 6,378,273 m, inverse flattening 298.279411123064

Any other shape, including the tiny grids used in unit tests, leaves the
winter index ``None`` and both shipping seasons at 0 days / no opening date.
The south grid is not georeferenced; both proxies are Arctic-only.

Other choices that do not change the dataclass contract are listed in
``model/README.md`` under Assumptions.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Literal

import numpy as np

try:
    from numba import njit, prange

    _HAVE_NUMBA = True
except Exception:  # pragma: no cover - numpy loop below is the reference
    njit = None
    prange = range
    _HAVE_NUMBA = False

Pole = Literal["north", "south"]
Band = Literal["median", "low", "high"]
ScenarioId = Literal["ssp119", "ssp126", "ssp245", "ssp370", "ssp585"]

SCENARIO_IDS: tuple[str, ...] = ("ssp119", "ssp126", "ssp245", "ssp370", "ssp585")
BANDS: tuple[str, ...] = ("low", "median", "high")

YEAR_MIN = 1979
YEAR_MAX = 2100
N_BOOT = 40
BOOT_SEED = 0
GT_PER_MM = 362.0
CELL_AREA_KM2 = 625.0
EXTENT_THRESHOLD = 15
TRAIN_END_YEAR = 2010
EVAL_START_YEAR = 2011
SCALE_YEAR_MIN = 2000
SCALE_YEAR_MAX = 2010
FALLBACK_START_YEAR = 2002
FALLBACK_SCALE_YEAR = 2025
GREENLAND_RATE_GT_YR = -264.0
ANTARCTICA_RATE_GT_YR = -135.0
MIN_FIT_YEARS = 8
NORTH_WIDTH = 304
NORTH_HEIGHT = 448

SOURCE_NSIDC = "nsidc-g02135"
SOURCE_FORECAST = "model-forecast"
SOURCE_NOT_READY = "model-not-ready"
SOURCE_GRACE = "grace-gracefo"
SOURCE_NASA = "nasa-science-summary-fallback"
METHOD_CSV = "nsidc_monthly_csv"
METHOD_MODEL = "model_cell_sum_scaled"

# Hughes 1980 ellipsoid and NSIDC north polar stereographic (EPSG:3411).
_HUGHES_A = 6_378_273.0
_HUGHES_INV_F = 298.279411123064
_HUGHES_F = 1.0 / _HUGHES_INV_F
_HUGHES_E2 = _HUGHES_F * (2.0 - _HUGHES_F)
_HUGHES_E = math.sqrt(_HUGHES_E2)
_LAT_TS = math.radians(70.0)
_LON0 = math.radians(-45.0)


def _t_conformal(phi: float) -> float:
    s = math.sin(phi)
    e = _HUGHES_E
    return math.tan(math.pi / 4.0 - phi / 2.0) / ((1.0 - e * s) / (1.0 + e * s)) ** (e / 2.0)


_T_C = _t_conformal(_LAT_TS)
_M_C = math.cos(_LAT_TS) / math.sqrt(1.0 - _HUGHES_E2 * math.sin(_LAT_TS) ** 2)


@dataclass(frozen=True)
class MonthlyGrid:
    pole: Pole
    year: int
    month: int
    width: int
    height: int
    values_u8: bytes  # row-major, 0-100 percent, 255 masked
    extent_km2: float | None  # CSV extent already converted to km^2; None on a gap
    area_km2: float | None
    gap_masked: bool
    pole_hole_cells: int


@dataclass(frozen=True)
class MassPoint:
    decimal_year: float
    mass_gt: float  # anomaly, negative = loss
    uncertainty_gt: float | None


@dataclass(frozen=True)
class TrainingBundle:
    grids: list[MonthlyGrid]
    cumulative_co2_gt_by_year: dict[int, float]  # historical, empty if missing
    ssp_cumulative_co2_gt: dict[str, dict[int, float]]  # canonical scenario -> year -> Gt
    ssp_warming_c: dict[str, dict[int, float]]  # canonical scenario -> year -> °C
    grace_greenland: list[MassPoint]
    grace_antarctica: list[MassPoint]
    cell_area_km2: dict[str, list[float]] | None  # pole -> row-major km^2, or None
    regressor: Literal["cumulative_co2_gt", "ipcc_ar6_warming_c_fallback"]


@dataclass(frozen=True)
class GridRequest:
    pole: Pole
    year: int
    month: int
    scenario: ScenarioId
    blend: float | None
    band: Band


@dataclass(frozen=True)
class ConcentrationGrid:
    width: int
    height: int
    values_u8: bytes
    extent_km2: float | None
    extent_scale: float
    is_forecast: bool
    gap_masked: bool
    pole_hole_cells: int
    source: str


@dataclass(frozen=True)
class ExtentPoint:
    year: int
    extent_km2: float | None
    low_km2: float | None
    high_km2: float | None
    is_forecast: bool
    gap_masked: bool
    source: str
    extent_method: Literal["nsidc_monthly_csv", "model_cell_sum_scaled"]


@dataclass(frozen=True)
class ShippingSeason:
    open_season_days: int
    opening_date: str | None
    open_month_count: int


@dataclass(frozen=True)
class ImpactResult:
    is_forecast: bool
    northern_sea_route: ShippingSeason
    northwest_passage: ShippingSeason
    ice_free_september_year: int | None
    winter_ice_loss_index: float | None
    greenland_mm: float
    antarctica_mm: float
    total_mm: float
    low_mm: float
    high_mm: float
    sea_level_source: Literal["grace-gracefo", "nasa-science-summary-fallback"]


@dataclass(frozen=True)
class PoleScore:
    rmse_km2: float
    bias_km2: float
    n_months: int
    skill_vs_climatology: float


@dataclass(frozen=True)
class BacktestResult:
    trained: bool
    train_end_year: int
    eval_start_year: int
    eval_end_year: int
    north: PoleScore
    south: PoleScore


def _blend_series(
    pathways: dict[str, dict[int, float]],
    scenario: str,
    blend: float | None,
) -> dict[int, float]:
    """Linear blend of neighbouring SSP pathways. ``None`` selects ``scenario``."""
    if blend is None:
        raw = pathways.get(scenario) or {}
        return {int(year): float(value) for year, value in raw.items()}
    position = float(blend)
    if position < 0.0:
        position = 0.0
    if position > 4.0:
        position = 4.0
    index = int(math.floor(position))
    if index >= 4:
        index = 4
        fraction = 0.0
    else:
        fraction = position - index
    left = pathways.get(SCENARIO_IDS[index]) or {}
    right = pathways.get(SCENARIO_IDS[min(index + 1, 4)]) or {}
    out: dict[int, float] = {}
    for year in set(left) | set(right):
        left_value = left.get(year)
        right_value = right.get(year)
        if left_value is None and right_value is None:
            continue
        if left_value is None:
            out[int(year)] = float(right_value)  # type: ignore[arg-type]
        elif right_value is None:
            out[int(year)] = float(left_value)
        else:
            out[int(year)] = (1.0 - fraction) * float(left_value) + fraction * float(right_value)
    return out


def _hold(series: dict[int, float], year: int) -> float | None:
    """Value at ``year``, else the latest value at or before it, else the earliest."""
    if not series:
        return None
    if year in series:
        return float(series[year])
    prior = [key for key in series if key <= year]
    if prior:
        return float(series[max(prior)])
    return float(series[min(series)])


def _north_latlon() -> tuple[np.ndarray, np.ndarray]:
    """Latitude and longitude of NSIDC 25 km north cell centres, shape (448, 304)."""
    columns = np.arange(NORTH_WIDTH, dtype=np.float64)
    rows = np.arange(NORTH_HEIGHT, dtype=np.float64)
    x = -3_850_000.0 + (columns + 0.5) * 25_000.0
    y = 5_850_000.0 - (rows + 0.5) * 25_000.0
    xx, yy = np.meshgrid(x, y)
    rho = np.hypot(xx, yy)
    t = rho * _T_C / (_HUGHES_A * _M_C)
    chi = np.pi / 2.0 - 2.0 * np.arctan(np.where(rho == 0.0, 0.0, t))
    e2 = _HUGHES_E2
    e4 = e2 * e2
    e6 = e4 * e2
    e8 = e4 * e4
    phi = (
        chi
        + (e2 / 2.0 + 5.0 * e4 / 24.0 + e6 / 12.0 + 13.0 * e8 / 360.0) * np.sin(2.0 * chi)
        + (7.0 * e4 / 48.0 + 29.0 * e6 / 240.0 + 811.0 * e8 / 11520.0) * np.sin(4.0 * chi)
        + (7.0 * e6 / 120.0 + 81.0 * e8 / 1120.0) * np.sin(6.0 * chi)
        + (4279.0 * e8 / 161280.0) * np.sin(8.0 * chi)
    )
    lat = np.rad2deg(phi)
    lon = np.rad2deg(_LON0 + np.arctan2(xx, -yy))
    lon = (lon + 180.0) % 360.0 - 180.0
    lat = np.where(rho == 0.0, 90.0, lat)
    return lat, lon


def _sigmoid(eta: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(eta, -60.0, 60.0)))


def _irls(
    y: np.ndarray,
    valid: np.ndarray,
    x: np.ndarray,
    counts: np.ndarray,
    original_mean_pct: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fractional logit IRLS. ``y`` and ``valid`` are (N, C), ``x`` and ``counts`` are (N,).

    A draw with fewer than 8 effective observations (counts included) predicts the
    cell's original training-mean concentration with slope 0.
    """
    w_obs = valid.astype(np.float64) * counts[:, None]
    n_eff = w_obs.sum(axis=0)
    weak = n_eff < MIN_FIT_YEARS
    n_safe = np.maximum(n_eff, 1.0)
    x_mean = (w_obs * x[:, None]).sum(axis=0) / n_safe
    x_centered = x[:, None] - x_mean
    y_bar = np.clip((w_obs * y).sum(axis=0) / n_safe, 0.001, 0.999)
    intercept = np.log(y_bar / (1.0 - y_bar))
    slope = np.zeros_like(intercept)
    w_obs = np.where(weak[None, :], 0.0, w_obs)
    for _ in range(12):
        eta = np.clip(intercept[None, :] + slope[None, :] * x_centered, -30.0, 30.0)
        probability = np.clip(_sigmoid(eta), 1e-6, 1.0 - 1e-6)
        variance = probability * (1.0 - probability)
        weight = w_obs * variance
        working = eta + (y - probability) / variance
        sw = weight.sum(axis=0) + 1e-8
        sx = (weight * x_centered).sum(axis=0)
        sx2 = (weight * x_centered * x_centered).sum(axis=0) + 1e-8
        sz = (weight * working).sum(axis=0)
        sxz = (weight * x_centered * working).sum(axis=0)
        det = sw * sx2 - sx * sx
        det = np.where(np.abs(det) < 1e-18, 1.0, det)
        new_intercept = (sx2 * sz - sx * sxz) / det
        new_slope = (sw * sxz - sx * sz) / det
        new_intercept = np.where(weak, intercept, new_intercept)
        new_slope = np.where(weak, 0.0, new_slope)
        if (
            np.nanmax(np.abs(new_intercept - intercept)) < 1e-6
            and np.nanmax(np.abs(new_slope - slope)) < 1e-8
        ):
            intercept, slope = new_intercept, new_slope
            break
        intercept, slope = new_intercept, new_slope
    baseline = np.clip(original_mean_pct / 100.0, 0.001, 0.999)
    intercept = np.where(weak, np.log(baseline / (1.0 - baseline)), intercept)
    slope = np.where(weak, 0.0, slope)
    x_mean = np.where(weak, 0.0, x_mean)
    intercept = np.nan_to_num(intercept, nan=0.0, posinf=30.0, neginf=-30.0)
    slope = np.nan_to_num(slope, nan=0.0, posinf=0.0, neginf=0.0)
    x_mean = np.nan_to_num(x_mean, nan=0.0)
    return intercept, slope, x_mean


def _irls_draws(y, valid, x, counts, original_mean_pct):
    """Reference bootstrap. ``counts`` is (draws, years)."""
    draws = counts.shape[0]
    cells = y.shape[1]
    intercept = np.zeros((draws, cells), dtype=np.float64)
    slope = np.zeros((draws, cells), dtype=np.float64)
    x_mean = np.zeros((draws, cells), dtype=np.float64)
    for draw in range(draws):
        intercept[draw], slope[draw], x_mean[draw] = _irls(y, valid, x, counts[draw], original_mean_pct)
    return intercept, slope, x_mean


if _HAVE_NUMBA:

    @njit(parallel=True, cache=True)
    def _irls_draws_numba(y, valid, x, counts, original):
        """Same fractional logit as ``_irls``, all bootstrap draws in one pass.

        A cell whose valid observations are identical has slope 0, which is the
        MLE, so those cells skip the iteration. Twelve iterations match the
        numpy loop's ceiling; it has already converged by then.
        """
        n_years, n_cells = y.shape
        n_draws = counts.shape[0]
        intercept = np.zeros((n_draws, n_cells))
        slope = np.zeros((n_draws, n_cells))
        x_mean = np.zeros((n_draws, n_cells))
        for cell in prange(n_cells):
            n_valid = 0
            first = 0.0
            constant = True
            for year in range(n_years):
                if not valid[year, cell]:
                    continue
                if n_valid == 0:
                    first = y[year, cell]
                elif y[year, cell] != first:
                    constant = False
                n_valid += 1
            if n_valid >= MIN_FIT_YEARS and constant:
                if first < 0.001:
                    first = 0.001
                elif first > 0.999:
                    first = 0.999
                level = np.log(first / (1.0 - first))
                for draw in range(n_draws):
                    intercept[draw, cell] = level
                continue
            for draw in range(n_draws):
                n_eff = 0.0
                sum_wx = 0.0
                sum_wy = 0.0
                for year in range(n_years):
                    if not valid[year, cell]:
                        continue
                    weight = counts[draw, year]
                    n_eff += weight
                    sum_wx += weight * x[year]
                    sum_wy += weight * y[year, cell]
                if n_eff < MIN_FIT_YEARS:
                    baseline = original[cell] / 100.0
                    if baseline < 0.001:
                        baseline = 0.001
                    elif baseline > 0.999:
                        baseline = 0.999
                    intercept[draw, cell] = np.log(baseline / (1.0 - baseline))
                    continue
                xm = sum_wx / n_eff
                y_bar = sum_wy / n_eff
                if y_bar < 0.001:
                    y_bar = 0.001
                elif y_bar > 0.999:
                    y_bar = 0.999
                a = np.log(y_bar / (1.0 - y_bar))
                b = 0.0
                for _iteration in range(12):
                    sw = 1e-8
                    sx = 0.0
                    sx2 = 1e-8
                    sz = 0.0
                    sxz = 0.0
                    for year in range(n_years):
                        if not valid[year, cell]:
                            continue
                        w_obs = counts[draw, year]
                        if w_obs == 0.0:
                            continue
                        xc = x[year] - xm
                        eta = a + b * xc
                        if eta > 30.0:
                            eta = 30.0
                        elif eta < -30.0:
                            eta = -30.0
                        if eta >= 0.0:
                            exp_term = np.exp(-eta)
                            probability = 1.0 / (1.0 + exp_term)
                        else:
                            exp_term = np.exp(eta)
                            probability = exp_term / (1.0 + exp_term)
                        if probability < 1e-6:
                            probability = 1e-6
                        elif probability > 1.0 - 1e-6:
                            probability = 1.0 - 1e-6
                        variance = probability * (1.0 - probability)
                        weight = w_obs * variance
                        working = eta + (y[year, cell] - probability) / variance
                        sw += weight
                        sx += weight * xc
                        sx2 += weight * xc * xc
                        sz += weight * working
                        sxz += weight * xc * working
                    det = sw * sx2 - sx * sx
                    if det < 1e-18 and det > -1e-18:
                        det = 1.0
                    a = (sx2 * sz - sx * sxz) / det
                    b = (sw * sxz - sx * sz) / det
                if not np.isfinite(a):
                    a = 0.0
                elif a > 30.0:
                    a = 30.0
                elif a < -30.0:
                    a = -30.0
                if not np.isfinite(b):
                    b = 0.0
                intercept[draw, cell] = a
                slope[draw, cell] = b
                x_mean[draw, cell] = xm
        return intercept, slope, x_mean

else:  # pragma: no cover
    _irls_draws_numba = None


def _bootstrap_coefficients(y, valid, x, counts, original_mean_pct):
    """40-draw IRLS. Numba is used when it imports; the numpy loop is the reference."""
    if _irls_draws_numba is not None:
        try:
            return _irls_draws_numba(
                np.ascontiguousarray(y, dtype=np.float64),
                np.ascontiguousarray(valid, dtype=np.bool_),
                np.ascontiguousarray(x, dtype=np.float64),
                np.ascontiguousarray(counts, dtype=np.float64),
                np.ascontiguousarray(original_mean_pct, dtype=np.float64),
            )
        except Exception:
            pass
    return _irls_draws(y, valid, x, counts, original_mean_pct)


def _lstsq_quadratic(times: np.ndarray, masses: np.ndarray) -> tuple[float, float, float, float]:
    """Return ``c, r, a, t0`` for ``M = c + r (t - t0) + 0.5 a (t - t0)^2``."""
    t0 = float(np.mean(times))
    dt = times - t0
    n = int(times.size)
    varying = bool(np.any(np.abs(dt) > 1e-9))
    if n >= 3 and varying:
        design = np.column_stack((np.ones(n), dt, 0.5 * dt * dt))
    elif n >= 2 and varying:
        design = np.column_stack((np.ones(n), dt))
    else:
        return float(np.mean(masses)), 0.0, 0.0, t0
    coef, *_ = np.linalg.lstsq(design, masses, rcond=None)
    if not np.all(np.isfinite(coef)):
        return float(np.mean(masses)), 0.0, 0.0, t0
    intercept = float(coef[0])
    rate = float(coef[1])
    acceleration = float(coef[2]) if coef.size > 2 else 0.0
    return intercept, rate, acceleration, t0


def _nearest_mass(times: np.ndarray, masses: np.ndarray, year: float) -> float:
    distance = np.abs(times - year)
    # Primary key is distance; an exact tie keeps the earlier sample.
    order = np.lexsort((times, distance))
    return float(masses[int(order[0])])


def _mass_mm(
    times: np.ndarray,
    masses: np.ndarray,
    intercept: float,
    rate: float,
    acceleration: float,
    t0: float,
    last: float,
    year: float,
    scale: float,
) -> float:
    first = float(masses[0])
    if year <= last:
        mass = _nearest_mass(times, masses, year)
    else:
        dt = year - t0
        mass = intercept + rate * dt + 0.5 * acceleration * dt * dt * scale
    return -(mass - first) / GT_PER_MM


class _MonthModel:
    """Bootstrap coefficients for one pole and calendar month."""

    def __init__(
        self,
        height: int,
        width: int,
        area: np.ndarray,
        fitted_idx: np.ndarray,
        const_idx: np.ndarray,
        const_pct: np.ndarray,
        intercept: np.ndarray,
        slope: np.ndarray,
        x_mean: np.ndarray,
        pole_hole_cells: int,
    ) -> None:
        self.height = height
        self.width = width
        self.area = area
        self.fitted_idx = fitted_idx
        self.const_idx = const_idx
        self.const_pct = const_pct
        self.intercept = intercept
        self.slope = slope
        self.x_mean = x_mean
        self.pole_hole_cells = pole_hole_cells
        self.extent_scale = 1.0

    def realize(self, x: float) -> tuple[dict[str, np.ndarray], np.ndarray, dict[str, float]]:
        """Percentile grids, unscaled bootstrap extents (40,), and raw extent of each band."""
        flat = {
            band: np.full(self.height * self.width, 255, dtype=np.uint8) for band in BANDS
        }
        draw_raw = np.zeros(N_BOOT, dtype=np.float64)
        if self.fitted_idx.size:
            eta = self.intercept.astype(np.float64) + self.slope.astype(np.float64) * (
                float(x) - self.x_mean.astype(np.float64)
            )
            probability = _sigmoid(eta)
            rounded = np.clip(np.rint(100.0 * probability), 0.0, 100.0)
            low, median, high = np.percentile(rounded, [10, 50, 90], axis=0, method="linear")
            for band, values in zip(BANDS, (low, median, high), strict=True):
                flat[band][self.fitted_idx] = np.clip(np.rint(values), 0.0, 100.0).astype(np.uint8)
            area = self.area.ravel()[self.fitted_idx]
            draw_raw += (rounded >= EXTENT_THRESHOLD).astype(np.float64) @ area
        if self.const_idx.size:
            const = np.clip(np.rint(self.const_pct), 0.0, 100.0).astype(np.uint8)
            for band in BANDS:
                flat[band][self.const_idx] = const
            area = self.area.ravel()[self.const_idx]
            draw_raw += float(((const.astype(np.float64) >= EXTENT_THRESHOLD) * area).sum())
        bands = {band: values.reshape(self.height, self.width) for band, values in flat.items()}
        raw_by_band = {
            band: float((((grid >= EXTENT_THRESHOLD) & (grid <= 100)) * self.area).sum())
            for band, grid in bands.items()
        }
        return bands, draw_raw, raw_by_band

    def extents_for(self, xs: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Unscaled median-grid extent and p10/p90 bootstrap extents for each driver value.

        Matches ``realize``: ``median`` is the cell-sum of the p50 grid, while
        ``low`` / ``high`` are percentiles of the 40 per-draw cell-sums.
        """
        xs = np.asarray(xs, dtype=np.float64).reshape(-1)
        n_years = int(xs.size)
        median_raw = np.zeros(n_years, dtype=np.float64)
        draw_raw = np.zeros((n_years, N_BOOT), dtype=np.float64)
        if n_years == 0:
            return median_raw, median_raw.copy(), median_raw.copy()
        if self.fitted_idx.size:
            area = self.area.ravel()[self.fitted_idx]
            # A few years at a time keeps the (years, draws, cells) tensor small.
            chunk = 4
            for start in range(0, n_years, chunk):
                end = min(n_years, start + chunk)
                x_chunk = xs[start:end]
                delta = x_chunk[:, None, None] - self.x_mean[None, :, :]
                eta = self.intercept[None, :, :] + self.slope[None, :, :] * delta
                rounded = np.clip(np.rint(100.0 * _sigmoid(eta)), 0.0, 100.0)
                draw_raw[start:end] = (rounded >= EXTENT_THRESHOLD) @ area
                median = np.percentile(rounded, 50, axis=1, method="linear")
                median = np.clip(np.rint(median), 0.0, 100.0)
                median_raw[start:end] = ((median >= EXTENT_THRESHOLD) * area).sum(axis=1)
        if self.const_idx.size:
            const = np.clip(np.rint(self.const_pct), 0.0, 100.0)
            area = self.area.ravel()[self.const_idx]
            extra = float(((const >= EXTENT_THRESHOLD) * area).sum())
            draw_raw += extra
            median_raw += extra
        if self.fitted_idx.size or self.const_idx.size:
            low = np.percentile(draw_raw, 10, axis=1, method="linear")
            high = np.percentile(draw_raw, 90, axis=1, method="linear")
        else:
            low = np.zeros(n_years, dtype=np.float64)
            high = np.zeros(n_years, dtype=np.float64)
        return median_raw, low, high


class _SheetCurve:
    def __init__(self, points: list[MassPoint]) -> None:
        ordered = sorted(points, key=lambda point: (point.decimal_year, point.mass_gt))
        self.times = np.asarray([point.decimal_year for point in ordered], dtype=np.float64)
        self.masses = np.asarray([point.mass_gt for point in ordered], dtype=np.float64)
        self.intercept, self.rate, self.acceleration, self.t0 = _lstsq_quadratic(self.times, self.masses)
        dt = self.times - self.t0
        self.fitted = self.intercept + self.rate * dt + 0.5 * self.acceleration * dt * dt
        self.resid = self.masses - self.fitted
        self.last = float(self.times[-1])

    def point_mm(self, year: float, scale: float) -> float:
        return _mass_mm(
            self.times,
            self.masses,
            self.intercept,
            self.rate,
            self.acceleration,
            self.t0,
            self.last,
            year,
            scale,
        )

    def bootstrap_mm(self, year: float, scale: float, rng: np.random.Generator) -> np.ndarray:
        n = int(self.times.size)
        out = np.empty(N_BOOT, dtype=np.float64)
        for draw in range(N_BOOT):
            index = rng.integers(0, n, size=n)
            starred = self.fitted + self.resid[index]
            intercept, rate, acceleration, t0 = _lstsq_quadratic(self.times, starred)
            out[draw] = _mass_mm(
                self.times,
                starred,
                intercept,
                rate,
                acceleration,
                t0,
                self.last,
                year,
                scale,
            )
        return out


def _grid_array(grid: MonthlyGrid) -> np.ndarray | None:
    if grid.width <= 0 or grid.height <= 0:
        return None
    values = np.frombuffer(grid.values_u8, dtype=np.uint8)
    if values.size != grid.width * grid.height:
        return None
    return values.reshape(grid.height, grid.width)


def _dominant_shape(grids: list[MonthlyGrid]) -> tuple[tuple[int, int], list[MonthlyGrid]]:
    counts: Counter[tuple[int, int]] = Counter((grid.height, grid.width) for grid in grids)
    shape = counts.most_common(1)[0][0]
    members = [grid for grid in grids if (grid.height, grid.width) == shape]
    members.sort(key=lambda grid: grid.year)
    return shape, members


def _empty_season() -> ShippingSeason:
    return ShippingSeason(0, None, 0)


def _zero_score() -> PoleScore:
    return PoleScore(0.0, 0.0, 0, 0.0)


class IceModel:
    def __init__(self) -> None:
        # None fits every month. Backtest sets this to {9} so only September,
        # the scored month, materializes coefficients. Earlier months still
        # consume the bootstrap stream so September's draws stay put.
        self._materialize_months: set[int] | None = None
        self._clear()

    def _clear(self) -> None:
        self._grids: dict[tuple[str, int, int], MonthlyGrid] = {}
        self._all_grids: list[MonthlyGrid] = []
        self._hist_co2: dict[int, float] = {}
        self._ssp_co2: dict[str, dict[int, float]] = {}
        self._ssp_warming: dict[str, dict[int, float]] = {}
        self._grace_g: list[MassPoint] = []
        self._grace_a: list[MassPoint] = []
        self._cell_area: dict[str, list[float]] | None = None
        self._regressor: Literal["cumulative_co2_gt", "ipcc_ar6_warming_c_fallback"] = "cumulative_co2_gt"
        self._models: dict[tuple[str, int], _MonthModel] = {}
        self._dims: dict[str, tuple[int, int]] = {}
        self._observed_through: tuple[int, int] | None = None
        self._lat: np.ndarray | None = None
        self._lon: np.ndarray | None = None
        self._greenland: _SheetCurve | None = None
        self._antarctica: _SheetCurve | None = None
        self._extent_cache: dict = {}
        self._impact_cache: dict = {}
        self._conc_cache: dict = {}
        self._backtest_cache: BacktestResult | None = None

    def update(self, bundle: TrainingBundle) -> None:
        """Fit per-cell, per-month logistic curves and the ice-sheet quadratic.

        Must not raise because a list is empty. An empty grid list leaves
        concentration() returning a masked grid with source 'model-not-ready'.
        """
        self._clear()
        self._hist_co2 = {int(year): float(value) for year, value in bundle.cumulative_co2_gt_by_year.items()}
        self._ssp_co2 = {
            str(scenario): {int(year): float(value) for year, value in (series or {}).items()}
            for scenario, series in bundle.ssp_cumulative_co2_gt.items()
        }
        self._ssp_warming = {
            str(scenario): {int(year): float(value) for year, value in (series or {}).items()}
            for scenario, series in bundle.ssp_warming_c.items()
        }
        self._grace_g = list(bundle.grace_greenland)
        self._grace_a = list(bundle.grace_antarctica)
        self._cell_area = bundle.cell_area_km2
        self._regressor = bundle.regressor
        self._all_grids = list(bundle.grids)
        self._index_grids()
        if self._observed_through is not None:
            self._fit_concentration()
        self._greenland = _fit_sheet(self._grace_g)
        self._antarctica = _fit_sheet(self._grace_a)

    def _index_grids(self) -> None:
        indexed: dict[tuple[str, int, int], MonthlyGrid] = {}
        shapes: dict[str, Counter[tuple[int, int]]] = {"north": Counter(), "south": Counter()}
        for grid in self._all_grids:
            indexed[(grid.pole, grid.year, grid.month)] = grid
            shapes[grid.pole][(grid.height, grid.width)] += 1
        self._grids = indexed
        for pole, counts in shapes.items():
            if counts:
                self._dims[pole] = counts.most_common(1)[0][0]
        observed = [
            (grid.year, grid.month)
            for grid in indexed.values()
            if not grid.gap_masked
        ]
        self._observed_through = max(observed) if observed else None
        if any(
            grid.pole == "north" and grid.height == NORTH_HEIGHT and grid.width == NORTH_WIDTH
            for grid in indexed.values()
        ):
            self._lat, self._lon = _north_latlon()

    def _fit_concentration(self) -> None:
        grouped: dict[tuple[str, int], list[MonthlyGrid]] = defaultdict(list)
        for grid in self._grids.values():
            if grid.gap_masked or _grid_array(grid) is None:
                continue
            grouped[(grid.pole, grid.month)].append(grid)
        # North then south, month 1..12, so Generator(0) is stable for a bundle.
        rng = np.random.default_rng(BOOT_SEED)
        for pole in ("north", "south"):
            for month in range(1, 13):
                members = grouped.get((pole, month))
                if not members:
                    continue
                _shape, usable = _dominant_shape(members)
                materialize = self._materialize_months is None or month in self._materialize_months
                fitted = self._fit_month(pole, month, usable, rng, materialize=materialize)
                if fitted is not None:
                    self._models[(pole, month)] = fitted

    def _fit_month(
        self,
        pole: str,
        month: int,
        grids: list[MonthlyGrid],
        rng: np.random.Generator,
        materialize: bool = True,
    ) -> _MonthModel | None:
        height, width = grids[0].height, grids[0].width
        stack = np.stack([_grid_array(grid) for grid in grids])
        valid = stack <= 100
        count = valid.sum(axis=0)
        percent = stack.astype(np.float64)
        years = [grid.year for grid in grids]
        x = np.asarray(
            [self._x_at(year, "ssp245", None, training=True) for year in years],
            dtype=np.float64,
        )
        sum_percent = np.where(valid, percent, 0.0).sum(axis=0)
        mean_percent = np.zeros((height, width), dtype=np.float64)
        np.divide(sum_percent, count, out=mean_percent, where=count > 0)
        fitted_mask = count >= MIN_FIT_YEARS
        const_mask = (count > 0) & ~fitted_mask
        fitted_idx = np.flatnonzero(fitted_mask)
        const_idx = np.flatnonzero(const_mask)
        const_pct = np.clip(np.rint(mean_percent[const_mask]), 0.0, 100.0)
        intercept = np.zeros((N_BOOT, fitted_idx.size), dtype=np.float64)
        slope = np.zeros((N_BOOT, fitted_idx.size), dtype=np.float64)
        x_mean = np.zeros((N_BOOT, fitted_idx.size), dtype=np.float64)
        if fitted_idx.size:
            fraction = np.clip(percent / 100.0, 0.001, 0.999)
            y_fit = fraction[:, fitted_mask]
            v_fit = valid[:, fitted_mask]
            original = mean_percent[fitted_mask]
            # Drawn even when this month is not materialized, so a later month
            # sees the same Generator(0) stream as a full fit.
            draws = rng.integers(0, len(years), size=(N_BOOT, len(years)))
            if materialize:
                counts = np.vstack(
                    [np.bincount(draws[draw], minlength=len(years)).astype(np.float64) for draw in range(N_BOOT)]
                )
                intercept, slope, x_mean = _bootstrap_coefficients(y_fit, v_fit, x, counts, original)
        if not materialize:
            return None
        latest = grids[-1]
        model = _MonthModel(
            height=height,
            width=width,
            area=self._area(pole, height, width),
            fitted_idx=fitted_idx,
            const_idx=const_idx,
            const_pct=const_pct,
            intercept=intercept,
            slope=slope,
            x_mean=x_mean,
            pole_hole_cells=int(latest.pole_hole_cells),
        )
        model.extent_scale = self._extent_scale(grids, model)
        return model

    def _area(self, pole: str, height: int, width: int) -> np.ndarray:
        table = self._cell_area or {}
        values = table.get(pole)
        if values is not None:
            array = np.asarray(values, dtype=np.float64)
            if array.size == height * width:
                return array.reshape(height, width)
        return np.full((height, width), CELL_AREA_KM2, dtype=np.float64)

    def _extent_scale(self, grids: list[MonthlyGrid], model: _MonthModel) -> float:
        by_year = {grid.year: grid for grid in grids}
        observed: list[float] = []
        xs: list[float] = []
        for year in range(SCALE_YEAR_MIN, SCALE_YEAR_MAX + 1):
            grid = by_year.get(year)
            if grid is None or grid.extent_km2 is None:
                continue
            xs.append(self._x_at(year, "ssp245", None, training=True))
            observed.append(float(grid.extent_km2))
        if not observed:
            return 1.0
        modeled, _low, _high = model.extents_for(np.asarray(xs, dtype=np.float64))
        mean_modeled = float(np.mean(modeled))
        if mean_modeled <= 0.0:
            return 1.0
        return float(np.mean(observed) / mean_modeled)

    def _x_at(self, year: int, scenario: str, blend: float | None, *, training: bool) -> float:
        if self._regressor == "ipcc_ar6_warming_c_fallback":
            if training:
                return self._training_warming(year)
            return self._warming_value(year, scenario, blend)
        return self._co2_value(year, scenario, blend)

    def _training_warming(self, year: int) -> float:
        # Historical AR6 warming is a single observed path. Average the pathways
        # that actually provide this year so the fit does not depend on a scenario.
        values = [
            float(series[year])
            for scenario in SCENARIO_IDS
            if year in (series := self._ssp_warming.get(scenario) or {})
        ]
        if values:
            return float(np.mean(values))
        return self._warming_value(year, "ssp245", None)

    def _warming_value(self, year: int, scenario: str, blend: float | None) -> float:
        value = _hold(_blend_series(self._ssp_warming, scenario, blend), year)
        return 0.0 if value is None else value

    def _co2_value(self, year: int, scenario: str, blend: float | None) -> float:
        if year in self._hist_co2:
            return float(self._hist_co2[year])
        ssp = _blend_series(self._ssp_co2, scenario, blend)
        if year in ssp:
            return float(ssp[year])
        candidates: list[tuple[int, float]] = []
        for key, value in self._hist_co2.items():
            if key <= year:
                candidates.append((key, float(value)))
        for key, value in ssp.items():
            if key <= year:
                candidates.append((key, float(value)))
        if candidates:
            # Same year in both series: historical was appended first and wins the tie.
            return max(candidates, key=lambda item: item[0])[1]
        available = set(self._hist_co2) | set(ssp)
        if not available:
            return 0.0
        earliest = min(available)
        if earliest in self._hist_co2:
            return float(self._hist_co2[earliest])
        return float(ssp[earliest])

    def _warming_scale(self, year: int, year_ref: int, scenario: str, blend: float | None) -> float:
        series = _blend_series(self._ssp_warming, scenario, blend)
        current = _hold(series, year)
        reference = _hold(series, year_ref)
        if current is None or reference is None or reference == 0.0:
            return 1.0
        return max(current / reference, 0.0)

    def concentration(self, req: GridRequest) -> ConcentrationGrid:
        blend_key = None if req.blend is None else round(float(req.blend), 6)
        key = (req.pole, req.year, req.month, req.scenario, blend_key, req.band)
        cached = self._conc_cache.get(key)
        if cached is not None:
            return cached
        result = self._concentration(req)
        self._conc_cache[key] = result
        return result

    def _concentration(self, req: GridRequest) -> ConcentrationGrid:
        """Observed year-month: return the satellite bytes for every band.

        Later year-month: logistic prediction at the blended regressor.
        Gap: all 255, extent_km2 None, gap_masked True.
        """
        grid = self._grids.get((req.pole, req.year, req.month))
        if grid is not None and grid.gap_masked:
            return _masked(
                grid.width,
                grid.height,
                source=SOURCE_NSIDC,
                is_forecast=False,
                gap=True,
                extent=None,
                scale=1.0,
                hole=grid.pole_hole_cells,
            )
        if grid is not None:
            return ConcentrationGrid(
                width=grid.width,
                height=grid.height,
                values_u8=grid.values_u8,
                extent_km2=grid.extent_km2,
                extent_scale=1.0,
                is_forecast=False,
                gap_masked=False,
                pole_hole_cells=grid.pole_hole_cells,
                source=SOURCE_NSIDC,
            )
        if self._observed_through is None:
            return self._not_ready(req.pole)
        if (req.year, req.month) <= self._observed_through:
            height, width = self._dims.get(req.pole, (1, 1))
            return _masked(
                width,
                height,
                source=SOURCE_NSIDC,
                is_forecast=False,
                gap=False,
                extent=None,
                scale=1.0,
                hole=0,
            )
        month_model = self._models.get((req.pole, req.month))
        if month_model is None:
            if req.pole not in self._dims:
                return self._not_ready(req.pole)
            height, width = self._dims[req.pole]
            return _masked(
                width,
                height,
                source=SOURCE_FORECAST,
                is_forecast=True,
                gap=False,
                extent=None,
                scale=1.0,
                hole=0,
            )
        return self._forecast(month_model, req)

    def _not_ready(self, pole: str) -> ConcentrationGrid:
        if pole in self._dims:
            height, width = self._dims[pole]
        else:
            height, width = 1, 1
        return _masked(
            width,
            height,
            source=SOURCE_NOT_READY,
            is_forecast=False,
            gap=False,
            extent=None,
            scale=1.0,
            hole=0,
        )

    def _forecast(self, model: _MonthModel, req: GridRequest) -> ConcentrationGrid:
        x = self._x_at(req.year, req.scenario, req.blend, training=False)
        bands, _draw_raw, raw_by_band = model.realize(x)
        band = req.band if req.band in BANDS else "median"
        grid = bands[band]
        return ConcentrationGrid(
            width=model.width,
            height=model.height,
            values_u8=np.ascontiguousarray(grid).tobytes(),
            extent_km2=raw_by_band[band] * model.extent_scale,
            extent_scale=model.extent_scale,
            is_forecast=True,
            gap_masked=False,
            pole_hole_cells=model.pole_hole_cells,
            source=SOURCE_FORECAST,
        )

    def extent_series(
        self,
        pole: Pole,
        month: int,
        scenario: ScenarioId,
        blend: float | None,
    ) -> list[ExtentPoint]:
        """1979..2100 inclusive for that pole and calendar month."""
        blend_key = None if blend is None else round(float(blend), 6)
        key = (pole, month, scenario, blend_key)
        cached = self._extent_cache.get(key)
        if cached is not None:
            return cached
        points = self._extent_series(pole, month, scenario, blend)
        self._extent_cache[key] = points
        return points

    def _extent_series(
        self,
        pole: Pole,
        month: int,
        scenario: ScenarioId,
        blend: float | None,
    ) -> list[ExtentPoint]:
        month_model = self._models.get((pole, month))
        points: list[ExtentPoint | None] = [None] * (YEAR_MAX - YEAR_MIN + 1)
        forecast_years: list[int] = []
        forecast_x: list[float] = []
        for year in range(YEAR_MIN, YEAR_MAX + 1):
            grid = self._grids.get((pole, year, month))
            slot = year - YEAR_MIN
            if grid is not None and grid.gap_masked:
                points[slot] = ExtentPoint(year, None, None, None, False, True, SOURCE_NSIDC, METHOD_CSV)
                continue
            if grid is not None:
                points[slot] = ExtentPoint(
                    year,
                    grid.extent_km2,
                    None,
                    None,
                    False,
                    False,
                    SOURCE_NSIDC,
                    METHOD_CSV,
                )
                continue
            if self._observed_through is None:
                points[slot] = ExtentPoint(year, None, None, None, False, False, SOURCE_NOT_READY, METHOD_MODEL)
                continue
            if (year, month) <= self._observed_through:
                points[slot] = ExtentPoint(year, None, None, None, False, False, SOURCE_NSIDC, METHOD_CSV)
                continue
            if month_model is None:
                points[slot] = ExtentPoint(year, None, None, None, True, False, SOURCE_FORECAST, METHOD_MODEL)
                continue
            forecast_years.append(year)
            forecast_x.append(self._x_at(year, scenario, blend, training=False))
        if forecast_years and month_model is not None:
            median, low, high = month_model.extents_for(np.asarray(forecast_x, dtype=np.float64))
            scale = month_model.extent_scale
            for index, year in enumerate(forecast_years):
                points[year - YEAR_MIN] = ExtentPoint(
                    year,
                    float(median[index]) * scale,
                    float(low[index]) * scale,
                    float(high[index]) * scale,
                    True,
                    False,
                    SOURCE_FORECAST,
                    METHOD_MODEL,
                )
        return [point for point in points if point is not None]

    def impact(self, year: int, scenario: ScenarioId, blend: float | None) -> ImpactResult:
        """Median ice. Sea-level low/high from the mass residual bootstrap."""
        blend_key = None if blend is None else round(float(blend), 6)
        key = (int(year), scenario, blend_key)
        cached = self._impact_cache.get(key)
        if cached is not None:
            return cached
        result = self._impact(year, scenario, blend)
        self._impact_cache[key] = result
        return result

    def _impact(self, year: int, scenario: ScenarioId, blend: float | None) -> ImpactResult:
        northern, northwest = self._shipping(year, scenario, blend)
        greenland_mm, antarctica_mm, low_mm, high_mm, source = self._sea_level(year, scenario, blend)
        return ImpactResult(
            is_forecast=self._year_is_forecast(year),
            northern_sea_route=northern,
            northwest_passage=northwest,
            ice_free_september_year=self._ice_free_year(scenario, blend),
            winter_ice_loss_index=self._winter_index(year, scenario, blend),
            greenland_mm=greenland_mm,
            antarctica_mm=antarctica_mm,
            total_mm=greenland_mm + antarctica_mm,
            low_mm=low_mm,
            high_mm=high_mm,
            sea_level_source=source,
        )

    def _year_is_forecast(self, year: int) -> bool:
        if self._observed_through is None:
            return False
        observed_year, observed_month = self._observed_through
        return year > observed_year or (year == observed_year and observed_month < 12)

    def _ice_free_year(self, scenario: str, blend: float | None) -> int | None:
        for point in self.extent_series("north", 9, scenario, blend):  # type: ignore[arg-type]
            if point.extent_km2 is not None and point.extent_km2 < 1_000_000.0:
                return point.year
        return None

    def _shipping(
        self,
        year: int,
        scenario: str,
        blend: float | None,
    ) -> tuple[ShippingSeason, ShippingSeason]:
        if self._lat is None or self._lon is None:
            return _empty_season(), _empty_season()
        northern = self._season(year, scenario, blend, 66.0, 80.0, 30.0, 180.0, dateline=True)
        northwest = self._season(year, scenario, blend, 68.0, 75.0, -140.0, -60.0, dateline=False)
        return northern, northwest

    def _season(
        self,
        year: int,
        scenario: str,
        blend: float | None,
        lat_min: float,
        lat_max: float,
        lon_min: float,
        lon_max: float,
        *,
        dateline: bool,
    ) -> ShippingSeason:
        assert self._lat is not None and self._lon is not None
        box = (self._lat >= lat_min) & (self._lat <= lat_max) & (self._lon >= lon_min) & (self._lon <= lon_max)
        if dateline:
            # Longitude 180 and -180 are the same meridian, the eastern edge of the NSR box.
            box = box | ((self._lat >= lat_min) & (self._lat <= lat_max) & np.isclose(self._lon, -180.0))
        open_months: list[int] = []
        for month in range(1, 13):
            grid = self._median_array("north", year, month, scenario, blend)
            if grid is None or grid.shape != self._lat.shape:
                continue
            values = grid[box]
            values = values[values <= 100]
            if values.size == 0:
                continue
            if float(values.mean()) < EXTENT_THRESHOLD:
                open_months.append(month)
        opening = None
        for month in open_months:
            if month >= 6:
                opening = f"{year:04d}-{month:02d}-01"
                break
        days = sum(28 if month == 2 else 30 for month in open_months)
        return ShippingSeason(days, opening, len(open_months))

    def _winter_index(self, year: int, scenario: str, blend: float | None) -> float | None:
        if self._lat is None:
            return None
        height, width = self._lat.shape
        total = np.zeros((height, width), dtype=np.float64)
        count = np.zeros((height, width), dtype=np.int16)
        for clim_year in range(1981, 2011):
            grid = self._grids.get(("north", clim_year, 2))
            if grid is None or grid.gap_masked or grid.height != height or grid.width != width:
                continue
            values = np.frombuffer(grid.values_u8, dtype=np.uint8).reshape(height, width)
            open_mask = values <= 100
            total[open_mask] += values[open_mask]
            count[open_mask] += 1
        if not np.any(count > 0):
            return None
        mean = np.zeros_like(total)
        np.divide(total, count, out=mean, where=count > 0)
        target = self._median_array("north", year, 2, scenario, blend)
        if target is None or target.shape != self._lat.shape:
            return None
        eligible = (self._lat >= 60.0) & (count > 0) & (mean >= EXTENT_THRESHOLD) & (target <= 100)
        n_eligible = int(eligible.sum())
        if n_eligible == 0:
            return None
        lost = int((eligible & (target < EXTENT_THRESHOLD)).sum())
        return lost / n_eligible

    def _median_array(
        self,
        pole: str,
        year: int,
        month: int,
        scenario: str,
        blend: float | None,
    ) -> np.ndarray | None:
        grid = self.concentration(
            GridRequest(
                pole,  # type: ignore[arg-type]
                year,
                month,
                scenario,  # type: ignore[arg-type]
                blend,
                "median",
            )
        )
        if grid.source == SOURCE_NOT_READY or grid.width <= 0 or grid.height <= 0:
            return None
        values = np.frombuffer(grid.values_u8, dtype=np.uint8)
        if values.size != grid.width * grid.height:
            return None
        return values.reshape(grid.height, grid.width).copy()

    def _sea_level(
        self,
        year: int,
        scenario: str,
        blend: float | None,
    ) -> tuple[float, float, float, float, Literal["grace-gracefo", "nasa-science-summary-fallback"]]:
        # Greenland draws come first so the seed stream does not depend on call order.
        rng = np.random.default_rng(BOOT_SEED)
        greenland_mm, greenland_draws = self._sheet_contribution(
            self._greenland, GREENLAND_RATE_GT_YR, year, scenario, blend, rng
        )
        antarctica_mm, antarctica_draws = self._sheet_contribution(
            self._antarctica, ANTARCTICA_RATE_GT_YR, year, scenario, blend, rng
        )
        combined = greenland_draws + antarctica_draws
        low, high = np.percentile(combined, [10, 90], method="linear")
        source: Literal["grace-gracefo", "nasa-science-summary-fallback"]
        if self._greenland is None and self._antarctica is None:
            source = SOURCE_NASA  # type: ignore[assignment]
        else:
            source = SOURCE_GRACE  # type: ignore[assignment]
        return greenland_mm, antarctica_mm, float(low), float(high), source

    def _sheet_contribution(
        self,
        curve: _SheetCurve | None,
        fallback_rate: float,
        year: int,
        scenario: str,
        blend: float | None,
        rng: np.random.Generator,
    ) -> tuple[float, np.ndarray]:
        if curve is None:
            mm = -self._fallback_mass(year, fallback_rate, scenario, blend) / GT_PER_MM
            return mm, np.full(N_BOOT, mm, dtype=np.float64)
        scale = 1.0
        if float(year) > curve.last:
            scale = self._warming_scale(year, int(math.floor(curve.last + 1e-9)), scenario, blend)
        mm = curve.point_mm(float(year), scale)
        return mm, curve.bootstrap_mm(float(year), scale, rng)

    def _fallback_mass(self, year: int, rate: float, scenario: str, blend: float | None) -> float:
        """NASA summary rate from 2002. After 2025 only the later segment is rescaled.

        ``M(Y) = rate * (min(Y, 2025) - 2002) + rate * scale * max(Y - 2025, 0)``
        with ``scale = max(W(Y) / W(2025), 0)`` when a warming pathway is present.
        """
        if year <= FALLBACK_START_YEAR:
            return 0.0
        if year <= FALLBACK_SCALE_YEAR:
            return rate * (year - FALLBACK_START_YEAR)
        scale = self._warming_scale(year, FALLBACK_SCALE_YEAR, scenario, blend)
        return rate * (FALLBACK_SCALE_YEAR - FALLBACK_START_YEAR) + rate * scale * (year - FALLBACK_SCALE_YEAR)

    def backtest(self) -> BacktestResult:
        """Second fit on year <= 2010. Does not replace the model update() stored."""
        if self._backtest_cache is not None:
            return self._backtest_cache
        result = self._backtest()
        self._backtest_cache = result
        return result

    def _backtest(self) -> BacktestResult:
        eval_end = self._eval_end_year()
        trainable = any((not grid.gap_masked) and grid.year <= TRAIN_END_YEAR for grid in self._all_grids)
        if not trainable:
            return BacktestResult(
                False,
                TRAIN_END_YEAR,
                EVAL_START_YEAR,
                eval_end,
                _zero_score(),
                _zero_score(),
            )
        held = IceModel()
        # September is the only scored month. Other months still advance
        # Generator(0) inside update(), they just skip the coefficient solve.
        held._materialize_months = {9}
        held.update(
            TrainingBundle(
                grids=[grid for grid in self._all_grids if grid.year <= TRAIN_END_YEAR],
                cumulative_co2_gt_by_year=dict(self._hist_co2),
                ssp_cumulative_co2_gt={key: dict(value) for key, value in self._ssp_co2.items()},
                ssp_warming_c={key: dict(value) for key, value in self._ssp_warming.items()},
                grace_greenland=list(self._grace_g),
                grace_antarctica=list(self._grace_a),
                cell_area_km2=self._cell_area,
                regressor=self._regressor,
            )
        )
        return BacktestResult(
            True,
            TRAIN_END_YEAR,
            EVAL_START_YEAR,
            eval_end,
            self._score_pole("north", held, eval_end),
            self._score_pole("south", held, eval_end),
        )

    def _eval_end_year(self) -> int:
        if self._observed_through is None:
            return TRAIN_END_YEAR
        year, month = self._observed_through
        end = year if month >= 9 else year - 1
        return end if end >= EVAL_START_YEAR else TRAIN_END_YEAR

    def _score_pole(self, pole: str, held: IceModel, eval_end: int) -> PoleScore:
        observed: list[float] = []
        predicted: list[float] = []
        for year in range(EVAL_START_YEAR, eval_end + 1):
            grid = self._grids.get((pole, year, 9))
            if grid is None or grid.gap_masked or grid.extent_km2 is None:
                continue
            forecast = held.concentration(GridRequest(pole, year, 9, "ssp245", None, "median"))  # type: ignore[arg-type]
            if forecast.extent_km2 is None:
                continue
            observed.append(float(grid.extent_km2))
            predicted.append(float(forecast.extent_km2))
        n = len(observed)
        if n == 0:
            return _zero_score()
        obs = np.asarray(observed, dtype=np.float64)
        pred = np.asarray(predicted, dtype=np.float64)
        error = pred - obs
        rmse = float(np.sqrt(np.mean(error**2)))
        bias = float(np.mean(error))
        climatology_values: list[float] = []
        for year in range(1981, 2011):
            grid = self._grids.get((pole, year, 9))
            if grid is None or grid.gap_masked or grid.extent_km2 is None:
                continue
            climatology_values.append(float(grid.extent_km2))
        if not climatology_values:
            skill = 0.0
        else:
            climatology = float(np.mean(climatology_values))
            climatology_rmse = float(np.sqrt(np.mean((climatology - obs) ** 2)))
            if climatology_rmse == 0.0:
                skill = 1.0 if rmse == 0.0 else 0.0
            else:
                skill = 1.0 - rmse / climatology_rmse
        return PoleScore(rmse, bias, n, skill)


def _fit_sheet(points: list[MassPoint]) -> _SheetCurve | None:
    if not points:
        return None
    return _SheetCurve(points)


def _masked(
    width: int,
    height: int,
    *,
    source: str,
    is_forecast: bool,
    gap: bool,
    extent: float | None,
    scale: float,
    hole: int,
) -> ConcentrationGrid:
    cells = max(width, 0) * max(height, 0)
    return ConcentrationGrid(
        width=width,
        height=height,
        values_u8=b"\xff" * cells,
        extent_km2=extent,
        extent_scale=scale,
        is_forecast=is_forecast,
        gap_masked=gap,
        pole_hole_cells=hole,
        source=source,
    )


__all__ = [
    "Pole",
    "Band",
    "ScenarioId",
    "MonthlyGrid",
    "MassPoint",
    "TrainingBundle",
    "GridRequest",
    "ConcentrationGrid",
    "ExtentPoint",
    "ShippingSeason",
    "ImpactResult",
    "PoleScore",
    "BacktestResult",
    "IceModel",
]
