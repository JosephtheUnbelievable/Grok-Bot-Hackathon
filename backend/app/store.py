"""Fetch public feeds, cache the last good bytes, and clean them.

Each upstream call uses an 8 second timeout. A failed call never raises out of
`refresh`: the last good cache is served and the feed is marked stale or down.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from app.cacheio import atomic_write, atomic_write_json, read_json
from app.cleaning import (
    CleanGrid,
    ExtentRecord,
    is_gap_month,
    newest_daily,
    parse_daily_csv,
    parse_monthly_csv,
    read_concentration_tif,
)
from app.ssp import warming_table

logger = logging.getLogger("ice.store")

TIMEOUT = 8.0
NSIDC = "https://noaadata.apps.nsidc.org/NOAA/G02135"
GIBS_MAP = (
    "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"
    "?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0"
    "&LAYERS=MODIS_Terra_CorrectedReflectance_TrueColor&FORMAT=image/jpeg"
    "&WIDTH=64&HEIGHT=32&CRS=EPSG:4326&BBOX=-90,-180,90,180&TIME=2024-09-15"
)
# Advertised AMSR2 end date checked 2026-11-19. A successful historical GetMap
# still counts as the service being up; this end date makes the feed stale.
AMSR2_END = date(2025, 9, 1)
NE_LAND = "https://naciscdn.org/naturalearth/110m/physical/ne_110m_land.zip"
NE_COAST = "https://naciscdn.org/naturalearth/110m/physical/ne_110m_coastline.zip"
IIASA_TS = "https://ixmp4.ece.iiasa.ac.at/v1/ssp-2018/iamc/timeseries/?table=true"
MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
FEED_IDS = (
    "nsidc_daily_extent",
    "nsidc_monthly_extent",
    "nsidc_monthly_concentration",
    "gibs",
    "grace_icesheet",
    "ssp_pathways",
    "natural_earth",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class GridItem:
    pole: str
    year: int
    month: int
    grid: CleanGrid
    extent_km2: float | None
    area_km2: float | None
    gap_masked: bool
    from_cache: bool


@dataclass
class ExtentItem:
    pole: str
    year: int
    month: int
    extent_km2: float | None
    area_km2: float | None
    gap_masked: bool


def _blank_feed(detail: str = "not fetched") -> dict:
    return {
        "status": "down",
        "last_updated": None,
        "last_success": None,
        "age_seconds": None,
        "stale": True,
        "using_cache": False,
        "detail": detail,
    }


def _feed(status: str, last_updated: datetime | None, last_success: datetime | None, using_cache: bool, detail: str) -> dict:
    now = utcnow()
    age = None
    if last_updated is not None:
        if last_updated.tzinfo is None:
            last_updated = last_updated.replace(tzinfo=timezone.utc)
        age = max(0, int((now - last_updated).total_seconds()))
    return {
        "status": status,
        "last_updated": iso_z(last_updated),
        "last_success": iso_z(last_success),
        "age_seconds": age,
        "stale": status != "ok",
        "using_cache": using_cache,
        "detail": detail[:240],
    }


class DataStore:
    def __init__(self, cache_dir: Path, client=None) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.lock = threading.RLock()
        self.monthly: dict[tuple[str, int, int], ExtentRecord] = {}
        self.grids: dict[tuple[str, int, int], tuple[CleanGrid, bool]] = {}
        self.daily_newest: dict[str, date | None] = {"north": None, "south": None}
        self.feeds = {name: _blank_feed() for name in FEED_IDS}
        self.observed_through: tuple[int, int] | None = None
        self.regressor = "ipcc_ar6_warming_c_fallback"
        self.warming = warming_table()
        self.cumulative_co2: dict[int, float] = {}
        self.ssp_cumulative: dict[str, dict[int, float]] = {}
        self.grace_greenland: list[tuple[float, float, float | None]] = []
        self.grace_antarctica: list[tuple[float, float, float | None]] = []
        self.monthly_from_cache = False
        self._missing: set[tuple[str, int, int]] = set()

    def refresh(self) -> None:
        own = self.client is None
        client = self.client or httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "ice-on-earth-backend/0.1"},
        )
        try:
            self._refresh(client)
        finally:
            if own:
                client.close()

    def _refresh(self, client) -> None:
        jobs = {}
        for pole in ("north", "south"):
            jobs[("daily", pole)] = self._daily_url(pole)
            for month in range(1, 13):
                jobs[("monthly", pole, month)] = self._monthly_url(pole, month)
        jobs[("gibs",)] = GIBS_MAP
        jobs[("ne", "land")] = NE_LAND
        jobs[("ne", "coast")] = NE_COAST

        results = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            future_map = {pool.submit(self._download, client, url): key for key, url in jobs.items()}
            for fut in as_completed(future_map):
                key = future_map[fut]
                try:
                    results[key] = fut.result()
                except Exception as exc:
                    results[key] = exc
        self._apply_daily(results)
        self._apply_monthly(results)
        self._apply_gibs(results.get(("gibs",)))
        self._apply_natural_earth(client, results.get(("ne", "land")), results.get(("ne", "coast")))
        self._apply_grace(client)
        self._apply_ssp(client)
        self._prefetch_grids(client)
        self._recompute_observed()

    def _download(self, client, url: str, method: str = "GET"):
        response = client.request(method, url, timeout=TIMEOUT, follow_redirects=True)
        return response.status_code, response.content, response.headers

    def _daily_url(self, pole: str) -> str:
        name = "N" if pole == "north" else "S"
        hem = "north" if pole == "north" else "south"
        return f"{NSIDC}/{hem}/daily/data/{name}_seaice_extent_daily_v4.0.csv"

    def _monthly_url(self, pole: str, month: int) -> str:
        name = "N" if pole == "north" else "S"
        hem = "north" if pole == "north" else "south"
        return f"{NSIDC}/{hem}/monthly/data/{name}_{month:02d}_extent_v4.0.csv"

    def tif_url(self, pole: str, year: int, month: int) -> str:
        hem = "north" if pole == "north" else "south"
        prefix = "N" if pole == "north" else "S"
        folder = f"{month:02d}_{MONTH_ABBR[month - 1]}"
        return (
            f"{NSIDC}/{hem}/monthly/geotiff/{folder}/"
            f"{prefix}_{year}{month:02d}_concentration_v4.0.tif"
        )

    def _cache_file(self, *parts: str) -> Path:
        return self.cache_dir.joinpath(*parts)

    def _write_cache(self, path: Path, data: bytes, last_updated: str | None) -> datetime:
        atomic_write(path, data)
        stored = utcnow()
        atomic_write_json(Path(str(path) + ".meta.json"), {"stored_at": iso_z(stored), "last_updated": last_updated})
        return stored

    def _read_cache(self, path: Path) -> tuple[bytes, datetime | None] | None:
        if not path.is_file():
            return None
        meta = read_json(Path(str(path) + ".meta.json")) or {}
        stored = _parse_iso(meta.get("stored_at"))
        if stored is None:
            stored = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        try:
            return path.read_bytes(), stored
        except OSError:
            return None

    def _apply_daily(self, results) -> None:
        newest = None
        used_cache = False
        live = 0
        failed = 0
        detail = "daily extent"
        for pole, filename in (("north", "N_seaice_extent_daily_v4.0.csv"), ("south", "S_seaice_extent_daily_v4.0.csv")):
            path = self._cache_file("nsidc", "daily", filename)
            payload = results.get(("daily", pole))
            text = None
            from_cache = False
            stored = None
            if isinstance(payload, tuple) and payload[0] == 200 and payload[1]:
                text = payload[1].decode("utf-8", errors="replace")
                stored = self._write_cache(path, payload[1], None)
                live += 1
            else:
                failed += 1
                cached = self._read_cache(path)
                if cached:
                    text = cached[0].decode("utf-8", errors="replace")
                    stored = cached[1]
                    from_cache = True
                    used_cache = True
            rows = parse_daily_csv(text) if text else []
            day = newest_daily(rows)
            self.daily_newest[pole] = day
            if day and (newest is None or day > newest):
                newest = day
                detail = f"{filename} newest {day.isoformat()}"
        if newest is None and failed:
            status = "stale" if used_cache else "down"
            self.feeds["nsidc_daily_extent"] = _feed(status, None, None, used_cache, "daily extent unavailable")
            return
        updated = datetime(newest.year, newest.month, newest.day, tzinfo=timezone.utc) if newest else None
        stale_window = False
        if newest is not None:
            stale_window = (utcnow().date() - newest).days > 5
        if failed and used_cache:
            status = "stale"
        elif failed and live == 0:
            status = "down"
        elif stale_window:
            status = "stale"
        else:
            status = "ok"
        self.feeds["nsidc_daily_extent"] = _feed(status, updated, utcnow() if live else stored, used_cache or (live == 0 and used_cache), detail)

    def _apply_monthly(self, results) -> None:
        merged: dict[tuple[str, int, int], ExtentRecord] = {}
        live = 0
        failed = 0
        used_cache = False
        newest = None
        newest_name = "monthly extent"
        stored_at = None
        for pole in ("north", "south"):
            prefix = "N" if pole == "north" else "S"
            for month in range(1, 13):
                filename = f"{prefix}_{month:02d}_extent_v4.0.csv"
                path = self._cache_file("nsidc", "monthly", filename)
                payload = results.get(("monthly", pole, month))
                text = None
                if isinstance(payload, tuple) and payload[0] == 200 and payload[1]:
                    text = payload[1].decode("utf-8", errors="replace")
                    stored_at = self._write_cache(path, payload[1], None)
                    live += 1
                else:
                    failed += 1
                    cached = self._read_cache(path)
                    if cached:
                        text = cached[0].decode("utf-8", errors="replace")
                        stored_at = cached[1]
                        used_cache = True
                if not text:
                    continue
                for rec in parse_monthly_csv(text, pole):
                    if is_gap_month(rec.year, rec.month):
                        rec = ExtentRecord(rec.year, rec.month, None, None, True)
                    merged[(pole, rec.year, rec.month)] = rec
                    if rec.extent_km2 is not None and (newest is None or (rec.year, rec.month) > newest):
                        newest = (rec.year, rec.month)
                        newest_name = filename
        with self.lock:
            self.monthly = merged
            self.monthly_from_cache = used_cache and live == 0
        if not merged:
            self.feeds["nsidc_monthly_extent"] = _feed(
                "stale" if used_cache else "down", None, stored_at, used_cache, "monthly extent unavailable"
            )
            return
        updated = datetime(newest[0], newest[1], 1, tzinfo=timezone.utc) if newest else None
        window = False
        if newest:
            window = (utcnow().date() - date(newest[0], newest[1], 1)).days > 45
        if failed and used_cache:
            status = "stale"
        elif window:
            status = "stale"
        elif failed and live == 0:
            status = "down"
        else:
            status = "ok" if not window else "stale"
        # A partial failure with some live files is still usable, but flag it.
        if failed and live:
            status = "stale"
            used_cache = used_cache
        self.feeds["nsidc_monthly_extent"] = _feed(
            status, updated, stored_at or utcnow(), used_cache and not live, newest_name
        )
        if failed and live and used_cache:
            self.feeds["nsidc_monthly_extent"]["using_cache"] = True

    def _apply_gibs(self, payload) -> None:
        path = self._cache_file("gibs", "getmap.jpg")
        ok = isinstance(payload, tuple) and payload[0] == 200 and payload[1][:2] == b"\xff\xd8"
        end = AMSR2_END
        lag = (utcnow().date() - end).days > 14
        if ok:
            stored = self._write_cache(path, payload[1], end.isoformat())
            status = "stale" if lag else "ok"
            detail = f"GetMap ok; AMSRU2 end {end.isoformat()}"
            self.feeds["gibs"] = _feed(status, datetime(end.year, end.month, end.day, tzinfo=timezone.utc), stored, False, detail)
            return
        cached = self._read_cache(path)
        if cached:
            self.feeds["gibs"] = _feed("stale", datetime(end.year, end.month, end.day, tzinfo=timezone.utc), cached[1], True, "GetMap failed; cached jpeg")
        else:
            self.feeds["gibs"] = _feed("down", None, None, False, "GetMap failed")

    def _apply_natural_earth(self, client, land, coast) -> None:
        ok_headers = []
        failed = False
        for label, payload, url in (("land", land, NE_LAND), ("coast", coast, NE_COAST)):
            path = self._cache_file("natural_earth", f"ne_110m_{'land' if label == 'land' else 'coastline'}.zip")
            if isinstance(payload, tuple) and payload[0] == 200:
                ok_headers.append(payload[2])
                # The HEAD-sized GET is the zip itself when we requested the zip URL.
                if payload[1][:2] == b"PK":
                    self._write_cache(path, payload[1], _header_date(payload[2]))
            else:
                # The parallel job used GET. A failure here is a failed fetch.
                cached = self._read_cache(path)
                if cached:
                    failed = True
                else:
                    failed = True
        if ok_headers and not failed:
            last = _header_date(ok_headers[0])
            moment = _parse_http_date(last) or datetime(2021, 9, 4, tzinfo=timezone.utc)
            self.feeds["natural_earth"] = _feed("ok", moment, utcnow(), False, "ne_110m_land.zip")
            return
        land_c = self._read_cache(self._cache_file("natural_earth", "ne_110m_land.zip"))
        coast_c = self._read_cache(self._cache_file("natural_earth", "ne_110m_coastline.zip"))
        if land_c or coast_c:
            stored = (land_c or coast_c)[1]
            self.feeds["natural_earth"] = _feed("stale", stored, stored, True, "HEAD/GET failed; cached zip")
        else:
            self.feeds["natural_earth"] = _feed("down", None, None, False, "natural earth unreachable")

    def _apply_grace(self, client) -> None:
        token = os.environ.get("EARTHDATA_TOKEN", "").strip()
        user = os.environ.get("EARTHDATA_USERNAME", "").strip()
        password = os.environ.get("EARTHDATA_PASSWORD", "").strip()
        if not token and not (user and password):
            self.grace_greenland = []
            self.grace_antarctica = []
            self.feeds["grace_icesheet"] = _feed(
                "stale", None, None, False, "earthdata-login-required; nasa-science-summary-fallback"
            )
            return
        # One attempt. Credentials are sent as a header or basic auth and never logged.
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        auth = (user, password) if user and password and not token else None
        points = {}
        error = "download failed"
        for name, short in (
            ("greenland", "GREENLAND_MASS_TELLUS_MASCON_CRI_TIME_SERIES_RL06.3_V4"),
            ("antarctica", "ANTARCTICA_MASS_TELLUS_MASCON_CRI_TIME_SERIES_RL06.3_V4"),
        ):
            url = (
                "https://cmr.earthdata.nasa.gov/search/granules.json"
                f"?short_name={short}&page_size=1"
            )
            try:
                meta = client.get(url, timeout=TIMEOUT)
                href = _grace_href(meta.json() if meta.status_code == 200 else {})
            except Exception:
                href = None
            if not href:
                error = "cmr lookup failed"
                continue
            try:
                fetched = client.get(href, headers=headers, auth=auth, timeout=TIMEOUT, follow_redirects=True)
            except Exception as exc:
                error = type(exc).__name__
                continue
            if fetched.status_code != 200 or fetched.content[:1] == b"<":
                error = f"HTTP {fetched.status_code}"
                continue
            parsed = _parse_grace(fetched.text)
            if parsed:
                points[name] = parsed
                self._write_cache(self._cache_file("grace", f"{name}.txt"), fetched.content, None)
        if points.get("greenland") or points.get("antarctica"):
            self.grace_greenland = points.get("greenland", [])
            self.grace_antarctica = points.get("antarctica", [])
            last = 0.0
            for series in points.values():
                if series:
                    last = max(last, series[-1][0])
            moment = datetime(int(last), 1, 1, tzinfo=timezone.utc) if last else None
            window = moment is not None and (utcnow() - moment).days > 180
            self.feeds["grace_icesheet"] = _feed(
                "stale" if window else "ok", moment, utcnow(), False, "GRACE/GRACE-FO mascon time series"
            )
            return
        cached_g = self._read_cache(self._cache_file("grace", "greenland.txt"))
        if cached_g:
            self.grace_greenland = _parse_grace(cached_g[0].decode("utf-8", errors="replace"))
            cached_a = self._read_cache(self._cache_file("grace", "antarctica.txt"))
            self.grace_antarctica = _parse_grace(cached_a[0].decode("utf-8", errors="replace")) if cached_a else []
            self.feeds["grace_icesheet"] = _feed("stale", None, cached_g[1], True, f"{error}; using cache")
            return
        self.grace_greenland = []
        self.grace_antarctica = []
        self.feeds["grace_icesheet"] = _feed(
            "stale", None, None, False, f"{error}; nasa-science-summary-fallback"
        )

    def _apply_ssp(self, client) -> None:
        """One live IIASA call. The timeseries metadata route was HTTP 500 on 2026-11-19,
        so a failure here selects the IPCC warming anchors and marks the feed stale.
        """
        try:
            response = client.request(
                "PATCH",
                IIASA_TS,
                json={"variable": {"name": "Emissions|CO2"}, "region": {"name": "World"}},
                timeout=TIMEOUT,
                follow_redirects=True,
            )
            code = response.status_code
        except Exception as exc:
            code = 0
            response = None
            err = type(exc).__name__
        else:
            err = ""
        if response is not None and code == 200:
            try:
                body = response.json()
                columns = body.get("results", {}).get("columns") or body.get("columns")
            except Exception:
                columns = None
            # A usable pathway table would name model, scenario, and year. The live
            # route does not. Anything else is the labeled warming fallback.
            if columns and "scenario" in columns and "value" in columns:
                self.regressor = "cumulative_co2_gt"
                self.feeds["ssp_pathways"] = _feed("ok", utcnow(), utcnow(), False, "marker Emissions|CO2")
                return
        self.regressor = "ipcc_ar6_warming_c_fallback"
        self.cumulative_co2 = {}
        self.ssp_cumulative = {}
        self.warming = warming_table()
        why = f"IIASA timeseries HTTP {code or err}; ipcc_ar6_warming_c_fallback"
        self.feeds["ssp_pathways"] = _feed("stale", None, None, False, why)

    def _prefetch_grids(self, client) -> None:
        wanted = []
        with self.lock:
            monthly = dict(self.monthly)
        for pole, default_month in (("north", 9), ("south", 2)):
            latest = _latest_month(monthly, pole)
            if latest:
                wanted.append((pole, latest[0], latest[1]))
            years = [y for (p, y, m), rec in monthly.items() if p == pole and m == default_month and rec.extent_km2 is not None]
            if years:
                wanted.append((pole, max(years), default_month))
        # A known-good September, so /ice/north/2024/9 works even if the newest tif lags.
        wanted.append(("north", 2024, 9))
        seen = set()
        unique = []
        for item in wanted:
            if item not in seen and not is_gap_month(item[1], item[2]):
                seen.add(item)
                unique.append(item)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda spec: self.ensure_grid(*spec, client=client), unique))
        self._update_concentration_feed()

    def ensure_grid(self, pole: str, year: int, month: int, client=None) -> CleanGrid | None:
        if is_gap_month(year, month):
            return None
        key = (pole, year, month)
        with self.lock:
            found = self.grids.get(key)
        if found:
            return found[0]
        if key in self._missing:
            return None
        path = self._cache_file(
            "nsidc", "geotiff", f"{'N' if pole == 'north' else 'S'}_{year}{month:02d}_concentration_v4.0.tif"
        )
        own = client is None and self.client is None
        http = client or self.client or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
        from_cache = False
        data = None
        try:
            try:
                status, body, _headers = self._download(http, self.tif_url(pole, year, month))
            except Exception:
                status, body = 0, b""
            if status == 200 and len(body) > 1000 and body[:2] in (b"II", b"MM"):
                data = body
                self._write_cache(path, body, f"{year:04d}-{month:02d}-01")
            else:
                cached = self._read_cache(path)
                if cached and cached[0][:2] in (b"II", b"MM"):
                    data = cached[0]
                    from_cache = True
                else:
                    self._missing.add(key)
                    return None
            grid = read_concentration_tif(data)
        except Exception:
            logger.exception("geotiff %s %s-%s", pole, year, month)
            self._missing.add(key)
            return None
        finally:
            if own:
                http.close()
        with self.lock:
            self.grids[key] = (grid, from_cache)
        return grid

    def _update_concentration_feed(self) -> None:
        with self.lock:
            keys = list(self.grids.keys())
            grids = dict(self.grids)
        if not keys:
            self.feeds["nsidc_monthly_concentration"] = _feed(
                "down", None, None, False, "no concentration geotiff"
            )
            return
        year, month = max((k[1], k[2]) for k in keys)
        from_cache = any(grids[k][1] for k in keys if (k[1], k[2]) == (year, month))
        updated = datetime(year, month, 1, tzinfo=timezone.utc)
        window = (utcnow().date() - date(year, month, 1)).days > 45
        status = "stale" if window or from_cache else "ok"
        prefix = "N" if any(k[0] == "north" and (k[1], k[2]) == (year, month) for k in keys) else "S"
        detail = f"{prefix}_{year}{month:02d}_concentration_v4.0.tif"
        self.feeds["nsidc_monthly_concentration"] = _feed(status, updated, utcnow(), from_cache, detail)

    def _recompute_observed(self) -> None:
        newest = None
        with self.lock:
            for (pole, year, month), rec in self.monthly.items():
                if rec.extent_km2 is None or rec.gap_masked:
                    continue
                if newest is None or (year, month) > newest:
                    newest = (year, month)
            self.observed_through = newest

    def iter_extent_rows(self) -> list[ExtentItem]:
        with self.lock:
            items = [
                ExtentItem(pole, year, month, rec.extent_km2, rec.area_km2, rec.gap_masked)
                for (pole, year, month), rec in self.monthly.items()
            ]
        return items

    def iter_grids(self) -> list[GridItem]:
        with self.lock:
            monthly = dict(self.monthly)
            grids = dict(self.grids)
        items = []
        for (pole, year, month), (grid, from_cache) in grids.items():
            rec = monthly.get((pole, year, month))
            items.append(
                GridItem(
                    pole=pole,
                    year=year,
                    month=month,
                    grid=grid,
                    extent_km2=None if rec is None else rec.extent_km2,
                    area_km2=None if rec is None else rec.area_km2,
                    gap_masked=bool(rec.gap_masked) if rec else False,
                    from_cache=from_cache,
                )
            )
        return items

    def record(self, pole: str, year: int, month: int) -> ExtentRecord | None:
        with self.lock:
            return self.monthly.get((pole, year, month))

    def grid_for(self, pole: str, year: int, month: int) -> tuple[CleanGrid, bool] | None:
        with self.lock:
            return self.grids.get((pole, year, month))

    def has_monthly(self, pole: str | None = None) -> bool:
        with self.lock:
            if pole is None:
                return bool(self.monthly)
            return any(p == pole for p, _, _ in self.monthly)

    def ice_can_answer(self) -> bool:
        return self.has_monthly() or bool(self.grids)

    def health(self, model_ready: bool) -> dict:
        feeds = {name: dict(self.feeds.get(name) or _blank_feed()) for name in FEED_IDS}
        statuses = [item["status"] for item in feeds.values()]
        if all(status == "ok" for status in statuses):
            overall = "ok"
        elif self.ice_can_answer() or model_ready:
            overall = "degraded"
        else:
            overall = "down"
        obs = None
        if self.observed_through:
            obs = {"year": self.observed_through[0], "month": self.observed_through[1]}
        return {
            "status": overall,
            "fixture": False,
            "generated_at": iso_z(utcnow()),
            "observed_through": obs,
            "regressor": self.regressor,
            "model_ready": model_ready,
            "feeds": feeds,
        }


def _latest_month(monthly, pole: str) -> tuple[int, int] | None:
    best = None
    for (p, year, month), rec in monthly.items():
        if p == pole or rec.extent_km2 is None:
            continue
        if best is None or (year, month) > best:
            best = (year, month)
    # bugfix below — the condition above is wrong. Recompute correctly.
    best = None
    for (p, year, month), rec in monthly.items():
        if p != pole or rec.extent_km2 is None or rec.gap_masked:
            continue
        if best is None or (year, month) > best:
            best = (year, month)
    return best


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _header_date(headers) -> str | None:
    if not headers:
        return None
    return headers.get("last-modified") or headers.get("Last-Modified")


def _parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S GMT", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _grace_href(payload: dict) -> str | None:
    entries = (payload.get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    for entry in entries:
        for link in entry.get("links") or []:
            href = link.get("href") or ""
            if href.endswith(".txt") and "protected" in href:
                return href
    return None


def _parse_grace(text: str) -> list[tuple[float, float, float | None]]:
    points = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not (line[0].isdigit() or line[0] in "+-"):
            continue
        parts = line.replace(",", " ").split()
        if len(parts) < 2:
            continue
        try:
            year = float(parts[0])
            mass = float(parts[1])
            unc = float(parts[2]) if len(parts) > 2 else None
        except ValueError:
            continue
        if 1990 < year < 2100:
            points.append((year, mass, unc))
    points.sort()
    return points
