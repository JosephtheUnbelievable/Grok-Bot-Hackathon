"""FastAPI entrypoint. `app` is the ASGI object uvicorn loads.

Run from backend/:

    PYTHONPATH=.. uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import base64
import logging
import math
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.adapter import ModelAdapter
from app.cleaning import is_gap_month
from app.fallback_model import GridRequest
from app.ssp import SCENARIOS
from app.store import DataStore, iso_z, load_dotenv, utcnow
from app.warp import render_png, transparent_png

logger = logging.getLogger("ice")

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

NSR_DEFINITION = (
    "Proxy, not a route solver. A month is open when the mean concentration of unmasked "
    "cells in lat 66–80 N, lon 30–180 E is below 15%. Days = 30 per open month, 28 in February. "
    "opening_date is the first day of the first open month on or after June, else null when no "
    "month from June onward is open."
)
NWP_DEFINITION = "Same proxy for lat 68–75 N, lon 140 W–60 W."
ICE_FREE_DEFINITION = (
    "First year in 1979–2100 whose September northern extent_km2 is below 1000000 under this "
    "scenario's median band. null if none."
)
WINTER_DEFINITION = (
    "Share of unmasked cells at lat >= 60 N whose 1981–2010 February mean was >= 15% and whose "
    "February concentration in the selected year is < 15%. Range 0–1. null if that climatology "
    "cannot be built."
)
SEA_LEVEL_BASELINE = (
    "Mass anomaly relative to the first GRACE sample, converted as mm = -mass_gt / 362. "
    "total_mm = greenland_mm + antarctica_mm. low_mm and high_mm are totals."
)
BAND_NOTE = "low is p10 and high is p90 of bootstrap extent; null on observed years"
BACKTEST_METRIC = "rmse of scaled modeled September extent against the NSIDC monthly CSV"

_SCENARIO_ALIASES = {
    "ssp119": "ssp119",
    "ssp1-1.9": "ssp119",
    "ssp126": "ssp126",
    "ssp1-2.6": "ssp126",
    "ssp245": "ssp245",
    "ssp2-4.5": "ssp245",
    "ssp370": "ssp370",
    "ssp3-7.0": "ssp370",
    "ssp585": "ssp585",
    "ssp5-8.5": "ssp585",
}


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, param: str | None):
        self.status = status
        self.code = code
        self.message = message
        self.param = param


def create_app(store: DataStore | None = None, adapter: ModelAdapter | None = None, start_refresh: bool = True) -> FastAPI:
    application = FastAPI(title="Ice on Earth", docs_url="/docs", redoc_url=None)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "X-Ice-Pole",
            "X-Ice-Year",
            "X-Ice-Month",
            "X-Ice-Scenario",
            "X-Ice-Blend",
            "X-Ice-Band",
            "X-Ice-Is-Forecast",
            "X-Ice-Source",
            "X-Ice-Stale",
            "X-Ice-Gap-Masked",
            "X-Ice-Extent-Km2",
            "X-Ice-Extent-Method",
            "X-Ice-Regressor",
            "X-Ice-Image-Layout",
            "X-Ice-Fixture",
            "X-Ice-Observed-Through",
            "Cache-Control",
        ],
    )
    application.state.store = store or DataStore(ROOT / "cache")
    application.state.adapter = adapter or ModelAdapter()
    application.state.start_refresh = start_refresh

    @application.on_event("startup")
    def _startup() -> None:
        if not application.state.start_refresh:
            return
        threading.Thread(target=_refresh_job, args=(application,), name="ice-refresh", daemon=True).start()

    @application.exception_handler(APIError)
    async def _api_error(_request: Request, exc: APIError):
        return JSONResponse(status_code=exc.status, content=_error(exc.code, exc.message, exc.param))

    @application.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return JSONResponse(status_code=404, content=_error("not_found", "path not found", "path"))
        return JSONResponse(
            status_code=exc.status_code,
            content=_error("feed_unavailable", str(exc.detail), None),
        )

    @application.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        logger.exception("request failed")
        return JSONResponse(
            status_code=503,
            content=_error("feed_unavailable", "feed unavailable", None),
        )

    @application.get("/health")
    def health():
        store_ = application.state.store
        adapter_ = application.state.adapter
        return JSONResponse(store_.health(adapter_.model_ready))

    @application.get("/ice/{pole}/{year}/{month}")
    def ice(pole: str, year: str, month: str, request: Request):
        pole_n = _pole(pole)
        year_n = _year(year)
        month_n = _month(month)
        scenario, blend, left = _scenario_blend(request.query_params.get("scenario"), request.query_params.get("blend"))
        band = _band(request.query_params.get("band"))
        fmt = _format(request.query_params.get("format"))
        body, headers, png = _ice_response(application, pole_n, year_n, month_n, scenario, blend, left, band)
        if fmt == "png":
            return Response(content=png, media_type="image/png", headers=headers)
        return JSONResponse(body, headers=headers)

    @application.get("/extent/{pole}")
    def extent(pole: str, request: Request):
        pole_n = _pole(pole)
        scenario, blend, left = _scenario_blend(request.query_params.get("scenario"), request.query_params.get("blend"))
        month = _extent_month(pole_n, request.query_params.get("month"))
        body = _extent_response(application, pole_n, month, scenario, blend, left)
        return JSONResponse(body, headers={"Cache-Control": _cache_control(body["stale"])})

    @application.get("/impact")
    def impact(request: Request):
        if request.query_params.get("year") in (None, ""):
            raise APIError(400, "invalid_parameter", "year must be an integer from 1979 to 2100", "year")
        year = _year(request.query_params.get("year"))
        scenario, blend, left = _scenario_blend(request.query_params.get("scenario"), request.query_params.get("blend"))
        return JSONResponse(_impact_response(application, year, scenario, blend, left))

    @application.get("/backtest")
    def backtest():
        return JSONResponse(_backtest_response(application))

    return application


def _refresh_job(application: FastAPI) -> None:
    store_ = application.state.store
    adapter_ = application.state.adapter
    try:
        store_.refresh()
    except Exception:
        logger.exception("feed refresh failed")
    try:
        adapter_.update_from_store(store_)
    except Exception:
        logger.exception("model update failed")


def _error(code: str, message: str, param) -> dict:
    return {"error": {"code": code, "message": message, "param": param}}


def _pole(value: str) -> str:
    pole = (value or "").lower()
    if pole not in ("north", "south"):
        raise APIError(404, "not_found", "pole must be north or south", "pole")
    return pole


def _year(value: str | None) -> int:
    text = (value or "").strip()
    if not text.isdigit():
        raise APIError(400, "invalid_parameter", "year must be an integer from 1979 to 2100", "year")
    year = int(text)
    if year < 1979 or year > 2100:
        raise APIError(400, "invalid_parameter", "year must be an integer from 1979 to 2100", "year")
    return year


def _month(value: str | None) -> int:
    text = (value or "").strip()
    if not text.isdigit():
        raise APIError(400, "invalid_parameter", "month must be an integer from 1 to 12", "month")
    month = int(text)
    if month < 1 or month > 12:
        raise APIError(400, "invalid_parameter", "month must be an integer from 1 to 12", "month")
    return month


def _extent_month(pole: str, value: str | None) -> int:
    if value in (None, ""):
        return 9 if pole == "north" else 2
    return _month(value)


def _band(value: str | None) -> str:
    if value in (None, ""):
        return "median"
    band = value.strip().lower()
    if band not in ("median", "low", "high"):
        raise APIError(400, "invalid_parameter", "band must be median, low, or high", "band")
    return band


def _format(value: str | None) -> str:
    if value in (None, ""):
        return "png"
    fmt = value.strip().lower()
    if fmt not in ("png", "json"):
        raise APIError(400, "invalid_parameter", "format must be png or json", "format")
    return fmt


def _normalize_scenario(value: str) -> str | None:
    return _SCENARIO_ALIASES.get(value.strip().lower().replace(" ", ""))


def _scenario_blend(scenario_q: str | None, blend_q: str | None) -> tuple[str, float | None, str]:
    if blend_q not in (None, ""):
        try:
            blend = float(blend_q)
        except ValueError:
            raise APIError(400, "invalid_parameter", "blend must be a number from 0 to 4", "blend") from None
        if math.isnan(blend) or blend < 0 or blend > 4:
            raise APIError(400, "invalid_parameter", "blend must be a number from 0 to 4", "blend")
        index = int(math.floor(blend))
        frac = blend - index
        left = SCENARIOS[min(index, 4)]
        scenario = left if frac == 0 else "blend"
        return scenario, blend, left
    scenario = _normalize_scenario(scenario_q or "ssp245")
    if scenario is None:
        raise APIError(
            400,
            "invalid_parameter",
            "scenario must be one of ssp119, ssp126, ssp245, ssp370, ssp585",
            "scenario",
        )
    return scenario, None, scenario


def _obs_tuple(store: DataStore) -> tuple[int, int] | None:
    return store.observed_through


def _obs_json(store: DataStore):
    obs = store.observed_through
    if not obs:
        return None
    return {"year": obs[0], "month": obs[1]}


def _is_forecast(store: DataStore, year: int, month: int) -> bool:
    obs = store.observed_through
    if obs is None:
        return True
    return (year, month) > obs


def _cache_control(stale: bool) -> str:
    return "no-cache" if stale else "public, max-age=300"


def _obs_header(store: DataStore) -> str:
    obs = store.observed_through
    if not obs:
        return ""
    return f"{obs[0]:04d}-{obs[1]:02d}"


def _shape(store: DataStore, pole: str) -> tuple[int, int, str, float]:
    for item in store.iter_grids():
        if item.pole == pole:
            return item.grid.width, item.grid.height, item.grid.projection, item.grid.nominal_resolution_m
    if pole == "north":
        return 304, 448, "EPSG:3411", 25000.0
    return 316, 332, "EPSG:3412", 25000.0


def _ice_headers(meta: dict) -> dict[str, str]:
    extent = meta["extent_km2"]
    blend = meta["blend"]
    return {
        "X-Ice-Pole": meta["pole"],
        "X-Ice-Year": str(meta["year"]),
        "X-Ice-Month": str(meta["month"]),
        "X-Ice-Scenario": str(meta["scenario"]),
        "X-Ice-Blend": "" if blend is None else str(blend),
        "X-Ice-Band": meta["band"],
        "X-Ice-Is-Forecast": "true" if meta["is_forecast"] else "false",
        "X-Ice-Source": meta["source"],
        "X-Ice-Stale": "true" if meta["stale"] else "false",
        "X-Ice-Gap-Masked": "true" if meta["gap_masked"] else "false",
        "X-Ice-Extent-Km2": "" if extent is None else str(extent),
        "X-Ice-Extent-Method": meta["extent_method"],
        "X-Ice-Regressor": meta["regressor"],
        "X-Ice-Image-Layout": "equirectangular-1024x512",
        "X-Ice-Fixture": "false",
        "X-Ice-Observed-Through": meta["observed_through_header"],
        "Cache-Control": _cache_control(meta["stale"]),
    }


def _ice_response(application, pole, year, month, scenario, blend, left, band):
    store: DataStore = application.state.store
    adapter: ModelAdapter = application.state.adapter
    gap = is_gap_month(year, month)
    forecast = _is_forecast(store, year, month)
    width, height, projection, resolution = _shape(store, pole)
    values = bytes([255]) * (width * height)
    extent = None
    area = None
    holes = 0
    scale = 1.0
    method = "nsidc_monthly_csv"
    source = "nsidc-g02135"
    stale = False
    grid_obj = None
    if gap:
        stale = _feed_stale(store, "nsidc_monthly_extent")
        source = "nsidc-g02135-cache" if _feed_cache(store, "nsidc_monthly_extent") else "nsidc-g02135"
    elif not forecast:
        grid_obj = store.ensure_grid(pole, year, month)
        rec = store.record(pole, year, month)
        if grid_obj is None and (rec is None or rec.extent_km2 is None) and not store.has_monthly(pole):
            raise APIError(503, "feed_unavailable", "no observation, model, or cached copy for this month", None)
        if rec is not None:
            extent = rec.extent_km2
            area = rec.area_km2
        found = store.grid_for(pole, year, month)
        if found is not None:
            grid_obj, from_cache = found
            width, height = grid_obj.width, grid_obj.height
            values = grid_obj.values
            projection = grid_obj.projection
            resolution = grid_obj.nominal_resolution_m
            holes = grid_obj.pole_hole_cells
            source = "nsidc-g02135-cache" if from_cache else "nsidc-g02135"
            stale = from_cache or _feed_stale(store, "nsidc_monthly_concentration") or _feed_stale(store, "nsidc_monthly_extent")
        else:
            source = "nsidc-g02135-cache" if _feed_cache(store, "nsidc_monthly_extent") else "nsidc-g02135"
            stale = True
    else:
        result = adapter.concentration(GridRequest(pole, year, month, left, blend, band))
        width, height = result.width, result.height
        values = result.values_u8
        extent = result.extent_km2
        scale = result.extent_scale
        holes = result.pole_hole_cells
        source = result.source
        method = "model_cell_sum_scaled" if source == "model-forecast" else "nsidc_monthly_csv"
        stale = source in {"model-not-ready", "model-forecast-cache"} or not adapter.model_ready or source.endswith("-cache")
        area = None
        for item in store.iter_grids():
            if item.pole == pole and item.grid.width == width:
                projection = item.grid.projection
                resolution = item.grid.nominal_resolution_m
                break
    meta = {
        "pole": pole,
        "year": year,
        "month": month,
        "scenario": scenario,
        "blend": blend,
        "band": band,
        "is_forecast": forecast and not gap,
        "source": source,
        "stale": stale,
        "gap_masked": gap,
        "extent_km2": extent,
        "extent_method": "nsidc_monthly_csv" if not (forecast and not gap) else method,
        "regressor": store.regressor,
        "observed_through_header": _obs_header(store),
    }
    # Observed months always report the CSV method, including the gap.
    if not meta["is_forecast"]:
        meta["extent_method"] = "nsidc_monthly_csv"
        scale = 1.0
    if gap:
        png = transparent_png()
    elif not forecast and grid_obj is not None:
        try:
            png = render_png(grid_obj)
        except Exception:
            logger.exception("warp failed")
            png = transparent_png()
            stale = True
    else:
        png = _render_values_png(store, pole, year, month, values, width, height, gap)
        if forecast and png == transparent_png() and source == "model-forecast":
            stale = True
    meta["stale"] = stale
    body = {
        "pole": pole,
        "year": year,
        "month": month,
        "scenario": scenario,
        "blend": blend,
        "band": band,
        "fixture": False,
        "generated_at": iso_z(utcnow()),
        "is_forecast": meta["is_forecast"],
        "source": source,
        "stale": stale,
        "gap_masked": gap,
        "regressor": store.regressor,
        "observed_through": _obs_json(store),
        "extent_km2": extent,
        "area_km2": area,
        "extent_method": meta["extent_method"],
        "extent_scale": scale,
        "pole_hole_cells": holes,
        "units": {"concentration": "percent", "extent": "km2", "area": "km2"},
        "grid": {
            "projection": projection,
            "nominal_resolution_m": resolution,
            "width": width,
            "height": height,
            "origin": "upper-left",
            "encoding": "row-major-uint8",
            "masked_value": 255,
        },
        "values_b64": base64.b64encode(values).decode("ascii"),
    }
    headers = _ice_headers(meta)
    return body, headers, png


def _render_values_png(store, pole, year, month, values, width, height, gap) -> bytes:
    if gap:
        return transparent_png()
    found = store.grid_for(pole, year, month)
    if found is not None and found[0].values == values:
        try:
            return render_png(found[0])
        except Exception:
            logger.exception("warp failed")
            return transparent_png()
    # Forecast grids share the template georeferencing.
    template = None
    for item in store.iter_grids():
        if item.pole == pole and item.month == month and item.grid.width == width and item.grid.height == height:
            template = item.grid
            break
    if template is None:
        return transparent_png()
    try:
        from app.cleaning import CleanGrid

        painted = CleanGrid(
            width=width,
            height=height,
            values=values,
            projection=template.projection,
            epsg=template.epsg,
            nominal_resolution_m=template.nominal_resolution_m,
            pole_hole_cells=0,
            origin_x=template.origin_x,
            origin_y=template.origin_y,
            pixel_size_x=template.pixel_size_x,
            pixel_size_y=template.pixel_size_y,
        )
        return render_png(painted)
    except Exception:
        logger.exception("forecast warp failed")
        return transparent_png()


def _png_or_transparent(*_args) -> bytes:
    return transparent_png()


def _extent_response(application, pole, month, scenario, blend, left):
    store: DataStore = application.state.store
    adapter: ModelAdapter = application.state.adapter
    if not store.has_monthly(pole):
        raise APIError(503, "feed_unavailable", "no sea-ice extent available", None)
    try:
        adapter.update_from_store(store)
    except Exception:
        logger.exception("extent model update failed")
    points = {point.year: point for point in adapter.extent_series(pole, month, left, blend)}
    obs_source = "nsidc-g02135-cache" if _feed_cache(store, "nsidc_monthly_extent") else "nsidc-g02135"
    series = []
    clim_values = []
    any_forecast = False
    for year in range(1979, 2101):
        gap = is_gap_month(year, month)
        forecast = _is_forecast(store, year, month)
        rec = store.record(pole, year, month)
        if gap or not forecast:
            extent = None if gap or rec is None else rec.extent_km2
            series.append(
                {
                    "year": year,
                    "extent_km2": extent,
                    "low_km2": None,
                    "high_km2": None,
                    "is_forecast": False,
                    "gap_masked": gap,
                    "source": obs_source,
                    "extent_method": "nsidc_monthly_csv",
                }
            )
            if extent is not None and 1981 <= year <= 2010:
                clim_values.append(extent)
            continue
        any_forecast = True
        point = points.get(year)
        series.append(
            {
                "year": year,
                "extent_km2": None if point is None else point.extent_km2,
                "low_km2": None if point is None else point.low_km2,
                "high_km2": None if point is None else point.high_km2,
                "is_forecast": True,
                "gap_masked": False,
                "source": "model-forecast" if point is not None and point.extent_km2 is not None else "model-not-ready",
                "extent_method": "model_cell_sum_scaled",
            }
        )
    clim = None
    if len(clim_values) >= 20:
        clim = int(round(sum(clim_values) / len(clim_values)))
    stale = _feed_stale(store, "nsidc_monthly_extent") or (any_forecast and (not adapter.model_ready or _feed_stale(store, "ssp_pathways")))
    return {
        "pole": pole,
        "month": month,
        "scenario": scenario,
        "blend": blend,
        "band_note": BAND_NOTE,
        "fixture": False,
        "generated_at": iso_z(utcnow()),
        "stale": stale,
        "regressor": store.regressor,
        "observed_through": _obs_json(store),
        "units": {"extent": "km2"},
        "climatology_1981_2010_km2": clim,
        "series": series,
    }


def _impact_response(application, year, scenario, blend, left):
    store: DataStore = application.state.store
    adapter: ModelAdapter = application.state.adapter
    try:
        adapter.update_from_store(store)
        result = adapter.impact(year, left, blend)
    except Exception:
        logger.exception("impact failed")
        raise APIError(503, "feed_unavailable", "impact could not be built", None) from None
    obs = store.observed_through
    forecast = obs is None or year > obs[0]
    stale = _feed_stale(store, "grace_icesheet") or _feed_stale(store, "ssp_pathways") or not adapter.model_ready
    return {
        "year": year,
        "scenario": scenario,
        "blend": blend,
        "fixture": False,
        "generated_at": iso_z(utcnow()),
        "stale": stale,
        "is_forecast": forecast,
        "regressor": store.regressor,
        "observed_through": _obs_json(store),
        "shipping": {
            "northern_sea_route": {
                "open_season_days": int(result.northern_sea_route.open_season_days),
                "opening_date": result.northern_sea_route.opening_date,
                "open_month_count": int(result.northern_sea_route.open_month_count),
                "definition": NSR_DEFINITION,
            },
            "northwest_passage": {
                "open_season_days": int(result.northwest_passage.open_season_days),
                "opening_date": result.northwest_passage.opening_date,
                "open_month_count": int(result.northwest_passage.open_month_count),
                "definition": NWP_DEFINITION,
            },
        },
        "communities": {
            "ice_free_september_year": result.ice_free_september_year,
            "winter_ice_loss_index": result.winter_ice_loss_index,
            "definitions": {
                "ice_free_september_year": ICE_FREE_DEFINITION,
                "winter_ice_loss_index": WINTER_DEFINITION,
            },
        },
        "sea_level": {
            "greenland_mm": _round_mm(result.greenland_mm),
            "antarctica_mm": _round_mm(result.antarctica_mm),
            "total_mm": _round_mm(result.total_mm),
            "low_mm": _round_mm(result.low_mm),
            "high_mm": _round_mm(result.high_mm),
            "conversion_gt_per_mm": 362,
            "baseline": SEA_LEVEL_BASELINE,
            "source": result.sea_level_source,
        },
    }


def _backtest_response(application):
    store: DataStore = application.state.store
    adapter: ModelAdapter = application.state.adapter
    try:
        adapter.update_from_store(store)
        result = adapter.backtest()
    except Exception:
        logger.exception("backtest failed")
        result = None
    if result is None or not result.trained and not store.has_monthly():
        trained = False
        north = south = {"rmse_km2": 0, "bias_km2": 0, "n_months": 0, "skill_vs_climatology": 0}
        end = store.observed_through[0] if store.observed_through else 2010
    else:
        trained = bool(result.trained)
        north = _score_json(result.north)
        south = _score_json(result.south)
        end = result.eval_end_year
    return {
        "fixture": False,
        "generated_at": iso_z(utcnow()),
        "stale": _feed_stale(store, "nsidc_monthly_extent"),
        "trained": trained,
        "train_end_year": 2010,
        "eval_start_year": 2011,
        "eval_end_year": end,
        "regressor": store.regressor,
        "metric": BACKTEST_METRIC,
        "by_pole": {"north": north, "south": south},
    }


def _score_json(score) -> dict:
    return {
        "rmse_km2": score.rmse_km2,
        "bias_km2": score.bias_km2,
        "n_months": score.n_months,
        "skill_vs_climatology": score.skill_vs_climatology,
    }


def _round_mm(value) -> float:
    return round(float(value), 2)


def _feed_stale(store: DataStore, name: str) -> bool:
    feed = store.feeds.get(name) or {}
    return feed.get("status") != "ok"


def _feed_cache(store: DataStore, name: str) -> bool:
    feed = store.feeds.get(name) or {}
    return bool(feed.get("using_cache"))


app = create_app()
