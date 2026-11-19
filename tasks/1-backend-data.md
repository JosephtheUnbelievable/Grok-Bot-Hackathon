# Role 1 — Data and API

You are one of three people in a ~40-minute hackathon. Build only the backend. Two other assistants are writing `model/` and `frontend/` at the same time. Stay inside your folder or you will collide.

## What you build

A FastAPI app that:

1. Downloads the public feeds below, with an 8 second timeout each.
2. Writes the last good bytes to `backend/cache/` (gitignored) using write-temp-then-rename.
3. Turns raw files into the cleaned structures in `API.md` (sentinels gone, extent in km², gap months flagged).
4. Calls `IceModel` from `model.ice_model` (repository root on `PYTHONPATH`).
5. Serves the HTTP contract in `API.md` exactly: paths, query names, JSON keys, units, headers, error shape.

Entry point: `backend/app/main.py` exposing `app`. From `backend/`:

```bash
PYTHONPATH=.. uvicorn app.main:app --reload --port 8000
```

If `import model.ice_model` fails, keep serving. `/health` returns HTTP 200 with `model_ready: false`. Observed months still render from NSIDC. Forecast requests return HTTP 200 with a fully masked grid, `extent_km2: null`, `source: "model-not-ready"`, `stale: true`. Do not crash the process.

## Folder ownership

- Branch: `feat/backend`
- You write: `backend/**` only (`app/`, `requirements.txt`, cache is runtime-only).
- You do not write: `model/`, `frontend/`, `fixtures/`, `tasks/`, `PLAN.md`, `API.md`, root `README.md`, `LICENSE`, `.gitignore`.
- You may read `API.md`, `PLAN.md`, and `fixtures/`.
- You may import `model`. You do not “fix” the model if the import works but a number looks wrong. Note it in `backend/README.md` under `Contract change requests`.

## Feeds and URLs

Checked 2026-11-19. Full notes are in `PLAN.md`. Implement from this list.

**NSIDC G02135 v4, no login.** Extent columns in the CSVs are **millions of km²**. Multiply by 1e6 once. Sentinel `-9999`.

- Daily (two header rows):
  - `https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/data/N_seaice_extent_daily_v4.0.csv`
  - `https://noaadata.apps.nsidc.org/NOAA/G02135/south/daily/data/S_seaice_extent_daily_v4.0.csv`
- Monthly extent, one file per calendar month (there is no combined monthly CSV; that URL 404s):
  - `https://noaadata.apps.nsidc.org/NOAA/G02135/north/monthly/data/N_{MM}_extent_v4.0.csv`
  - `https://noaadata.apps.nsidc.org/NOAA/G02135/south/monthly/data/S_{MM}_extent_v4.0.csv`
  - Columns: `year, mo, source_dataset, region, extent, area`.
- Monthly concentration GeoTIFF:
  - `https://noaadata.apps.nsidc.org/NOAA/G02135/north/monthly/geotiff/{MM}_{Mon}/N_{YYYY}{MM}_concentration_v4.0.tif`
  - South uses `south` and prefix `S_`.
  - Folders: `01_Jan` `02_Feb` `03_Mar` `04_Apr` `05_May` `06_Jun` `07_Jul` `08_Aug` `09_Sep` `10_Oct` `11_Nov` `12_Dec`.
- uint16 class values: `0–1000` means percent × 10; `2510` pole hole → store 100 and count `pole_hole_cells`; `2530` / `2540` / `2550` → masked 255. Map these **before** any call into the model.
- Gap, verified in the v4 files: monthly 1987-12 and 1988-01 are `-9999`. Daily dates 1987-12-03 through 1988-01-12 are absent. For those months set `gap_masked: true`, `extent_km2: null`, grid all 255. Do not interpolate. Every-other-day SMMR sampling before 1987-08-20 is not a gap.
- Trust the GeoTIFF’s CRS tag. Report it as `grid.projection`. Read width and height from the file.

**NASA GIBS, no login.** Used for `/health` and as the documented basemap. The historical slider does not depend on it. A HEAD or a tiny GetMap is enough:

`https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS=MODIS_Terra_CorrectedReflectance_TrueColor&FORMAT=image/jpeg&WIDTH=64&HEIGHT=32&CRS=EPSG:4326&BBOX=-90,-180,90,180&TIME=2024-09-15`

The AMSR2 sea-ice layer’s advertised end date was 2025-09-01. If that end is more than 14 days behind today, mark `gibs` stale even when the GetMap succeeds.

**GRACE / GRACE-FO.** Product pages (the files themselves need an Earthdata login):

- `https://podaac.jpl.nasa.gov/dataset/GREENLAND_MASS_TELLUS_MASCON_CRI_TIME_SERIES_RL06.3_V4`
- `https://podaac.jpl.nasa.gov/dataset/ANTARCTICA_MASS_TELLUS_MASCON_CRI_TIME_SERIES_RL06.3_V4`

Read `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD` from the environment or `backend/.env`. Never commit them. If login is missing or the download fails, pass empty `grace_greenland` / `grace_antarctica` lists into the bundle and let the model use the −264 / −135 Gt/yr fallback. Health feed `grace_icesheet` is then `stale` with `detail` explaining why. Do not invent a mass time series in the backend.

**SSP / CO2.** Prefer a live marker pathway (World `Emissions|CO2`, cumulated onto a Global Carbon Budget running sum from 1850). Explorer: `https://ssp.apps.ece.iiasa.ac.at/`. The old `data.ece.iiasa.ac.at/ssp/` host has migrated; do not call it. If the query fails, set `TrainingBundle.regressor` to `ipcc_ar6_warming_c_fallback` and fill `ssp_warming_c` from IPCC AR6 WG1 Table SPM.1 best estimates: 2021–2040 is 1.5 °C on all five pathways; 2081–2100 is 1.4, 1.8, 2.7, 3.6, 4.4 °C for `ssp119`, `ssp126`, `ssp245`, `ssp370`, `ssp585`. Interpolate linearly in year. Leave `ssp_cumulative_co2_gt` empty in that mode. Health feed `ssp_pathways` is `stale`.

**Natural Earth, no login.** HEAD these; do not fail the ice routes if they fail:

- `https://naciscdn.org/naturalearth/110m/physical/ne_110m_land.zip`
- `https://naciscdn.org/naturalearth/110m/physical/ne_110m_coastline.zip`

A static 2021 `Last-Modified` is `ok`. Stale only when the HEAD fails and a cached zip exists.

## Contract you implement

Implement every route in `API.md`. The shapes below are the checklist; field-level notes (blend math, headers, sentinel table, freshness windows) are in `API.md` and they win if this summary is shorter.

| Method and path | Job |
| --- | --- |
| `GET /health` | Per-feed status for `nsidc_daily_extent`, `nsidc_monthly_extent`, `nsidc_monthly_concentration`, `gibs`, `grace_icesheet`, `ssp_pathways`, `natural_earth`. Overall `ok` / `degraded` / `down`. Always HTTP 200. |
| `GET /ice/{pole}/{year}/{month}` | Query `scenario`, `blend`, `band`, `format=png\|json`. PNG is equirectangular **1024×512**, north up, −180 at the left, RGBA, open water and mask transparent, ice `#5dade2`→`#ffffff` from 15% to 100%. JSON is the native grid as `values_b64`. |
| `GET /extent/{pole}` | Years 1979–2100 for one month. Default month 9 north, 2 south. |
| `GET /impact` | `year` required. Shipping proxy, ice-free September year, winter index, sea level. The model computes the numbers; you serialize them and copy the definition strings from `API.md`. |
| `GET /backtest` | No query. Pass through `IceModel.backtest()`. |

Errors: `{ "error": { "code", "message", "param" } }` with 400 / 404 / 503 as in `API.md`. A stale cache is 200, never 503.

`observed_through` is the newest monthly CSV row that is not `-9999`. Echo it on health, ice, extent, and impact.

`blend` in `[0, 4]` overrides `scenario`. Canonical scenario ids: `ssp119`, `ssp126`, `ssp245`, `ssp370`, `ssp585`.

Observed extent comes from the CSV. Forecast extent comes from the model (`extent_method: "model_cell_sum_scaled"`). Do not recompute observed extent from pixels.

CORS: allow `*` in dev. `fixture` is always `false` on this server. `Cache-Control: public, max-age=300` when not stale, `no-cache` when stale.

Fit the model off the request path (startup thread plus after each successful refresh). Swap in the new `IceModel` under a lock. Requests use the previous fit until then. First-run forecast behaviour is the `model-not-ready` rule above, not a blocked worker.

You own the equirectangular warp for `format=png`. Nearest-neighbour from the GeoTIFF georeferencing is enough. If the warp library is missing, still return JSON, and for PNG return the last cached PNG or a transparent 1024×512 with `stale: true` rather than a 500.

## How to test alone

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=.. .venv/bin/uvicorn app.main:app --port 8000
```

Suggested `requirements.txt` (you may add a GeoTIFF reader; keep it installable in a couple of minutes): `fastapi`, `uvicorn`, `httpx`, `pillow`, `numpy`. Add the raster reader you actually call.

With the network unplugged after one successful fetch:

- `curl -s localhost:8000/health` is JSON, HTTP 200, and any feed you broke says `stale` or `down` with `using_cache: true` where a cache file exists.
- `curl -s -o /tmp/ice.png -D - "localhost:8000/ice/north/2020/9?format=png"` writes a PNG (or a transparent PNG if you only have a cache of the CSV) and shows `X-Ice-Stale`.
- `curl -s "localhost:8000/ice/north/1987/12?format=json"` has `gap_masked: true` and `extent_km2: null`.
- `curl -s -o /dev/null -w "%{http_code}" "localhost:8000/ice/north/1970/9"` is 400 and the body matches `fixtures/error.json`’s shape.
- `curl -s "localhost:8000/extent/north?scenario=ssp245"` returns a `series` array.
- `curl -s "localhost:8000/impact?year=2050&scenario=ssp245"` returns all three panels’ keys even when GRACE is missing (`sea_level.source` may be `nasa-science-summary-fallback`).
- `curl -s localhost:8000/backtest` returns JSON with `trained` true or false, never a stack trace.

Point a second terminal at `fixtures/` only as a schema reference. Do not start the frontend; that is Role 3.

## Acceptance criteria

- All five routes exist and match `API.md` keys and units.
- A down feed does not take down `/health` or a route that has a cache.
- 1987-12 and 1988-01 are masked. `-9999` and GeoTIFF flags never appear in JSON.
- Extent from the monthly CSV is converted from millions of km² exactly once.
- Forecast responses set `is_forecast: true` only after `observed_through`.
- Earthdata secrets are not in git.
- `git status` shows changes only under `backend/`.

## Minute 20

Push `feat/backend`. At the checkpoint, merge order is model, then you, then frontend. Rebase onto `main` first. After your merge, `PYTHONPATH=.. uvicorn app.main:app --port 8000` on `main` should import `model` and return `/health`.
