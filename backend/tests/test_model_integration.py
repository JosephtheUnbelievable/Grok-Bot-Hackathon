"""The real IceModel, driven through the HTTP API, not the linear-trend fallback."""

import pytest
from fastapi.testclient import TestClient

from app.adapter import ModelAdapter
from app.cleaning import CleanGrid, ExtentRecord
from app.main import create_app
from app.ssp import warming_table
from app.store import DataStore


def _grid(values: bytes) -> CleanGrid:
    return CleanGrid(
        width=len(values),
        height=1,
        values=values,
        projection="EPSG:3411",
        epsg=3411,
        nominal_resolution_m=25000.0,
        pole_hole_cells=0,
        origin_x=-3_850_000.0,
        origin_y=5_850_000.0,
        pixel_size_x=25_000.0,
        pixel_size_y=25_000.0,
    )


def _store(tmp_path) -> DataStore:
    store = DataStore(tmp_path / "cache")
    warming = warming_table()
    # Shared history, then a wide split so ssp585 loses ice sooner than ssp126.
    anchor = warming["ssp245"][2024]
    for year in range(2025, 2101):
        step = year - 2024
        warming["ssp126"][year] = anchor + 0.004 * step
        warming["ssp585"][year] = anchor + 0.06 * step
    store.warming = warming
    store.regressor = "ipcc_ar6_warming_c_fallback"
    for index, year in enumerate(range(1998, 2021)):
        # Gentle decline. A steep one hits 0% under both scenarios and the extents match.
        values = bytes(
            [
                max(0, 92 - index),
                max(0, 78 - index),
                max(0, 58 - index),
                max(0, 40 - index // 2),
            ]
        )
        extent = 6_000_000 - index * 80_000
        store.monthly[("north", year, 9)] = ExtentRecord(year, 9, extent, None, False)
        store.grids[("north", year, 9)] = (_grid(values), False)
    store.monthly[("north", 1987, 12)] = ExtentRecord(1987, 12, None, None, True)
    store.monthly[("north", 1988, 1)] = ExtentRecord(1988, 1, None, None, True)
    store.observed_through = (2020, 9)
    return store


def _app(tmp_path):
    store = _store(tmp_path)
    adapter = ModelAdapter()
    adapter.update_from_store(store)
    return create_app(store=store, adapter=adapter, start_refresh=False)


def test_real_model_serves_forecasts_and_scenarios_diverge(tmp_path):
    client = TestClient(_app(tmp_path))
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["model_ready"] is True
    assert body["model_error"] in (None, "")
    assert body["forecast_source"] == "model.ice_model"
    assert body["regressor"] == "ipcc_ar6_warming_c_fallback"

    observed = client.get("/ice/north/2010/9?format=json")
    assert observed.status_code == 200
    observed_body = observed.json()
    assert observed_body["is_forecast"] is False
    assert observed_body["source"].startswith("nsidc-g02135")
    assert observed_body["extent_method"] == "nsidc_monthly_csv"
    assert observed_body["extent_km2"] == 6_000_000 - 12 * 80_000
    assert observed_body["gap_masked"] is False

    gap = client.get("/ice/north/1987/12?format=json")
    assert gap.status_code == 200
    assert gap.json()["gap_masked"] is True
    assert gap.json()["extent_km2"] is None
    assert set(gap.json()["values_b64"])  # present
    assert gap.json()["source"].startswith("nsidc-g02135")

    january = client.get("/extent/north?month=1")
    assert january.status_code == 200
    jan = {row["year"]: row for row in january.json()["series"]}
    assert jan[1988]["gap_masked"] is True
    assert jan[1988]["extent_km2"] is None

    extents = {}
    impacts = {}
    for scenario in ("ssp126", "ssp585"):
        extent = client.get(f"/extent/north?month=9&scenario={scenario}")
        assert extent.status_code == 200
        series = {row["year"]: row for row in extent.json()["series"]}
        assert len(series) == 122
        assert series[2010]["is_forecast"] is False
        assert series[2010]["source"].startswith("nsidc-g02135")
        assert series[2010]["extent_method"] == "nsidc_monthly_csv"
        assert series[2010]["extent_km2"] == 6_000_000 - 12 * 80_000
        for year in (2050, 2080):
            point = series[year]
            assert point["is_forecast"] is True
            assert point["gap_masked"] is False
            assert point["source"] == "model-forecast"
            assert point["extent_method"] == "model_cell_sum_scaled"
            assert point["extent_km2"] is not None and point["extent_km2"] >= 0
            assert point["low_km2"] is not None and point["high_km2"] is not None
            assert 0 <= point["low_km2"] <= point["high_km2"]
        extents[scenario] = series

        impact = client.get(f"/impact?year=2080&scenario={scenario}")
        assert impact.status_code == 200
        impact_body = impact.json()
        assert impact_body["is_forecast"] is True
        assert impact_body["regressor"] == "ipcc_ar6_warming_c_fallback"
        assert "shipping" in impact_body and "communities" in impact_body and "sea_level" in impact_body
        assert impact_body["sea_level"]["conversion_gt_per_mm"] == 362
        # Each millimetre field is rounded on its own, so the sum can be off by a hundredth.
        assert impact_body["sea_level"]["total_mm"] == pytest.approx(
            impact_body["sea_level"]["greenland_mm"] + impact_body["sea_level"]["antarctica_mm"],
            abs=0.02,
        )
        impacts[scenario] = impact_body

    assert extents["ssp585"][2050]["extent_km2"] < extents["ssp126"][2050]["extent_km2"]
    assert extents["ssp585"][2080]["extent_km2"] < extents["ssp126"][2080]["extent_km2"]
    assert impacts["ssp585"]["sea_level"]["total_mm"] > impacts["ssp126"]["sea_level"]["total_mm"]
    ice_126 = impacts["ssp126"]["communities"]["ice_free_september_year"]
    ice_585 = impacts["ssp585"]["communities"]["ice_free_september_year"]
    if ice_126 is not None and ice_585 is not None:
        assert ice_585 <= ice_126

    backtest = client.get("/backtest")
    assert backtest.status_code == 200
    score = backtest.json()
    assert score["trained"] is True
    assert score["train_end_year"] == 2010
    assert score["eval_start_year"] == 2011
    assert score["by_pole"]["north"]["n_months"] > 0
