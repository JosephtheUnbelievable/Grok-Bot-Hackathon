"""Linear-trend stand-in used only when `model.ice_model` cannot be imported.

Fits a line to the real NSIDC monthly extent (km² already converted). Forecast
years scale that line by the blended IPCC warming pathway relative to SSP2-4.5.
This is not the per-cell logistic model. Health keeps `model_ready` false.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from app.ssp import SCENARIOS, blended_warming

Pole = Literal["north", "south"]
Band = Literal["median", "low", "high"]
ScenarioId = Literal["ssp119", "ssp126", "ssp245", "ssp370", "ssp585"]
CELL_KM2 = 625.0
Z90 = 1.2815515655446004
GT_PER_MM = 362.0
RATE_GREENLAND = -264.0
RATE_ANTARCTICA = -135.0


@dataclass(frozen=True)
class MonthlyGrid:
    pole: Pole
    year: int
    month: int
    width: int
    height: int
    values_u8: bytes
    extent_km2: float | None
    area_km2: float | None
    gap_masked: bool
    pole_hole_cells: int


@dataclass(frozen=True)
class MassPoint:
    decimal_year: float
    mass_gt: float
    uncertainty_gt: float | None


@dataclass(frozen=True)
class TrainingBundle:
    grids: list
    cumulative_co2_gt_by_year: dict
    ssp_cumulative_co2_gt: dict
    ssp_warming_c: dict
    grace_greenland: list
    grace_antarctica: list
    cell_area_km2: dict | None
    regressor: str


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
    extent_method: str


@dataclass(frozen=True)
class ShippingSeason:
    open_season_days: int
    opening_date: str | None
    open_month_count: int
    method: str = ""


@dataclass(frozen=True)
class ImpactResult:
    is_forecast: bool
    northern_sea_route: ShippingSeason
    northwest_passage: ShippingSeason
    ice_free_september_year: int | None
    winter_ice_loss_index: float | None
    winter_method: str
    community_february: dict | None
    greenland_mm: float
    antarctica_mm: float
    total_mm: float
    low_mm: float
    high_mm: float
    sea_level_source: str


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


def _empty_season(method: str = "") -> ShippingSeason:
    return ShippingSeason(0, None, 0, method)


class FallbackIceModel:
    """Same method names as IceModel. Labeled linear trend on fetched extent."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, int, int], tuple[float | None, float | None, bool]] = {}
        self.templates: dict[tuple[str, int], MonthlyGrid] = {}
        self.by_ym: dict[tuple[str, int, int], MonthlyGrid] = {}
        self.warming: dict = {}
        self.regressor = "ipcc_ar6_warming_c_fallback"
        self.grace_g: list[MassPoint] = []
        self.grace_a: list[MassPoint] = []
        self.obs: tuple[int, int] | None = None
        self._ready = False

    def update(self, bundle: TrainingBundle) -> None:
        rows: dict[tuple[str, int, int], tuple[float | None, float | None, bool]] = {}
        templates: dict[tuple[str, int], MonthlyGrid] = {}
        by_ym: dict[tuple[str, int, int], MonthlyGrid] = {}
        obs: tuple[int, int] | None = None
        for grid in bundle.grids:
            key = (grid.pole, grid.year, grid.month)
            rows[key] = (grid.extent_km2, grid.area_km2, bool(grid.gap_masked))
            if grid.extent_km2 is not None and not grid.gap_masked:
                if obs is None or (grid.year, grid.month) > obs:
                    obs = (grid.year, grid.month)
            if grid.width >= 32 and grid.values_u8 and not grid.gap_masked:
                by_ym[key] = grid
                prev = templates.get((grid.pole, grid.month))
                if prev is None or grid.year >= prev.year:
                    templates[(grid.pole, grid.month)] = grid
        self.rows = rows
        self.templates = templates
        self.by_ym = by_ym
        self.warming = dict(bundle.ssp_warming_c or {})
        self.regressor = bundle.regressor
        self.grace_g = list(bundle.grace_greenland or [])
        self.grace_a = list(bundle.grace_antarctica or [])
        self.obs = obs
        self.geo: dict[tuple[int, int], object] = {}
        self._ready = True

    def attach_georef(self, items) -> None:
        """North CleanGrids keyed by (year, month), for the corridor fallback."""
        geo = {}
        for item in items:
            if item.pole != "north" or item.grid.origin_x is None:
                continue
            geo[(item.year, item.month)] = item.grid
        self.geo = geo

    def _forecast(self, year: int, month: int) -> bool:
        if self.obs is None:
            return True
        return (year, month) > self.obs

    def _fit(self, pole: str, month: int):
        xs: list[int] = []
        ys: list[float] = []
        for year in range(1979, 2101):
            rec = self.rows.get((pole, year, month))
            if not rec or rec[2] or rec[0] is None:
                continue
            if self._forecast(year, month):
                continue
            xs.append(year)
            ys.append(float(rec[0]))
        if len(xs) < 2:
            if ys:
                return None, None, 0.0, xs[-1] if xs else None, ys[-1]
            return None, None, 0.0, None, None
        slope, intercept = np.polyfit(np.asarray(xs, dtype=float), np.asarray(ys, dtype=float), 1)
        pred = intercept + slope * np.asarray(xs, dtype=float)
        sigma = float(np.std(ys - pred))
        return float(intercept), float(slope), sigma, xs[-1], ys[-1]

    def _trend_extent(self, pole: str, month: int, year: int, scenario: str, blend: float | None):
        intercept, slope, sigma, last_year, last_ext = self._fit(pole, month)
        if last_ext is None:
            return None, None, None
        if intercept is None or slope is None or last_year is None:
            median = float(last_ext)
        else:
            raw = float(intercept + slope * year)
            delta = raw - float(last_ext)
            factor = 1.0
            if self.warming:
                try:
                    w = blended_warming(self.warming, year, scenario, blend)
                    wref = blended_warming(self.warming, year, "ssp245", None)
                    factor = 1.0 if wref <= 0 else max(w / wref, 0.0)
                except Exception:
                    factor = 1.0
            median = max(0.0, float(last_ext) + delta * factor)
        median = min(max(0.0, median), 25_000_000.0)
        low = min(max(0.0, median - Z90 * sigma), median)
        high = min(max(median, median + Z90 * sigma), 25_000_000.0)
        return median, low, high

    def _masked(self, width: int, height: int, *, forecast: bool, gap: bool, source: str) -> ConcentrationGrid:
        return ConcentrationGrid(
            width=width,
            height=height,
            values_u8=bytes([255]) * (width * height),
            extent_km2=None,
            extent_scale=1.0,
            is_forecast=forecast,
            gap_masked=gap,
            pole_hole_cells=0,
            source=source,
        )

    def _shape(self, pole: str) -> tuple[int, int]:
        for (p, _m), grid in self.templates.items():
            if p == pole:
                return grid.width, grid.height
        return (304, 448) if pole == "north" else (316, 332)

    def concentration(self, req: GridRequest) -> ConcentrationGrid:
        key = (req.pole, req.year, req.month)
        rec = self.rows.get(key)
        gap = bool(rec[2]) if rec else (req.year, req.month) in {(1987, 12), (1988, 1)}
        width, height = self._shape(req.pole)
        if gap:
            grid = self._masked(width, height, forecast=False, gap=True, source="nsidc-g02135")
            return grid
        if not self._forecast(req.year, req.month):
            found = self.by_ym.get(key)
            if found is not None:
                return ConcentrationGrid(
                    width=found.width,
                    height=found.height,
                    values_u8=found.values_u8,
                    extent_km2=found.extent_km2,
                    extent_scale=1.0,
                    is_forecast=False,
                    gap_masked=False,
                    pole_hole_cells=found.pole_hole_cells,
                    source="nsidc-g02135",
                )
            extent = rec[0] if rec else None
            masked = self._masked(width, height, forecast=False, gap=False, source="nsidc-g02135")
            return ConcentrationGrid(
                width=masked.width,
                height=masked.height,
                values_u8=masked.values_u8,
                extent_km2=extent,
                extent_scale=1.0,
                is_forecast=False,
                gap_masked=False,
                pole_hole_cells=0,
                source="nsidc-g02135",
            )
        median, low, high = self._trend_extent(req.pole, req.month, req.year, req.scenario, req.blend)
        template = self.templates.get((req.pole, req.month))
        if template is None or median is None:
            return self._masked(width, height, forecast=True, gap=False, source="model-not-ready")
        target = {"low": low, "high": high, "median": median}[req.band]
        adjusted, extent, scale = _adjust(template.values_u8, template.width, template.height, float(target or 0))
        return ConcentrationGrid(
            width=template.width,
            height=template.height,
            values_u8=adjusted,
            extent_km2=extent,
            extent_scale=scale,
            is_forecast=True,
            gap_masked=False,
            pole_hole_cells=template.pole_hole_cells,
            source="model-forecast",
        )

    def extent_series(self, pole: str, month: int, scenario: str, blend: float | None) -> list[ExtentPoint]:
        points: list[ExtentPoint] = []
        for year in range(1979, 2101):
            rec = self.rows.get((pole, year, month))
            gap = bool(rec[2]) if rec else (year, month) in {(1987, 12), (1988, 1)}
            forecast = self._forecast(year, month)
            if gap:
                points.append(ExtentPoint(year, None, None, None, False, True, "nsidc-g02135", "nsidc_monthly_csv"))
                continue
            if not forecast:
                extent = rec[0] if rec else None
                points.append(ExtentPoint(year, extent, None, None, False, False, "nsidc-g02135", "nsidc_monthly_csv"))
                continue
            median, low, high = self._trend_extent(pole, month, year, scenario, blend)
            if median is not None:
                median = max(0.0, float(median))
                low = max(0.0, float(low if low is not None else median))
                high = max(low, float(high if high is not None else median))
            points.append(
                ExtentPoint(year, median, low, high, True, False, "model-forecast", "model_cell_sum_scaled")
            )
        return points

    def impact(self, year: int, scenario: str, blend: float | None) -> ImpactResult:
        ice_free = None
        for point in self.extent_series("north", 9, scenario, blend):
            if point.extent_km2 is not None and point.extent_km2 < 1_000_000:
                ice_free = point.year
                break
        g_mm, a_mm, source = _sea_level(year, scenario, blend, self.warming, self.grace_g, self.grace_a)
        total = g_mm + a_mm
        forecast = self.obs is None or year > self.obs[0]
        nsr, nwp, winter, places, wmethod = self._corridors(year, scenario, blend, forecast)
        return ImpactResult(
            is_forecast=forecast,
            northern_sea_route=nsr,
            northwest_passage=nwp,
            ice_free_september_year=ice_free,
            winter_ice_loss_index=winter,
            winter_method=wmethod,
            community_february=places,
            greenland_mm=g_mm,
            antarctica_mm=a_mm,
            total_mm=total,
            low_mm=total,
            high_mm=total,
            sea_level_source=source,
        )

    def _grid_for_month(self, year: int, month: int, scenario: str, blend: float | None, forecast: bool):
        if not forecast:
            grid = self.geo.get((year, month))
            if grid is None:
                return None
            return grid, grid.array()
        candidates = [item for (y, m), item in self.geo.items() if m == month]
        if not candidates:
            return None
        grid = candidates[-1] if len(candidates) == 1 else max(
            ((y, self.geo[(y, month)]) for y in {yy for (yy, mm) in self.geo if mm == month}),
            key=lambda pair: pair[0],
        )[1]
        median, _low, _high = self._trend_extent("north", month, year, scenario, blend)
        target = 0.0 if median is None else max(0.0, float(median))
        adjusted, _extent, _scale = _adjust(grid.values, grid.width, grid.height, target)
        arr = np.frombuffer(adjusted, dtype=np.uint8).reshape(grid.height, grid.width)
        return grid, arr

    def _corridors(self, year, scenario, blend, forecast):
        from app.corridors import (
            COMMUNITIES,
            METHOD,
            NSR,
            NWP,
            WINTER_METHOD,
            corridor_mask,
            open_fraction,
            sample_ocean,
            season_from_fractions,
        )

        if not self.geo:
            empty = _empty_season(METHOD + " No north concentration grid is loaded.")
            return empty, empty, None, None, WINTER_METHOD + " No February grid is loaded."
        nsr_frac, nwp_frac = {}, {}
        for month in range(1, 13):
            got = self._grid_for_month(year, month, scenario, blend, forecast)
            if got is None:
                continue
            grid, values = got
            ocean = values != 255
            nsr_mask = corridor_mask(grid, NSR) & ocean
            nwp_mask = corridor_mask(grid, NWP) & ocean
            nsr_f = open_fraction(values, nsr_mask)
            nwp_f = open_fraction(values, nwp_mask)
            if nsr_f is not None:
                nsr_frac[month] = nsr_f
            if nwp_f is not None:
                nwp_frac[month] = nwp_f
        nsr_days, nsr_open, nsr_count = season_from_fractions(year, nsr_frac)
        nwp_days, nwp_open, nwp_count = season_from_fractions(year, nwp_frac)
        note = "" if len(nsr_frac) >= 10 else f" Partial: {len(nsr_frac)} of 12 months."
        nsr = ShippingSeason(max(0, nsr_days), nsr_open, nsr_count, METHOD + note)
        nwp = ShippingSeason(max(0, nwp_days), nwp_open, nwp_count, METHOD + note)
        feb = self._grid_for_month(year, 2, scenario, blend, forecast)
        places = {}
        if feb is None:
            return nsr, nwp, None, None, WINTER_METHOD + " No February grid is loaded."
        grid, values = feb
        lost = 0
        usable = 0
        for name, (lat, lon) in COMMUNITIES.items():
            conc = sample_ocean(grid, values, lat, lon)
            if conc is None:
                places[name] = None
                continue
            conc_i = int(max(0, min(100, round(conc))))
            below = conc_i < 15
            usable += 1
            lost += int(below)
            places[name] = {"february_percent": conc_i, "below_15": below}
        winter = None if usable == 0 else lost / usable
        return nsr, nwp, winter, places, WINTER_METHOD

    def backtest(self) -> BacktestResult:
        north = _score_pole(self.rows, "north", self.obs)
        south = _score_pole(self.rows, "south", self.obs)
        trained = north.n_months > 0 or south.n_months > 0
        end = self.obs[0] if self.obs else 2010
        if self.obs and (self.obs[0], 9) > self.obs and end > 1979:
            # September of the observed-through year is not in yet.
            if self.obs[1] < 9:
                end = self.obs[0] - 1
        return BacktestResult(trained, 2010, 2011, max(end, 2011) if trained else end, north, south)


def _adjust(values: bytes, width: int, height: int, target_km2: float) -> tuple[bytes, float, float]:
    arr = np.frombuffer(values, dtype=np.uint8).reshape(height, width).copy()
    target_n = max(0, int(np.floor(target_km2 / CELL_KM2 + 0.5)))
    ice_idx = np.flatnonzero((arr >= 15) & (arr <= 100))
    order = ice_idx[np.argsort(arr.flat[ice_idx], kind="stable")] if ice_idx.size else ice_idx
    n_ice = int(order.size)
    if target_n < n_ice:
        arr.flat[order[: n_ice - target_n]] = 0
    elif target_n > n_ice:
        marg = np.flatnonzero((arr >= 1) & (arr <= 14))
        if marg.size:
            mord = marg[np.argsort(arr.flat[marg], kind="stable")[::-1]]
            arr.flat[mord[: target_n - n_ice]] = 15
    bad = (arr > 100) & (arr < 255)
    arr[bad] = 255
    n = int(((arr >= 15) & (arr <= 100)).sum())
    extent = max(0.0, float(n * CELL_KM2))
    return np.ascontiguousarray(arr).tobytes(), extent, 1.0


def _sheet_mm(rate: float, year: int, warming: dict, scenario: str, blend: float | None) -> float:
    if year <= 2002:
        return 0.0
    if year <= 2025 or not warming:
        mass = rate * (year - 2002)
    else:
        try:
            w = blended_warming(warming, year, scenario, blend)
            w0 = blended_warming(warming, 2025, scenario, blend)
            scale = 1.0 if w0 <= 0 else max(w / w0, 0.0)
        except Exception:
            scale = 1.0
        mass = rate * (2025 - 2002) + rate * scale * (year - 2025)
    return float(-mass / GT_PER_MM)


def _sea_level(year, scenario, blend, warming, grace_g, grace_a):
    if grace_g or grace_a:
        # Real samples are not invented here. Without a fitted quadratic (that is
        # the model package), fall through to the published rates if a sheet is empty
        # and use the last anomaly for a sheet that has points.
        g_mm = _grace_mm(grace_g, RATE_GREENLAND, year, warming, scenario, blend)
        a_mm = _grace_mm(grace_a, RATE_ANTARCTICA, year, warming, scenario, blend)
        source = "grace-gracefo" if grace_g and grace_a else "nasa-science-summary-fallback"
        return g_mm, a_mm, source
    return (
        _sheet_mm(RATE_GREENLAND, year, warming, scenario, blend),
        _sheet_mm(RATE_ANTARCTICA, year, warming, scenario, blend),
        "nasa-science-summary-fallback",
    )


def _grace_mm(points, rate, year, warming, scenario, blend) -> float:
    if not points:
        return _sheet_mm(rate, year, warming, scenario, blend)
    ordered = sorted(points, key=lambda p: p.decimal_year)
    first = ordered[0].mass_gt
    last = ordered[-1]
    if year <= last.decimal_year:
        nearest = min(ordered, key=lambda p: abs(p.decimal_year - year))
        mass = nearest.mass_gt
    else:
        # Hold the last anomaly, then apply the warming-scaled rate after the record.
        mass = last.mass_gt + rate * max(year - last.decimal_year, 0)
    return float(-(mass - first) / GT_PER_MM)


def _score_pole(rows, pole: str, obs) -> PoleScore:
    train = []
    for year in range(1979, 2011):
        rec = rows.get((pole, year, 9))
        if rec and rec[0] is not None and not rec[2]:
            train.append((year, float(rec[0])))
    eval_end = 2010
    if obs:
        eval_end = obs[0] if obs[1] >= 9 else obs[0] - 1
    observed = []
    for year in range(2011, eval_end + 1):
        rec = rows.get((pole, year, 9))
        if rec and rec[0] is not None and not rec[2]:
            observed.append((year, float(rec[0])))
    clim_vals = [v for y, v in train if 1981 <= y <= 2010]
    if len(train) < 8 or not observed or len(clim_vals) < 20:
        return PoleScore(0.0, 0.0, 0, 0.0)
    xs = np.array([y for y, _ in train], dtype=float)
    ys = np.array([v for _, v in train], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    clim = float(np.mean(clim_vals))
    err = []
    cerr = []
    for year, value in observed:
        pred = float(intercept + slope * year)
        err.append(pred - value)
        cerr.append(clim - value)
    err_a = np.asarray(err)
    cerr_a = np.asarray(cerr)
    rmse = float(np.sqrt(np.mean(err_a ** 2)))
    crmse = float(np.sqrt(np.mean(cerr_a ** 2)))
    skill = 0.0 if crmse == 0 else 1.0 - rmse / crmse
    return PoleScore(rmse, float(np.mean(err_a)), len(observed), skill)
