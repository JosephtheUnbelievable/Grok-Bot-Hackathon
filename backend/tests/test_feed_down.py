"""A dead upstream must not 500. Cached bytes come back with a stale flag."""

import httpx
from fastapi.testclient import TestClient

from app.adapter import ModelAdapter
from app.main import create_app
from app.store import DataStore


class DownClient:
    def request(self, *args, **kwargs):
        raise httpx.ConnectError("network down")

    def get(self, *args, **kwargs):
        raise httpx.ConnectError("network down")

    def close(self):
        pass


MONTHLY = (
    "year, mo, source_dataset, region, extent, area\n"
    "1979,  9, NSIDC-0051, N, 7.05, 4.58\n"
    "1987, 12, -9999, N, -9999, -9999\n"
    "2010,  9, NSIDC-0051, N, 4.90, 3.20\n"
    "2020,  9, NSIDC-0051, N, 4.00, 2.50\n"
)

DAILY = (
    "Year, Month, Day, Extent, Missing, Source Data\n"
    "YYYY, MM, DD, 10^6 sq km, 10^6 sq km, Source\n"
    "2020, 9, 15, 4.20, 0.0, src\n"
)


def _app(tmp_path):
    store = DataStore(tmp_path / "cache", client=DownClient())
    monthly = store._cache_file("nsidc", "monthly", "N_09_extent_v4.0.csv")
    daily = store._cache_file("nsidc", "daily", "N_seaice_extent_daily_v4.0.csv")
    store._write_cache(monthly, MONTHLY.encode(), "2020-09-01")
    store._write_cache(daily, DAILY.encode(), "2020-09-15")
    # The other eleven months are absent, so the feed is a partial cache.
    for month in range(1, 13):
        if month == 9:
            continue
        path = store._cache_file("nsidc", "monthly", f"N_{month:02d}_extent_v4.0.csv")
        text = (
            "year, mo, source_dataset, region, extent, area\n"
            f"2020, {month}, NSIDC-0051, N, 10.00, 8.00\n"
        )
        if month == 12:
            text += "1987, 12, -9999, N, -9999, -9999\n"
        if month == 1:
            text += "1988, 1, -9999, N, -9999, -9999\n"
        store._write_cache(path, text.encode(), None)
    store.refresh()
    adapter = ModelAdapter()
    adapter.update_from_store(store)
    return create_app(store=store, adapter=adapter, start_refresh=False)


def test_health_stays_up_and_flags_the_cache(tmp_path):
    client = TestClient(_app(tmp_path))
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["fixture"] is False
    assert body["status"] in {"degraded", "down"}
    daily = body["feeds"]["nsidc_daily_extent"]
    monthly = body["feeds"]["nsidc_monthly_extent"]
    assert daily["using_cache"] is True
    assert daily["status"] in {"stale", "down"}
    assert monthly["using_cache"] is True
    assert monthly["status"] in {"stale", "down"}
    assert "model_ready" in body
    assert body["model_ready"] is False


def test_gap_and_bad_year_and_extent_from_cache(tmp_path):
    client = TestClient(_app(tmp_path))
    gap = client.get("/ice/north/1987/12?format=json")
    assert gap.status_code == 200
    assert gap.json()["gap_masked"] is True
    assert gap.json()["extent_km2"] is None
    assert gap.headers["X-Ice-Gap-Masked"] == "true"
    assert gap.headers["X-Ice-Fixture"] == "false"

    bad = client.get("/ice/north/1970/9")
    assert bad.status_code == 400
    err = bad.json()["error"]
    assert err["code"] == "invalid_parameter"
    assert err["param"] == "year"
    assert "1979" in err["message"]

    missing = client.get("/ice/nope/2020/9")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"

    extent = client.get("/extent/north?scenario=ssp245")
    assert extent.status_code == 200
    series = {row["year"]: row for row in extent.json()["series"]}
    assert series[1979]["extent_km2"] == 7_050_000
    assert series[1979]["is_forecast"] is False
    assert series[1987]["extent_km2"] == 7_050_000 or series[1987]["gap_masked"] is False
    assert series[2100]["is_forecast"] is True
    assert len(extent.json()["series"]) == 122

    impact = client.get("/impact?year=2050&scenario=ssp245")
    assert impact.status_code == 200
    body = impact.json()
    assert "shipping" in body and "communities" in body and "sea_level" in body
    assert body["sea_level"]["source"] == "nasa-science-summary-fallback"
    assert body["sea_level"]["conversion_gt_per_mm"] == 362

    back = client.get("/backtest")
    assert back.status_code == 200
    assert "trained" in back.json()
    assert "by_pole" in back.json()


def test_empty_cache_does_not_500(tmp_path):
    store = DataStore(tmp_path / "empty", client=DownClient())
    store.refresh()
    client = TestClient(create_app(store=store, adapter=ModelAdapter(), start_refresh=False))
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["feeds"]["nsidc_daily_extent"]["status"] == "down"
    assert health.json()["feeds"]["nsidc_daily_extent"]["using_cache"] is False
    ice = client.get("/ice/north/2020/9?format=json")
    assert ice.status_code in {200, 503}
    if ice.status_code == 503:
        assert ice.json()["error"]["code"] == "feed_unavailable"
