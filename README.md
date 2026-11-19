# Grok-Bot-Hackathon

A 3D globe of sea ice at both poles and the Greenland and Antarctic ice sheets, from the satellite record into five climate scenarios. Built for the Berkeley × Stanford Grok Bot Hackathon, Track 3.

The MIT license is in `LICENSE` (Copyright 2026 Joseph Lin).

How the three people work without colliding is in `PLAN.md`. The HTTP and Python contract is in `API.md`. Paste one file from `tasks/` into your own assistant.

The sections below are the submission skeleton. Fill them in during the last five minutes. One person edits this file. Leave the headings as they are.

## Inspiration

_Fill in during the last 5 minutes._

## What it does

_Fill in during the last 5 minutes._

Year slider 1979 → today → 2100. Scenario slider across SSP1-1.9, SSP1-2.6, SSP2-4.5, SSP3-7.0, and SSP5-8.5, blended between the stops. Observed years stay on the NSIDC record. Later years come from a per-cell logistic fit against cumulative CO2. The side panel reports Arctic shipping open-season length, communities losing winter ice, and ice-sheet sea level (~362 Gt per mm).

## How we built it (tech stack)

_Fill in during the last 5 minutes._

Agreed stack: Python FastAPI in `backend/`, the forecast in `model/`, Vite + Three.js in `frontend/`. Live inputs: NSIDC Sea Ice Index G02135, NASA GIBS, GRACE/GRACE-FO (Earthdata login, with a labeled public fallback), IIASA SSP pathways (IPCC warming anchors if that query fails), Natural Earth coastlines. Details and which URLs were checked are in `PLAN.md`.

## Challenges we ran into

_Fill in during the last 5 minutes._

## Accomplishments

_Fill in during the last 5 minutes._

## What we learned

_Fill in during the last 5 minutes._

## What's next

_Fill in during the last 5 minutes._

Replace the ice-sheet extrapolation with [ISSM](https://issm.jpl.nasa.gov/) (Ice-sheet and Sea-level System Model): a physics-based ice-sheet model (higher-order / full-Stokes flow, surface mass balance, ocean melt at the grounding line) forced by the same SSP temperature pathways, instead of a GRACE trend with an acceleration term scaled by warming.

## How Grok Bot helped build it

_Fill in during the last 5 minutes. Export each assistant session and point at it from here._

## How to run it

_The three roles add real dependency files inside their own folders. Commands below are the agreed interface; they will fail until those files exist._

```bash
# Role 2 — model tests, no network
cd model
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
PYTHONPATH=.. .venv/bin/pytest

# Role 1 — API on port 8000
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=.. .venv/bin/uvicorn app.main:app --reload --port 8000

# Role 3 — globe on port 5173
cd frontend
npm install
# build against fixtures until the API is up
VITE_USE_FIXTURES=true npm run dev
# then
VITE_USE_FIXTURES=false VITE_API_BASE=http://127.0.0.1:8000 npm run dev
```

`GET http://127.0.0.1:8000/health` should return JSON even when a feed is down.

Earthdata credentials, if you have them, go in `backend/.env` (`EARTHDATA_USERNAME`, `EARTHDATA_PASSWORD`). That file is gitignored. Without it the ice-sheet path uses the labeled NASA Science rate fallback described in `API.md`.
