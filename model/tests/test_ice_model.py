"""Synthetic tests for the sea-ice forecast model. No network and no fixtures."""

from __future__ import annotations

import math

import numpy as np
import pytest

from model.ice_model import (
    NORTH_HEIGHT,
    NORTH_WIDTH,
    GridRequest,
    IceModel,
    MassPoint,
    MonthlyGrid,
    ShippingSeason,
    TrainingBundle,
    _north_latlon,
)

SCENARIOS = ("ssp119", "ssp126", "ssp245", "ssp370", "ssp585")


def _pathways(values: dict[int, float]) -> dict[str, dict[int, float]]:
    return {scenario: dict(values) for scenario in SCENARIOS}


def _grid(
    pole: str,
    year: int,
    month: int,
    values: bytes | list[int],
    *,
    extent: float | None = 1_000_000.0,
    gap: bool = False,
    width: int | None = None,
    height: int | None = None,
    hole: int = 0,
) -> MonthlyGrid:
    raw = bytes(values) if isinstance(values, (bytes, bytearray)) else bytes(int(v) for v in values)
    if width is None:
        width = len(raw)
    if height is None:
        height = 1
    return MonthlyGrid(
        pole=pole,  # type: ignore[arg-type]
        year=year,
        month=month,
        width=width,
        height=height,
        values_u8=raw,
        extent_km2=None if gap else extent,
        area_km2=None,
        gap_masked=gap,
        pole_hole_cells=hole,
    )


def _bundle(
    grids: list[MonthlyGrid],
    *,
    co2: dict[int, float] | None = None,
    ssp_co2: dict[str, dict[int, float]] | None = None,
    warming: dict[str, dict[int, float]] | None = None,
    greenland: list[MassPoint] | None = None,
    antarctica: list[MassPoint] | None = None,
    area: dict[str, list[float]] | None = None,
    regressor: str = "cumulative_co2_gt",
) -> TrainingBundle:
    return TrainingBundle(
        grids=list(grids),
        cumulative_co2_gt_by_year={} if co2 is None else dict(co2),
        ssp_cumulative_co2_gt={scenario: {} for scenario in SCENARIOS} if ssp_co2 is None else ssp_co2,
        ssp_warming_c={scenario: {} for scenario in SCENARIOS} if warming is None else warming,
        grace_greenland=[] if greenland is None else list(greenland),
        grace_antarctica=[] if antarctica is None else list(antarctica),
        cell_area_km2=area,
        regressor=regressor,  # type: ignore[arg-type]
    )


def _fit(bundle: TrainingBundle) -> IceModel:
    model = IceModel()
    model.update(bundle)
    return model


def _req(pole: str, year: int, month: int, band: str = "median", scenario: str = "ssp245", blend: float | None = None) -> GridRequest:
    return GridRequest(
        pole,  # type: ignore[arg-type]
        year,
        month,
        scenario,  # type: ignore[arg-type]
        blend,
        band,  # type: ignore[arg-type]
    )


def test_negative_slope_follows_the_regressor_not_the_year() -> None:
    """Concentration falls from 90% to 10% as the regressor rises.

    Calendar time runs the other way, so a fit against the year would get the
    sign wrong: a later forecast year is given the higher regressor and must
    come out with less ice.
    """
    years = list(range(2000, 2012))
    regressor = [1000.0 - 50.0 * i for i in range(12)]
    concentration = [10 + round(80 * i / 11) for i in range(12)]
    assert concentration[0] == 10 and concentration[-1] == 90
    # As the regressor rises (last year -> first year), concentration goes 90 -> 10.
    order = np.argsort(regressor)
    assert concentration[int(order[0])] == 90
    assert concentration[int(order[-1])] == 10

    grids = [
        _grid("north", year, 9, [value], extent=4_000_000.0)
        for year, value in zip(years, concentration, strict=True)
    ]
    hist = {year: value for year, value in zip(years, regressor, strict=True)}
    future = {2030: 200.0, 2040: 1800.0, 2100: 1800.0}
    model = _fit(_bundle(grids, co2=hist, ssp_co2=_pathways(future)))

    for band in ("low", "high"):
        observed = model.concentration(_req("north", 2000, 9, band, scenario="ssp585", blend=3.2))
        assert observed.values_u8 == bytes([concentration[0]])
        assert observed.is_forecast is False
        assert observed.source == "nsidc-g02135"

    low_x = model.concentration(_req("north", 2030, 9, "median"))
    high_x = model.concentration(_req("north", 2040, 9, "median"))
    assert low_x.is_forecast is True and high_x.is_forecast is True
    assert low_x.source == high_x.source == "model-forecast"
    assert high_x.values_u8[0] < low_x.values_u8[0]

    again = _fit(_bundle(grids, co2=hist, ssp_co2=_pathways(future)))
    for band in ("low", "high"):
        first = model.concentration(_req("north", 2040, 9, band))
        second = again.concentration(_req("north", 2040, 9, band))
        assert first.values_u8 == second.values_u8

    impact = model.impact(2005, "ssp245", None)
    assert impact.winter_ice_loss_index is None
    assert impact.northern_sea_route == ShippingSeason(0, None, 0)
    assert impact.northwest_passage == ShippingSeason(0, None, 0)
    assert impact.is_forecast is False


def test_observed_month_is_returned_byte_for_byte_for_every_band() -> None:
    raw = bytes([0, 15, 100, 255, 42, 7])
    grid = _grid("north", 2005, 9, raw, width=3, height=2, extent=12345.0, hole=4)
    model = _fit(_bundle([grid], co2={2005: 1500.0}))
    for band in ("low", "median", "high"):
        for scenario, blend in (("ssp119", None), ("ssp585", 1.5)):
            out = model.concentration(_req("north", 2005, 9, band, scenario=scenario, blend=blend))
            assert out.values_u8 == raw
            assert out.is_forecast is False
            assert out.source == "nsidc-g02135"
            assert out.extent_km2 == 12345.0
            assert out.extent_scale == 1.0
            assert out.gap_masked is False
            assert out.pole_hole_cells == 4
            assert out.width == 3 and out.height == 2


def test_gap_grid_stays_masked_and_extent_is_none() -> None:
    gap = _grid("south", 1988, 1, [1, 2, 3, 4], width=2, height=2, gap=True)
    real = _grid("south", 2010, 1, [40], extent=500.0)
    model = _fit(_bundle([gap, real], co2={2010: 1200.0}))
    out = model.concentration(_req("south", 1988, 1, "high", scenario="ssp126", blend=0.0))
    assert out.gap_masked is True
    assert out.extent_km2 is None
    assert out.values_u8 == bytes([255, 255, 255, 255])
    assert out.is_forecast is False
    assert out.source == "nsidc-g02135"
    assert set(out.values_u8) == {255}


def test_backtest_train_end_is_2010_and_main_fit_is_unchanged() -> None:
    grids = []
    co2: dict[int, float] = {}
    for year in range(2000, 2011):
        grids.append(_grid("north", year, 9, [80], extent=6_000_000.0))
        co2[year] = 1000.0 + 10.0 * (year - 2000)
    for year in range(2011, 2017):
        grids.append(_grid("north", year, 9, [10], extent=3_000_000.0))
        co2[year] = 1000.0 + 10.0 * (year - 2000)
    co2[2040] = 2000.0
    bundle = _bundle(grids, co2=co2, ssp_co2=_pathways({2040: 2000.0, 2100: 2500.0}))
    model = _fit(bundle)

    def grab() -> tuple[bytes, bytes, bytes]:
        return tuple(model.concentration(_req("north", 2040, 9, band)).values_u8 for band in ("low", "median", "high"))  # type: ignore[return-value]

    before = grab()
    assert before[1][0] < 40
    result = model.backtest()
    after = grab()
    assert result.train_end_year == 2010
    assert result.eval_start_year == 2011
    assert result.eval_end_year == 2016
    assert result.trained is True
    assert before == after
    assert result.north.n_months == 6
    assert result.south.n_months == 0
    assert result.south.rmse_km2 == 0.0
    assert math.isfinite(result.north.rmse_km2)
    assert math.isfinite(result.north.bias_km2)
    assert math.isfinite(result.north.skill_vs_climatology)
    # A second model trained on the same bundle still matches, seed included.
    other = _fit(bundle)
    assert other.concentration(_req("north", 2040, 9, "median")).values_u8 == before[1]


def test_mass_loss_converts_to_positive_sea_level() -> None:
    model = _fit(
        _bundle(
            [],
            warming=_pathways({year: 1.5 for year in range(1979, 2101)}),
            greenland=[MassPoint(2002.0, 0.0, 12.0), MassPoint(2012.0, -1000.0, 12.0)],
            antarctica=[MassPoint(2002.0, 0.0, None), MassPoint(2012.0, 0.0, None)],
        )
    )
    at_loss = model.impact(2012, "ssp245", None)
    assert at_loss.sea_level_source == "grace-gracefo"
    assert at_loss.greenland_mm == pytest.approx(1000.0 / 362.0)
    assert at_loss.antarctica_mm == pytest.approx(0.0, abs=1e-6)
    assert at_loss.total_mm == pytest.approx(at_loss.greenland_mm + at_loss.antarctica_mm)
    assert at_loss.greenland_mm > 0
    assert at_loss.total_mm > 0
    assert at_loss.low_mm == pytest.approx(at_loss.total_mm, abs=1e-3)
    assert at_loss.high_mm == pytest.approx(at_loss.total_mm, abs=1e-3)
    assert model.impact(2002, "ssp245", None).greenland_mm == pytest.approx(0.0, abs=1e-6)
    assert model.impact(2030, "ssp245", None).greenland_mm > at_loss.greenland_mm
    assert at_loss.winter_ice_loss_index is None
    assert at_loss.northern_sea_route.open_season_days == 0
    assert at_loss.northern_sea_route.opening_date is None
    assert at_loss.community_places is None
    assert "304" in at_loss.places_method


def test_empty_update_does_not_raise_and_reports_not_ready() -> None:
    model = IceModel()
    model.update(_bundle([]))
    out = model.concentration(_req("north", 2020, 9))
    assert out.source == "model-not-ready"
    assert out.extent_km2 is None
    assert out.is_forecast is False
    assert set(out.values_u8) == {255}
    assert out.width * out.height == len(out.values_u8)

    model.update(_bundle([_grid("north", 2001, 6, [55])], co2={2001: 10.0}))
    assert model.concentration(_req("north", 2001, 6)).source == "nsidc-g02135"
    model.update(_bundle([]))
    assert model.concentration(_req("north", 2001, 6)).source == "model-not-ready"


def test_extent_series_covers_1979_through_2100() -> None:
    model = IceModel()
    model.update(_bundle([]))
    series = model.extent_series("south", 2, "ssp370", None)
    assert len(series) == 122
    assert [point.year for point in series] == list(range(1979, 2101))

    grids = [_grid("north", year, 9, [70], extent=2_500_000.0) for year in range(2000, 2012)]
    co2 = {year: 1000.0 + year for year in range(2000, 2012)}
    co2[2050] = 3000.0
    fitted = _fit(_bundle(grids, co2=co2, ssp_co2=_pathways({2050: 3000.0, 2100: 4000.0})))
    points = fitted.extent_series("north", 9, "ssp245", None)
    assert len(points) == 122
    assert points[0].year == 1979 and points[-1].year == 2100
    observed = next(point for point in points if point.year == 2004)
    assert observed.extent_km2 == 2_500_000.0
    assert observed.low_km2 is None and observed.high_km2 is None
    assert observed.is_forecast is False
    assert observed.extent_method == "nsidc_monthly_csv"
    assert observed.source == "nsidc-g02135"
    forecast = next(point for point in points if point.year == 2050)
    assert forecast.is_forecast is True
    assert forecast.source == "model-forecast"
    assert forecast.extent_method == "model_cell_sum_scaled"
    assert forecast.extent_km2 is not None
    assert forecast.low_km2 is not None and forecast.high_km2 is not None
    assert forecast.gap_masked is False


def test_short_record_uses_the_mean_and_masked_cells_stay_masked() -> None:
    short = [_grid("north", year, 9, [10, 40], width=2) for year in range(2000, 2004)]
    co2 = {year: float(year) for year in range(2000, 2004)}
    co2[2050] = 9_000.0
    model = _fit(_bundle(short, co2=co2, ssp_co2=_pathways({2050: 9_000.0})))
    assert model.concentration(_req("north", 2050, 9)).values_u8 == bytes([10, 40])

    long_grids = []
    long_co2 = {}
    for i, year in enumerate(range(2000, 2012)):
        long_grids.append(_grid("north", year, 9, [255, 80], width=2))
        long_co2[year] = 1000.0 + 50.0 * i
    long_co2[2040] = 5_000.0
    fitted = _fit(_bundle(long_grids, co2=long_co2, ssp_co2=_pathways({2040: 5_000.0})))
    forecast = fitted.concentration(_req("north", 2040, 9))
    assert forecast.values_u8[0] == 255
    assert forecast.values_u8[1] == 80


def test_extent_scale_uses_625_when_no_cell_area_map() -> None:
    grids = [_grid("north", year, 9, [100], extent=1_000_000.0) for year in range(2000, 2011)]
    co2 = {year: 1000.0 + year for year in range(2000, 2011)}
    model = _fit(_bundle(grids, co2=co2, ssp_co2=_pathways({2030: 2000.0})))
    observed = model.concentration(_req("north", 2005, 9, "low"))
    assert observed.values_u8 == bytes([100])
    assert observed.extent_km2 == 1_000_000.0
    assert observed.extent_scale == 1.0
    forecast = model.concentration(_req("north", 2030, 9, "median"))
    assert forecast.extent_scale == pytest.approx(1_000_000.0 / 625.0)
    assert forecast.extent_km2 == pytest.approx(1_000_000.0)
    assert forecast.values_u8 == bytes([100])


def test_cell_area_map_replaces_the_625_default() -> None:
    grids = [_grid("south", year, 2, [100], extent=1_000_000.0) for year in range(2000, 2011)]
    co2 = {year: 500.0 for year in range(2000, 2011)}
    model = _fit(
        _bundle(grids, co2=co2, ssp_co2=_pathways({2040: 800.0}), area={"south": [1000.0]})
    )
    forecast = model.concentration(_req("south", 2040, 2, "high"))
    assert forecast.extent_scale == pytest.approx(1000.0)
    assert forecast.extent_km2 == pytest.approx(1_000_000.0)


def test_blend_interpolates_the_driver_not_the_grids() -> None:
    years = list(range(2000, 2012))
    hist = {year: 1000.0 + 40.0 * i for i, year in enumerate(years)}
    grids = [_grid("north", year, 9, [90 - 5 * i]) for i, year in enumerate(years)]
    ssp = {scenario: {2060: 1500.0, 2100: 1500.0} for scenario in SCENARIOS}
    ssp["ssp126"] = {2060: 2200.0, 2100: 2200.0}
    ssp["ssp245"] = {2060: 2200.0, 2100: 2200.0}
    model = _fit(_bundle(grids, co2=hist, ssp_co2=ssp))
    at_119 = model.concentration(_req("north", 2060, 9, scenario="ssp119", blend=None)).values_u8[0]
    at_blend_0 = model.concentration(_req("north", 2060, 9, scenario="ssp585", blend=0.0)).values_u8[0]
    at_126 = model.concentration(_req("north", 2060, 9, scenario="ssp126", blend=None)).values_u8[0]
    at_blend_1 = model.concentration(_req("north", 2060, 9, scenario="ssp119", blend=1.0)).values_u8[0]
    midway = model.concentration(_req("north", 2060, 9, scenario="ssp585", blend=0.5)).values_u8[0]
    assert at_119 == at_blend_0
    assert at_126 == at_blend_1
    assert at_119 > midway > at_126


def test_warming_fallback_does_not_invent_a_co2_series() -> None:
    years = list(range(2000, 2012))
    warming = {year: 0.4 + 0.08 * i for i, year in enumerate(years)}
    warming[2030] = 1.0
    warming[2060] = 2.5
    grids = [_grid("north", year, 9, [88 - 4 * i]) for i, year in enumerate(years)]
    model = _fit(
        _bundle(
            grids,
            co2={},
            ssp_co2={scenario: {} for scenario in SCENARIOS},
            warming=_pathways(warming),
            regressor="ipcc_ar6_warming_c_fallback",
        )
    )
    earlier = model.concentration(_req("north", 2030, 9)).values_u8[0]
    later = model.concentration(_req("north", 2060, 9)).values_u8[0]
    assert later < earlier
    assert model.concentration(_req("north", 2003, 9, "low")).values_u8 == bytes([88 - 4 * 3])


def test_no_grace_uses_the_nasa_summary_rates() -> None:
    model = _fit(_bundle([]))
    impact = model.impact(2012, "ssp585", None)
    assert impact.sea_level_source == "nasa-science-summary-fallback"
    assert impact.greenland_mm == pytest.approx(264.0 * 10 / 362.0)
    assert impact.antarctica_mm == pytest.approx(135.0 * 10 / 362.0)
    assert impact.total_mm == pytest.approx(impact.greenland_mm + impact.antarctica_mm)
    assert impact.low_mm == pytest.approx(impact.total_mm)
    assert impact.high_mm == pytest.approx(impact.total_mm)
    assert model.impact(2002, "ssp245", None).total_mm == pytest.approx(0.0)


def test_fallback_rate_scales_only_after_2025() -> None:
    warming = {2025: 1.5, 2035: 3.0}
    model = _fit(_bundle([], warming=_pathways(warming)))
    impact = model.impact(2035, "ssp245", None)
    # 23 years at the NASA rate, then 10 years at twice that rate.
    assert impact.greenland_mm == pytest.approx(264.0 * 43 / 362.0)
    assert impact.antarctica_mm == pytest.approx(135.0 * 43 / 362.0)
    assert impact.sea_level_source == "nasa-science-summary-fallback"


def test_ice_free_september_is_the_first_year_under_one_million() -> None:
    grids = [_grid("north", year, 9, [80], extent=2_000_000.0) for year in range(2000, 2012)]
    grids.append(_grid("north", 2012, 9, [10], extent=800_000.0))
    model = _fit(_bundle(grids, co2={year: float(year) for year in range(2000, 2013)}))
    assert model.impact(2012, "ssp245", None).ice_free_september_year == 2012


def test_north_grid_georeference_matches_the_nsidc_25km_grid() -> None:
    lat, lon = _north_latlon()
    assert lat.shape == (NORTH_HEIGHT, NORTH_WIDTH)
    assert lon.shape == lat.shape
    # Upper-left cell centre, North Pacific side of the NSIDC grid.
    assert 30.0 < float(lat[0, 0]) < 33.0
    assert 165.0 < float(lon[0, 0]) < 172.0
    assert float(lat[233:235, 153:155].min()) > 89.5
    # Negative y (lower rows) follows the -45° central meridian toward Greenland.
    assert -55.0 < float(lon[400, 152]) < -35.0
    nsr = (lat >= 66.0) & (lat <= 80.0) & ((lon >= 30.0) | np.isclose(lon, -180.0))
    nwp = (lat >= 68.0) & (lat <= 75.0) & (lon >= -140.0) & (lon <= -60.0)
    assert np.any(nsr)
    assert np.any(nwp)
    assert not np.any(nsr & nwp)


def test_winter_index_uses_cells_at_or_above_60n() -> None:
    lat, lon = _north_latlon()
    outside = np.argwhere((lat >= 60.0) & (lon > -60.0) & (lon < 30.0))
    low = np.argwhere(lat < 50.0)
    assert outside.shape[0] >= 2 and low.shape[0] >= 1
    cell_a = (int(outside[0, 0]), int(outside[0, 1]))
    cell_b = (int(outside[1, 0]), int(outside[1, 1]))
    cell_c = (int(low[0, 0]), int(low[0, 1]))

    def paint(assignments: dict[tuple[int, int], int]) -> bytes:
        grid = np.full((NORTH_HEIGHT, NORTH_WIDTH), 255, dtype=np.uint8)
        for cell, value in assignments.items():
            grid[cell] = value
        return grid.tobytes()

    clim = {cell_a: 80, cell_b: 80, cell_c: 80}
    target = {cell_a: 0, cell_b: 100, cell_c: 0}
    grids = [
        _grid("north", year, 2, paint(clim), width=NORTH_WIDTH, height=NORTH_HEIGHT, extent=5_000_000.0)
        for year in range(1981, 2011)
    ]
    grids.append(
        _grid("north", 2015, 2, paint(target), width=NORTH_WIDTH, height=NORTH_HEIGHT, extent=4_000_000.0)
    )
    co2 = {year: 1000.0 for year in list(range(1981, 2011)) + [2015, 2030]}
    model = _fit(_bundle(grids, co2=co2, ssp_co2=_pathways({2030: 1000.0})))
    observed = model.impact(2015, "ssp245", None)
    assert observed.winter_ice_loss_index == pytest.approx(0.5)
    assert observed.northern_sea_route.open_season_days == 0
    forecast = model.impact(2030, "ssp245", None)
    assert forecast.winter_ice_loss_index == pytest.approx(0.0)


def test_shipping_season_counts_open_months_and_uses_28_day_february() -> None:
    lat, lon = _north_latlon()
    nsr = (lat >= 66.0) & (lat <= 80.0) & ((lon >= 30.0) | np.isclose(lon, -180.0))
    nwp = (lat >= 68.0) & (lat <= 75.0) & (lon >= -140.0) & (lon <= -60.0)
    assert np.any(nsr) and np.any(nwp) and not np.any(nsr & nwp)
    grid = np.full((NORTH_HEIGHT, NORTH_WIDTH), 255, dtype=np.uint8)
    grid[nwp] = 15  # mean of 15 is not open; the proxy is strict < 15
    grid[nsr] = 0
    raw = grid.tobytes()
    grids = [
        _grid(
            "north",
            2016,
            month,
            raw,
            width=NORTH_WIDTH,
            height=NORTH_HEIGHT,
            extent=5_000_000.0 if month == 9 else 6_000_000.0,
        )
        for month in range(1, 13)
    ]
    model = _fit(_bundle(grids, co2={2016: 1500.0}))
    impact = model.impact(2016, "ssp245", None)
    assert impact.is_forecast is False
    assert impact.northern_sea_route.open_month_count == 12
    # 2016 is a leap year and every NSR day is open, so the count is 366, not 12*30.
    assert impact.northern_sea_route.open_season_days == 366
    assert impact.northern_sea_route.opening_date == "2016-06-01"
    assert "daily-linear-interpolation" in impact.northern_sea_route.method
    assert impact.northwest_passage.open_season_days == 0
    assert impact.northwest_passage.opening_date is None
    assert impact.northwest_passage.open_month_count == 0
    assert impact.winter_ice_loss_index is None


def _ordered_warming() -> dict[str, dict[int, float]]:
    """Shared history through 2025, then SSP anchors. SSP1-1.9 eases down to 1.4."""
    anchors = {
        "ssp119": 1.4,
        "ssp126": 1.8,
        "ssp245": 2.7,
        "ssp370": 3.6,
        "ssp585": 4.4,
    }
    table: dict[str, dict[int, float]] = {}
    for scenario, end in anchors.items():
        series = {}
        for year in range(1979, 2101):
            if year <= 2025:
                series[year] = 1.5
            elif year >= 2090:
                series[year] = end
            else:
                span = 2090 - 2025
                series[year] = 1.5 + (year - 2025) / span * (end - 1.5)
        table[scenario] = series
    return table


def _grace_points(mass_at) -> list[MassPoint]:
    points = []
    for year in np.arange(2002.0, 2025.0, 0.5):
        seasonal = 120.0 * math.sin(2.0 * math.pi * float(year))
        points.append(MassPoint(float(year), float(mass_at(float(year)) + seasonal), 40.0))
    return points


def _assert_sea_level_shape(model: IceModel) -> dict[str, list[float]]:
    order = ("ssp119", "ssp126", "ssp245", "ssp370", "ssp585")
    milestones = (2030, 2050, 2080, 2100)
    probe = sorted(set(milestones) | set(range(2026, 2101, 5)))
    by_scenario: dict[str, list[float]] = {}
    for scenario in order:
        previous = None
        recorded: list[float] = []
        for year in probe:
            impact = model.impact(year, scenario, None)  # type: ignore[arg-type]
            assert impact.low_mm <= impact.total_mm + 1e-9
            assert impact.total_mm <= impact.high_mm + 1e-9
            assert impact.total_mm == pytest.approx(impact.greenland_mm + impact.antarctica_mm)
            assert impact.total_mm >= -1e-6
            if previous is not None:
                assert impact.total_mm + 1e-6 >= previous
            previous = impact.total_mm
            if year in milestones:
                recorded.append(impact.total_mm)
        assert recorded[0] < recorded[-1]
        by_scenario[scenario] = recorded
    for index in range(len(milestones)):
        column = [by_scenario[scenario][index] for scenario in order]
        assert column == sorted(column)
    return by_scenario


def test_accelerating_grace_loss_rises_with_time_and_warming() -> None:
    def greenland(year: float) -> float:
        dt = year - 2002.0
        return -180.0 * dt + 0.5 * (-8.0) * dt * dt

    def antarctica(year: float) -> float:
        dt = year - 2002.0
        return -90.0 * dt + 0.5 * (-3.0) * dt * dt

    model = _fit(
        _bundle(
            [],
            warming=_ordered_warming(),
            greenland=_grace_points(greenland),
            antarctica=_grace_points(antarctica),
        )
    )
    assert model._greenland is not None and model._greenland.acceleration < 0
    assert model._antarctica is not None and model._antarctica.acceleration < 0
    by_scenario = _assert_sea_level_shape(model)
    assert model.impact(2100, "ssp585", None).sea_level_source == "grace-gracefo"
    assert 40 < by_scenario["ssp126"][-1] < 250
    assert by_scenario["ssp126"][-1] < by_scenario["ssp585"][-1] < 450


def test_decelerating_grace_loss_does_not_bend_sea_level_back() -> None:
    """A positive quadratic acceleration used to reverse sea level, hotter scenarios first."""

    def sheet(scale: float):
        def mass_at(year: float) -> float:
            return scale * -5200.0 * (1.0 - math.exp(-(year - 2002.0) / 6.0))

        return mass_at

    model = _fit(
        _bundle(
            [],
            warming=_ordered_warming(),
            greenland=_grace_points(sheet(1.0)),
            antarctica=_grace_points(sheet(0.45)),
        )
    )
    assert model._greenland is not None and model._greenland.acceleration > 0
    by_scenario = _assert_sea_level_shape(model)
    assert model.impact(2050, "ssp126", None).sea_level_source == "grace-gracefo"
    assert 30 < by_scenario["ssp126"][-1] < 300
    assert by_scenario["ssp585"][1] >= by_scenario["ssp126"][1]
    assert by_scenario["ssp585"][2] > 0
    assert by_scenario["ssp585"][-1] > by_scenario["ssp126"][-1]


def test_nasa_fallback_is_ordered_monotonic_and_in_ar6_range() -> None:
    model = _fit(_bundle([], warming=_ordered_warming()))
    sample = model.impact(2050, "ssp126", None)
    assert sample.sea_level_source == "nasa-science-summary-fallback"
    assert sample.low_mm == pytest.approx(sample.total_mm)
    assert sample.high_mm == pytest.approx(sample.total_mm)
    by_scenario = _assert_sea_level_shape(model)
    assert 50 < by_scenario["ssp126"][-1] < 220
    assert 150 < by_scenario["ssp585"][-1] < 400
    assert by_scenario["ssp585"][-1] > by_scenario["ssp126"][-1]


def test_towns_and_shipping_days_follow_the_monthly_curve() -> None:
    values = {1: 90, 2: 90, 3: 80, 4: 60, 5: 40, 6: 20, 7: 0, 8: 0, 9: 5, 10: 25, 11: 60, 12: 90}
    grids = []
    for month, value in values.items():
        raw = np.full((NORTH_HEIGHT, NORTH_WIDTH), value, dtype=np.uint8).tobytes()
        grids.append(
            _grid(
                "north",
                2016,
                month,
                raw,
                width=NORTH_WIDTH,
                height=NORTH_HEIGHT,
                extent=4_000_000.0,
            )
        )
    model = _fit(_bundle(grids, co2={2016: 1500.0}))
    impact = model.impact(2016, "ssp245", None)
    days = impact.northern_sea_route.open_season_days
    assert days % 30 != 0
    assert 60 < days < 200
    assert impact.northern_sea_route.opening_date is not None
    assert impact.northern_sea_route.opening_date.startswith("2016-06-")
    assert "daily-linear-interpolation" in impact.northern_sea_route.method
    places = impact.community_places
    assert places is not None
    for name in ("Utqiagvik", "Kivalina", "Shishmaref", "Tuktoyaktuk"):
        place = places[name]
        assert place is not None
        assert place["february_percent"] == 90
        assert place["below_15"] is False
        assert place["open_water_days"] == days
    assert "100 km" in impact.places_method
