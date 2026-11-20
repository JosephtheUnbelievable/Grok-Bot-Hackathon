"""Call `model.ice_model.IceModel` when it imports. Otherwise the linear-trend fallback.

`model_ready` is true only after the real package has accepted a bundle. The
fallback still answers forecast routes so the API is up while model/ is in progress.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback

from app.fallback_model import (
    BacktestResult,
    ConcentrationGrid,
    FallbackIceModel,
    GridRequest,
    ImpactResult,
    MassPoint,
    MonthlyGrid,
    PoleScore,
    TrainingBundle,
)

logger = logging.getLogger("ice.adapter")

try:
    from model.ice_model import IceModel as RealIceModel
    from model.ice_model import MassPoint as RealMassPoint
    from model.ice_model import MonthlyGrid as RealMonthlyGrid
    from model.ice_model import TrainingBundle as RealTrainingBundle

    REAL_IMPORT = True
    IMPORT_ERROR = ""
except Exception as exc:  # model/ is built in parallel and may be absent
    RealIceModel = None
    RealMassPoint = MassPoint
    RealMonthlyGrid = MonthlyGrid
    RealTrainingBundle = TrainingBundle
    REAL_IMPORT = False
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


_PRECOMPUTE_SERIES = (
    ("north", 9),
    ("south", 2),
)
_PRECOMPUTE_SCENARIOS = ("ssp119", "ssp126", "ssp245", "ssp370", "ssp585")


def _has_cell_fit(model) -> bool:
    """True when at least one cell had the 8 unmasked years the logistic needs."""
    for month_model in getattr(model, "_models", {}).values():
        index = getattr(month_model, "fitted_idx", None)
        if index is not None and len(index) > 0:
            return True
    return False


def _store_signature(store) -> tuple:
    grids = []
    for item in store.iter_grids():
        values = item.grid.values or b""
        grids.append(
            (
                item.pole,
                item.year,
                item.month,
                item.grid.width,
                item.grid.height,
                item.extent_km2,
                bool(item.gap_masked),
                len(values),
                bytes(values[:4]),
                bytes(values[-4:]) if values else b"",
            )
        )
    grids.sort()
    rows = tuple(
        sorted(
            (row.pole, row.year, row.month, row.extent_km2, bool(row.gap_masked))
            for row in store.iter_extent_rows()
        )
    )
    warming = tuple(
        (scenario, len(series), series.get(2020), series.get(2050), series.get(2090))
        for scenario, series in sorted((store.warming or {}).items())
    )
    co2 = tuple(sorted((store.cumulative_co2 or {}).items()))
    return (
        store.regressor,
        store.observed_through,
        tuple(grids),
        rows,
        warming,
        co2,
        len(store.grace_greenland),
        len(store.grace_antarctica),
    )


class ModelAdapter:
    def __init__(self) -> None:
        self.real_import = REAL_IMPORT
        self.import_error = IMPORT_ERROR
        self._lock = threading.Lock()
        self._fallback = FallbackIceModel()
        self._real = None
        self._real_ready = False
        self.last_error = IMPORT_ERROR or None
        self._signature = None
        self._fitting = False
        self._fit_event = threading.Event()

    @property
    def model_ready(self) -> bool:
        return self._real_ready

    @property
    def model_error(self) -> str | None:
        return self.last_error

    @property
    def forecast_source(self) -> str:
        if self._real_ready:
            return "model.ice_model"
        if self.last_error:
            reason = self.last_error
        elif self.import_error:
            reason = self.import_error
        else:
            reason = "model fit has not finished"
        return f"linear-trend-fallback ({reason})"

    def update_from_store(self, store) -> None:
        signature = _store_signature(store)
        while True:
            with self._lock:
                if signature == self._signature:
                    return
                if not self._fitting:
                    self._fitting = True
                    self._fit_event.clear()
                    break
            self._fit_event.wait(timeout=2.0)
        fallback = self._fallback
        real = None
        ready = False
        try:
            fallback_bundle = _bundle(store, MonthlyGrid, MassPoint, TrainingBundle, placeholders=True)
            fallback = FallbackIceModel()
            fallback.update(fallback_bundle)
            fallback.attach_georef(store.iter_grids())
            if self.real_import and RealIceModel is not None:
                try:
                    real_bundle = _bundle(
                        store, RealMonthlyGrid, RealMassPoint, RealTrainingBundle, placeholders=False
                    )
                    logger.info(
                        "fitting IceModel on %s concentration grids (%s)",
                        len(real_bundle.grids),
                        real_bundle.regressor,
                    )
                    started = time.perf_counter()
                    real = RealIceModel()
                    real.update(real_bundle)
                    logger.info("IceModel.update finished in %.1fs", time.perf_counter() - started)
                    if _has_cell_fit(real):
                        ready = True
                        self.last_error = None
                        _precompute(real)
                    else:
                        real = None
                        ready = False
                        self.last_error = (
                            "no cell had 8 unmasked concentration years; linear-trend fallback"
                        )
                except Exception as exc:
                    logger.exception("IceModel.update failed; keeping the linear-trend fallback")
                    self.last_error = f"update: {type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                    real = None
                    ready = False
        finally:
            with self._lock:
                self._fallback = fallback
                self._real = real
                self._real_ready = ready
                self._signature = signature
                self._fitting = False
                self._fit_event.set()

    def _active(self):
        with self._lock:
            if self._real_ready and self._real is not None:
                return self._real
            return self._fallback

    def _note(self, where: str, exc: BaseException) -> None:
        logger.exception("%s failed; using linear-trend fallback", where)
        self.last_error = f"{where}: {type(exc).__name__}: {exc}\n{traceback.format_exc()}"

    def concentration(self, req: GridRequest) -> ConcentrationGrid:
        try:
            return self._active().concentration(req)
        except Exception as exc:
            self._note("concentration", exc)
            try:
                return self._fallback.concentration(req)
            except Exception as inner:
                self._note("concentration fallback", inner)
                return ConcentrationGrid(
                    8, 8, bytes([255]) * 64, None, 1.0, True, False, 0, "model-not-ready"
                )

    def extent_series(self, pole, month, scenario, blend):
        try:
            return list(self._active().extent_series(pole, month, scenario, blend))
        except Exception as exc:
            self._note("extent_series", exc)
            return self._fallback.extent_series(pole, month, scenario, blend)

    def impact(self, year, scenario, blend) -> ImpactResult:
        try:
            return self._active().impact(year, scenario, blend)
        except Exception as exc:
            self._note("impact", exc)
            return self._fallback.impact(year, scenario, blend)

    def backtest(self) -> BacktestResult:
        try:
            return self._active().backtest()
        except Exception as exc:
            self._note("backtest", exc)
            return self._fallback.backtest()


def _precompute(model) -> None:
    """Warm backtest and the default extent series so the first request is not the fit."""
    try:
        model.backtest()
    except Exception:
        logger.exception("backtest precompute failed")
    for pole, month in _PRECOMPUTE_SERIES:
        for scenario in _PRECOMPUTE_SCENARIOS:
            try:
                model.extent_series(pole, month, scenario, None)
            except Exception:
                logger.exception("extent precompute failed for %s %s %s", pole, month, scenario)


def _bundle(store, grid_cls, mass_cls, bundle_cls, placeholders: bool):
    grids = []
    seen = set()
    for item in store.iter_grids():
        grids.append(
            grid_cls(
                pole=item.pole,
                year=item.year,
                month=item.month,
                width=item.grid.width,
                height=item.grid.height,
                values_u8=item.grid.values,
                extent_km2=item.extent_km2,
                area_km2=item.area_km2,
                gap_masked=item.gap_masked,
                pole_hole_cells=item.grid.pole_hole_cells,
            )
        )
        seen.add((item.pole, item.year, item.month))
    if placeholders:
        for rec in store.iter_extent_rows():
            key = (rec.pole, rec.year, rec.month)
            if key in seen:
                continue
            grids.append(
                grid_cls(
                    pole=rec.pole,
                    year=rec.year,
                    month=rec.month,
                    width=1,
                    height=1,
                    values_u8=b"\xff",
                    extent_km2=rec.extent_km2,
                    area_km2=rec.area_km2,
                    gap_masked=rec.gap_masked,
                    pole_hole_cells=0,
                )
            )
    grace_g = [
        mass_cls(decimal_year=p[0], mass_gt=p[1], uncertainty_gt=p[2]) for p in store.grace_greenland
    ]
    grace_a = [
        mass_cls(decimal_year=p[0], mass_gt=p[1], uncertainty_gt=p[2]) for p in store.grace_antarctica
    ]
    return bundle_cls(
        grids=grids,
        cumulative_co2_gt_by_year=dict(store.cumulative_co2),
        ssp_cumulative_co2_gt={k: dict(v) for k, v in store.ssp_cumulative.items()},
        ssp_warming_c={k: dict(v) for k, v in store.warming.items()},
        grace_greenland=grace_g,
        grace_antarctica=grace_a,
        cell_area_km2=None,
        regressor=store.regressor,
    )
