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
- Warming history is NASA GISTEMP v4 (J-D), shifted so the 1880–1900 mean is 0 °C, then spliced from the last observed year to each SSP's 2081–2100 anchor in 2090. Cumulative CO2 is not the regressor: the IIASA timeseries route returned HTTP 500 and no Global Carbon Budget series was joined (`regressor: ipcc_ar6_warming_c_fallback`).
- Shipping days and the winter index, while the model package is absent, come from concentration grids: the share of corridor cells below 15% (Northern Sea Route and Northwest Passage), and February ocean cells near Utqiagvik, Kivalina, Shishmaref, and Tuktoyaktuk. Each block has a `method` string. `IceModel.impact` is used instead once that package loads.
- With no GRACE samples, `low_mm` and `high_mm` equal `total_mm`. There are no residuals to bootstrap. Rates are −264 and −135 Gt/yr from 2002.
- `ice_free_september_year` still comes from the September extent trend. `/health` adds `model_error`, `forecast_source`, and `gistemp_source` so a silent fallback is visible.
- `EARTHDATA_TOKEN` is read as a Bearer token, in addition to `EARTHDATA_USERNAME` / `EARTHDATA_PASSWORD`.
- Monthly staleness uses the 1st of the newest real month and the 45-day window from API.md.
