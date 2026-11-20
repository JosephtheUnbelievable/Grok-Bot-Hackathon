# Grok-Bot-Hackathon

A 3D globe of sea ice at both poles and the Greenland and Antarctic ice sheets, from the satellite record into five climate scenarios. Built for the Berkeley × Stanford Grok Bot Hackathon, Track 3.

The MIT license is in `LICENSE` (Copyright 2026 Joseph Lin).

How the three people work without colliding is in `PLAN.md`. The HTTP and Python contract is in `API.md`. Paste one file from `tasks/` into your own assistant.

The sections below are the submission skeleton. Fill them in during the last five minutes. One person edits this file. Leave the headings as they are.

## Inspiration

Arctic sea ice already changes how long northern routes stay open, whether coastal communities keep winter ice, and how much the Greenland and Antarctic ice sheets add to sea level. We wanted those three consequences on one globe: the satellite record, then the five IPCC scenarios out to 2100, readable without a specialist tool. That is Track 3 of this hackathon: make live public Earth-observation data useful to people on the ground.

## What it does

Year slider 1979 → today → 2100. Scenario slider across SSP1-1.9, SSP1-2.6, SSP2-4.5, SSP3-7.0, and SSP5-8.5, blended between the stops. Observed years stay on the NSIDC record. Later years come from a per-cell fractional-logit fit. Cumulative CO2 is the intended regressor. The forecast currently runs on the IPCC AR6 warming fallback (`ipcc_ar6_warming_c_fallback`) because the IIASA SSP query returns HTTP 500.

The side panel reports Arctic shipping open-season length, communities losing winter ice, and ice-sheet sea level (~362 Gt per mm). With `AISSTREAM_API_KEY` set, live Arctic ships sit on the same globe.

A full run puts September 2050 Arctic extent at about 4.0M / 3.4M / 2.6M km² for SSP1-1.9 / SSP2-4.5 / SSP5-8.5. Northern September backtest skill is about 0.59. September extent stays above 1 million km² through 2100, including under SSP5-8.5.

## How we built it (tech stack)

A team of three worked at once. Each person drove their own Grok Bot from a brief in `tasks/`: backend, model, and frontend. `PLAN.md` and `API.md` were the contracts that let them work without colliding.

- Backend (`backend/`): FastAPI, live NSIDC Sea Ice Index feeds, and the `ships/` AIS layer (live positions from AISStream).
- Model (`model/ice_model.py`): a per-cell fractional-logit IRLS fit of sea-ice concentration against the climate regressor, a 40-draw bootstrap with seed 0, numba when it imports, and a numpy fallback.
- Frontend (`frontend/`): Vite + Three.js globe, scenario and year sliders, an impact panel, and the ships layer.

The API also checks NASA GIBS and Natural Earth. GRACE/GRACE-FO is read only when Earthdata credentials are present. The URLs that were checked are in `PLAN.md`.

## Challenges we ran into

Backend and model were merged without being run together. The store fetched only the latest concentration grid for each month, so a cell had fewer than 8 years and the fit produced zero slopes. Extent was the same on every scenario, while `/health` still said `model_ready`. The fix on main prefetches the full non-gap record and trusts `model.ice_model` only after a cell has 8 or more unmasked years. Until that fit exists, the API keeps a labeled linear-trend fallback.

A full fit took about 15.6 minutes in numpy and about 39 seconds with numba.

The IIASA timeseries route returns HTTP 500, so the live regressor stays `ipcc_ar6_warming_c_fallback`. This run has no Earthdata login, so sea level uses the labeled NASA Science rate fallback (−264 Gt/yr Greenland, −135 Gt/yr Antarctica).

On main, a real GRACE series is still extrapolated with a quadratic, and warming scales only the acceleration term. Greenland’s acceleration is positive, so a hotter scenario can turn sea level the wrong way. The warming-scaled mean loss rate that corrects it is open in pull request #12 and is not on main.

## Accomplishments

- The globe shows both poles, a year slider from 1979 to 2100, the five SSP stops, the impact panel, and optional live ships, from the NSIDC record rather than the dev fixtures.
- After the wiring fix, scenarios separate. September 2050 Arctic extent is about 4.0M / 3.4M / 2.6M km² for SSP1-1.9 / SSP2-4.5 / SSP5-8.5. Northern September backtest skill is about 0.59. There is no ice-free September by 2100 under SSP5-8.5; extent stays above 1 million km².
- December 1987 and January 1988 stay masked, with null extent. `GET /health` returns JSON when a feed is down.

## What we learned

The written contract in `API.md` let three assistants build separate folders at the same time. A merge that had never been run could still look healthy: `model_ready` has to mean a cell was actually fit. A fallback has to keep its own name, whether that name is the IPCC warming series or the NASA sea-level rate.

## What's next

Land the warming-scaled mean loss rate so a GRACE quadratic cannot bend a hotter scenario back toward a mass gain. Switch the forecast driver to cumulative CO2 when the IIASA query returns a pathway table.

Replace the ice-sheet extrapolation with [ISSM](https://issm.jpl.nasa.gov/) (Ice-sheet and Sea-level System Model): a physics-based ice-sheet model (higher-order / full-Stokes flow, surface mass balance, ocean melt at the grounding line) forced by the same SSP temperature pathways, instead of a GRACE trend with an acceleration term scaled by warming.

## How Grok Bot helped build it

Each teammate’s Grok Bot took a task brief from `tasks/`, built that folder, and opened pull requests. A routine reviewed teammates’ pull requests against `API.md` on every new PR. A smoke test of the whole stack caught the zero-slope wiring bug. A Grok Bot wrote this README from the repo and those results.

Session exports belong in `sessions/`. TODO: each teammate adds their export with personal information removed.

## How to run it

Python 3.11 or newer. The dependency files are already in `model/`, `backend/`, `frontend/`, and `ships/`.

```bash
# Model tests, no network
cd model
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
PYTHONPATH=.. .venv/bin/pytest

# API on port 8000. Run this from backend/ so `app` imports.
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r ../ships/requirements.txt
PYTHONPATH=.. .venv/bin/uvicorn app.main:app --reload --port 8000

# Globe on port 5173
cd frontend
npm install
# Fixtures, until the API has finished fitting
VITE_USE_FIXTURES=true npm run dev
# Live API (VITE_API_BASE defaults to http://127.0.0.1:8000)
VITE_USE_FIXTURES=false VITE_API_BASE=http://127.0.0.1:8000 npm run dev

# Ships unit tests, no network and no API key
cd ships
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd ..
PYTHONPATH=. ships/.venv/bin/pytest ships/tests
```

The first API start downloads the full NSIDC monthly record and fits the model. That takes several minutes. Wait for the log line `IceModel.update finished` before opening the globe.

`GET http://127.0.0.1:8000/health` returns JSON even when a feed is down.

`AISSTREAM_API_KEY` enables live ships. Set it in the shell or in the repo-root `.env` (gitignored; importing `ships` loads that file). Without the key, `/ships` is an empty list and `/ships/health` is `down`. The globe still loads.

Earthdata credentials are optional. Put `EARTHDATA_TOKEN`, or `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD`, in `backend/.env`. That file is gitignored. Without it the ice-sheet path uses the labeled NASA Science rate fallback described in `API.md`.
