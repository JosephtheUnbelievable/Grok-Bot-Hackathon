"""Call `model.ice_model.IceModel` when it imports. Otherwise the linear-trend fallback.

`model_ready` is true only after the real package has accepted a bundle. The
fallback still answers forecast routes so the API is up while model/ is in progress.
"""

from __future__ import annotations

import logging
import threading
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


class ModelAdapter:
    def __init__(self) -> None:
        self.real_import = REAL_IMPORT
        self.import_error = IMPORT_ERROR
        self._lock = threading.Lock()
        self._fallback = FallbackIceModel()
        self._real = None
        self._real_ready = False
        self.last_error = IMPORT_ERROR or None

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
        reason = self.last_error or "model.ice_model is not importable"
        return f"linear-trend-fallback ({reason})"

    def update_from_store(self, store) -> None:
        fallback_bundle = _bundle(store, MonthlyGrid, MassPoint, TrainingBundle, placeholders=True)
        fallback = FallbackIceModel()
        fallback.update(fallback_bundle)
        real = None
        ready = False
        if self.real_import and RealIceModel is not None:
            try:
                real_bundle = _bundle(store, RealMonthlyGrid, RealMassPoint, RealTrainingBundle, placeholders=False)
                real = RealIceModel()
                real.update(real_bundle)
                ready = True
            except Exception as exc:
                logger.exception("IceModel.update failed; keeping the linear-trend fallback")
                self.last_error = f"update: {type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                real = None
                ready = False
            else:
                self.last_error = None
        fallback.attach_georef(store.iter_grids())
        with self._lock:
            self._fallback = fallback
            self._real = real
            self._real_ready = ready

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
