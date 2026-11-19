# backend/

Role 1. Branch `feat/backend`. FastAPI on port 8000.

It downloads NSIDC Sea Ice Index G02135 (daily and monthly extent, monthly concentration GeoTIFFs), checks NASA GIBS and Natural Earth, and reads GRACE/GRACE-FO only when Earthdata credentials are present. IIASA SSP pathways are attempted once; if that query cannot be turned into cumulative CO2, forecasts use the IPCC AR6 warming anchors (`regressor: ipcc_ar6_warming_c_fallback`).

`model.ice_model.IceModel` is called when that package imports. Until it does, a linear trend on the real monthly extent series answers forecast routes. `model_ready` stays false in that mode. The fallback is not fixture data.

## Run

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=.. .venv/bin/uvicorn app.main:app --port 8000
```

Optional credentials live in `backend/.env` (see `.env.example`). Do not commit them.

```bash
PYTHONPATH=.. .venv/bin/pytest
```

Cache is `backend/cache/` (gitignored). A failed fetch serves the last good bytes and sets the feed to `stale` or `down`. `/health` stays HTTP 200.

## Contract change requests

_Do not edit API.md from this branch. Notes for the checkpoint:_

- When `import model.ice_model` fails, `/health` reports `model_ready: false`, but forecast `/extent`, `/impact`, and `/ice` are still filled by a linear trend on the fetched NSIDC monthly series (source `model-forecast`, `stale: true`). The brief also describes a fully masked `model-not-ready` grid. Confirm which one the globe should treat as the checkpoint forecast. A forecast month with no concentration template still returns `source: "model-not-ready"`.
- IPCC fallback warming is 1.5 °C for every year through 2040 (including 1979–2020), linear from 2040 to the 2081–2100 anchor, then flat. AR6 does not specify the pre-2021 values.
- With no GRACE samples, `low_mm` and `high_mm` equal `total_mm`. There are no residuals to bootstrap. Rates are −264 and −135 Gt/yr from 2002.
- The fallback does not compute the shipping corridors or the winter-ice index (both need many concentration grids and the model’s lat/lon boxes). Those fields are 0 / null, with the API.md definition strings copied through. `ice_free_september_year` comes from the September extent trend.
- `EARTHDATA_TOKEN` is read as a Bearer token, in addition to `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`.
- Monthly staleness uses the 1st of the newest real month and the 45-day window from API.md.
