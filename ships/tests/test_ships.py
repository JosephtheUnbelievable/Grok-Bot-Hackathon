"""Offline tests for cleaning, staleness, ice risk, and the no-key routes.

The jsonl capture is loaded only through ``load_recorded_sample``. The HTTP
app is never pointed at that file.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ships.ice import assess, normalize_concentration, project
from ships.sample import FIXTURE_NOTICE, load_recorded_sample
from ships.worker import Fleet, backoff_seconds, bounding_boxes, jittered
from ships.router import create_app

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "testdata" / "ais_sample.jsonl"
NOW = datetime(2026, 11, 19, 23, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_live_keys(monkeypatch):
    monkeypatch.delenv("AISSTREAM_API_KEY", raising=False)
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)


def _when(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def position(mmsi, lat, lon, when, sog=10, cog=0, heading=0, name=None, message_type="PositionReport"):
    meta = {"MMSI": mmsi, "time_utc": _when(when) if isinstance(when, datetime) else when}
    if name:
        meta["ShipName"] = name
    body = {
        "UserID": mmsi,
        "Latitude": lat,
        "Longitude": lon,
        "Sog": sog,
        "Cog": cog,
        "TrueHeading": heading,
        "Valid": True,
    }
    return {"MessageType": message_type, "MetaData": meta, "Message": {message_type: body}}


def static(mmsi, name, type_code, when):
    stamp = _when(when) if isinstance(when, datetime) else when
    return {
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": mmsi, "time_utc": stamp},
        "Message": {"ShipStaticData": {"UserID": mmsi, "Name": name, "Type": type_code, "Valid": True}},
    }


def live(monkeypatch) -> Fleet:
    monkeypatch.setenv("AISSTREAM_API_KEY", "test-key-not-a-secret")
    fleet = Fleet()
    fleet.mark_connected(NOW)
    fleet.note_rx(NOW)
    return fleet


def ships_of(fleet: Fleet, **kwargs):
    body = fleet.ships_payload(now=kwargs.pop("now", NOW), **kwargs)
    return body, {ship["mmsi"]: ship for ship in body["ships"]}


def test_null_island_dropped_and_arctic_position_kept(monkeypatch):
    fleet = live(monkeypatch)
    fleet.ingest(position(273456789, 0, 0, NOW))
    fleet.ingest(position(273456789, 70, 10, NOW))
    body, rows = ships_of(fleet)
    assert "273456789" in rows
    assert rows["273456789"]["lat"] == 70
    assert rows["273456789"]["lon"] == 10
    assert body["fixture"] is False


def test_speed_sentinel_and_heading_sentinel(monkeypatch):
    fleet = live(monkeypatch)
    fleet.ingest(position(273456789, 70, 10, NOW, sog=80, heading=90))
    _, rows = ships_of(fleet)
    assert rows == {}
    fleet.ingest(position(273456789, 71, 11, NOW + timedelta(minutes=1), sog=102.3, cog=360, heading=511))
    _, rows = ships_of(fleet)
    ship = rows["273456789"]
    assert ship["lat"] == 71
    assert ship["sog_kn"] is None
    assert ship["cog_deg"] is None
    assert ship["heading"] is None
    fleet.ingest(position(273111222, 72, 12, NOW, sog=102.2, cog=15, heading=15))
    _, rows = ships_of(fleet)
    assert rows["273111222"]["sog_kn"] is None
    assert rows["273111222"]["lat"] == 72


def test_mmsi_rules(monkeypatch):
    fleet = live(monkeypatch)
    fleet.ingest(position(123, 70, 10, NOW))
    fleet.ingest(position("003456789", 70, 11, NOW))
    fleet.ingest(position(111111111, 70, 12, NOW))
    fleet.ingest(position(273456789, 70.5, 13, NOW))
    _, rows = ships_of(fleet)
    assert list(rows) == ["273456789"]


def test_duplicate_and_reorder_do_not_grow_the_track(monkeypatch):
    fleet = live(monkeypatch)
    start = NOW - timedelta(minutes=30)
    fleet.ingest(position(273456789, 70, 10, start, sog=11))
    fleet.ingest(position(273456789, 70.2, 10.2, NOW, sog=9))
    before = ships_of(fleet)[1]["273456789"]
    fleet.ingest(position(273456789, 70.2, 10.2, NOW, sog=1))
    fleet.ingest(position(273456789, 69, 9, start, sog=20, cog=180))
    _, rows = ships_of(fleet)
    ship = rows["273456789"]
    assert ship["lat"] == before["lat"]
    assert ship["lon"] == before["lon"]
    assert ship["sog_kn"] == 9
    assert len(ship["track"]) == 2
    stamps = [point["t"] for point in ship["track"]]
    assert stamps == sorted(stamps)
    for minute in range(13):
        fleet.ingest(position(230000111, 80 + minute * 0.01, 20, NOW + timedelta(minutes=minute), sog=5))
    _, rows = ships_of(fleet, now=NOW + timedelta(minutes=12))
    track = rows["230000111"]["track"]
    assert len(track) == 12
    assert track[0]["lat"] != 80
    assert [point["t"] for point in track] == sorted(point["t"] for point in track)
    assert track[-1]["source"] == "aisstream"


def test_recorded_sample_is_labeled_and_routes_do_not_read_it(monkeypatch):
    body = load_recorded_sample(SAMPLE)
    assert body["fixture"] is True
    assert body["source"] == "recorded-sample"
    assert body["fixture_notice"] == FIXTURE_NOTICE
    example = next(ship for ship in body["ships"] if ship["mmsi"] == "273456789")
    assert example["name"] == "EXAMPLE"
    assert example["vessel_type"] == "cargo"
    assert example["vessel_type_code"] == 70
    assert example["lat"] == 70.2
    assert example["sog_kn"] is None
    assert example["heading"] is None
    assert example["cog_deg"] is None
    assert len(example["track"]) == 2
    assert all(ship["lat"] >= 60 for ship in body["ships"])
    assert not any(ship["mmsi"] in {"123", "003456789"} for ship in body["ships"])
    ice = next(ship for ship in body["ships"] if ship["mmsi"] == "230000111")
    assert ice["icebreaker"] is True
    assert ice["name"] == "EXAMPLE ICEBREAKER"
    fishing = next(ship for ship in body["ships"] if ship["mmsi"] == "257111222")
    assert fishing["vessel_type"] == "fishing"
    plain = next(ship for ship in body["ships"] if ship["name"] == "EXAMPLE")
    assert plain["icebreaker"] is False

    source = (ROOT / "router.py").read_text(encoding="utf-8")
    assert "load_recorded_sample" not in source
    assert "ais_sample" not in source
    assert "recorded-sample" not in source

    _orig = Path.read_text

    def guarded(self, *args, **kwargs):
        if getattr(self, "name", "") == "ais_sample.jsonl":
            raise AssertionError("live route read the recorded sample")
        return _orig(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    app = create_app(start_worker=False)
    with TestClient(app) as client:
        health = client.get("/ships/health")
        ships = client.get("/ships")
    assert health.status_code == 200
    assert ships.status_code == 200
    assert health.json()["using_sample"] is False
    assert health.json()["fixture"] is False
    assert ships.json()["fixture"] is False
    assert ships.json()["ships"] == []
    assert "recorded-sample" not in ships.text


def test_disconnect_keeps_position_and_backoff_sequence(monkeypatch):
    fleet = live(monkeypatch)
    fleet.ingest(position(273456789, 76.5, -40, NOW, sog=8, cog=12, heading=12))
    fleet.mark_disconnected()
    body, rows = ships_of(fleet)
    assert rows["273456789"]["lat"] == 76.5
    assert rows["273456789"]["lon"] == -40
    assert rows["273456789"]["is_stale"] is True
    assert body["stale"] is True
    assert body["source"] == "aisstream-stale"
    health = fleet.health_payload(now=NOW)
    assert health["status"] == "stale"
    assert health["using_sample"] is False
    assert "test-key-not-a-secret" not in json.dumps(health)

    assert [backoff_seconds(i) for i in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30]
    assert backoff_seconds(-3) == 1
    assert backoff_seconds("nope") == 1
    for attempt in range(8):
        base = backoff_seconds(attempt)
        assert base <= jittered(base, 0) <= base * 1.25
        assert base <= jittered(base, 1) <= base * 1.25 + 1e-9
    assert len(bounding_boxes(False)) == 1
    assert len(bounding_boxes(True)) == 6


def test_own_age_stale_and_forget(monkeypatch):
    fleet = live(monkeypatch)
    fleet.ingest(position(273456789, 70, 10, NOW - timedelta(minutes=16)))
    fleet.ingest(position(230000222, 71, 11, NOW))
    body, rows = ships_of(fleet)
    assert body["stale"] is False
    assert rows["273456789"]["is_stale"] is True
    assert rows["230000222"]["is_stale"] is False
    kept, _ = ships_of(fleet, now=NOW - timedelta(minutes=16) + timedelta(hours=6))
    assert any(ship["mmsi"] == "273456789" for ship in kept["ships"])
    gone, _ = ships_of(fleet, now=NOW + timedelta(hours=6, seconds=1))
    assert gone["ships"] == []


def test_ice_projection_levels_and_missing_grid(monkeypatch):
    lat6, lon6 = project(70, 25, 0, 12 * 6)
    assert lat6 == pytest.approx(71.2, abs=0.02)
    assert lon6 == pytest.approx(25, abs=0.02)

    seen = []

    def sampler(lat, lon):
        seen.append((lat, lon))
        if lat >= 71:
            return 60
        return 0

    heavy = assess(70, 25, 12, 0, 90, sampler)
    assert seen[1][0] == pytest.approx(71.2, abs=0.02)
    assert seen[2][0] == pytest.approx(72.4, abs=0.02)
    assert heavy["level"] == "heavy"
    assert heavy["concentration_ahead"] == 60

    def marginal(lat, lon):
        return 0 if abs(lat - 70) < 0.05 else 20

    crossing = assess(70, 10, 12, 0, None, marginal)
    assert crossing["level"] == "marginal"
    assert crossing["entering"] is True
    assert crossing["concentration_here"] == 0
    assert crossing["concentration_ahead"] == 20

    clear = assess(70, 10, 12, 90, 0, lambda lat, lon: 10)
    assert clear["level"] == "clear"
    assert clear["entering"] is False

    unknown = assess(70, 10, 12, 0, 0, lambda lat, lon: None)
    assert unknown["level"] == "unknown"
    assert unknown["concentration_here"] is None
    assert unknown["concentration_ahead"] is None
    assert normalize_concentration(255) is None
    assert assess(70, 10, 5, 0, 0, lambda lat, lon: 255)["level"] == "unknown"

    calls = []

    def once(lat, lon):
        calls.append((lat, lon))
        return 40

    stopped = assess(70, 10, 0, 0, 0, once)
    assert len(calls) == 1
    assert stopped["concentration_ahead"] == 40
    assert assess(70, 10, None, 0, 0, lambda lat, lon: 4)["concentration_ahead"] is None

    fleet = live(monkeypatch)
    fleet.ingest(static(230000111, "EXAMPLE ICEBREAKER", 52, NOW - timedelta(minutes=1)))
    fleet.ingest(position(230000111, 80, 40, NOW, sog=8))
    fleet.ingest(static(273456789, "EXAMPLE", 70, NOW - timedelta(minutes=1)))
    fleet.ingest(position(273456789, 70, 10, NOW, sog=12, cog=0))

    def boom(lat, lon):
        raise RuntimeError("grid missing")

    body, rows = ships_of(fleet, concentration_at=boom)
    assert "230000111" in rows
    assert rows["230000111"]["icebreaker"] is True
    assert rows["230000111"]["ice_risk"]["level"] == "unknown"
    assert rows["273456789"]["icebreaker"] is False
    assert rows["273456789"]["vessel_type"] == "cargo"
    assert fleet.decode_errors >= 1
    assert body["count"] == 2


def test_gfw_history_does_not_move_the_ship(monkeypatch):
    fleet = live(monkeypatch)
    fleet.ingest(position(273456789, 70, 10, NOW, sog=6))
    fleet.add_gfw_points(273456789, [{"lat": 65, "lon": 12, "t": NOW - timedelta(hours=80)}])
    _, rows = ships_of(fleet)
    ship = rows["273456789"]
    assert ship["lat"] == 70
    assert ship["lon"] == 10
    assert any(point["source"] == "gfw" for point in ship["track"])
    assert ship["track"][-1]["source"] == "aisstream"


def test_no_key_routes_are_empty_and_worker_does_not_connect():
    app = create_app(start_worker=True)
    fleet = app.state.ships_fleet
    fleet.ingest(position(273456789, 70, 10, NOW))
    with TestClient(app) as client:
        health = client.get("/ships/health")
        ships = client.get("/ships")
    assert health.status_code == 200
    assert ships.status_code == 200
    assert health.headers["cache-control"] == "no-cache"
    assert ships.headers["cache-control"] == "no-cache"
    assert health.json()["status"] == "down"
    assert health.json()["using_sample"] is False
    assert health.json()["fixture"] is False
    assert "unset" in health.json()["detail"]
    assert health.json()["detail"].startswith("AISSTREAM_API_KEY is unset")
    assert "gfw: off" in health.json()["detail"]
    assert ships.json()["ships"] == []
    assert ships.json()["count"] == 0
    assert ships.json()["fixture"] is False
    assert ships.json()["source"] == "aisstream"
    assert "recorded-sample" not in ships.text + health.text

    async def no_connect():
        from ships.worker import run_forever

        await run_forever(Fleet())

    asyncio.run(no_connect())


def test_class_b_and_bad_frame(monkeypatch):
    fleet = live(monkeypatch)
    fleet.ingest(
        position(
            257111222,
            68.5,
            -20,
            NOW,
            sog=6.5,
            message_type="StandardClassBPositionReport",
        )
    )
    fleet.consume("{")
    _, rows = ships_of(fleet)
    assert rows["257111222"]["lat"] == 68.5
    assert fleet.decode_errors == 1


def test_keyed_route_returns_a_ship_without_opening_a_socket(monkeypatch):
    monkeypatch.setenv("AISSTREAM_API_KEY", "test-key-not-a-secret")
    app = create_app(start_worker=False, concentration_at=lambda lat, lon: 0)
    fleet = app.state.ships_fleet
    moment = datetime.now(timezone.utc)
    fleet.mark_connected(moment)
    fleet.note_rx(moment)
    fleet.ingest(static(273456789, "EXAMPLE", 70, moment - timedelta(minutes=1)))
    fleet.ingest(position(273456789, 72.1, 40.2, moment, sog=12.4, cog=35, heading=40))
    with TestClient(app) as client:
        response = client.get("/ships")
        health = client.get("/ships/health")
    assert response.status_code == 200
    body = response.json()
    ship = body["ships"][0]
    assert ship["mmsi"] == "273456789"
    assert ship["name"] == "EXAMPLE"
    assert ship["vessel_type"] == "cargo"
    assert ship["ice_risk"]["level"] == "clear"
    assert ship["icebreaker"] is False
    assert body["bounding_box"]["min_lat"] == 60
    assert health.json()["status"] == "ok"
    assert health.json()["using_sample"] is False
    assert "test-key-not-a-secret" not in response.text + health.text
