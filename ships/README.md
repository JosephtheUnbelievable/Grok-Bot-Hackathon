# ships/

Role 4. Branch `feat/ships`. You are the only person who writes here.

Live Arctic ship markers from the AISStream.io websocket, filtered to about 60 N–90 N. The process keeps the latest position and a short track per MMSI, samples the current NSIDC concentration at the ship and along the next 6–12 hours of travel, and flags heavy ice. The backend mounts it with one hook. Two routes: `GET /ships` and `GET /ships/health`.

Agreed layout (you create it):

```
ships/
  __init__.py
  router.py            # install(app, concentration_at=...), APIRouter, standalone app
  worker.py            # websocket, cleaning, reconnect
  ice.py               # heading projection and ice_risk
  requirements.txt
  testdata/ais_sample.jsonl   # recorded sample, unit tests only
  tests/test_ships.py
```

Run the tests from the repo root with `PYTHONPATH=.`. Run the standalone app on port **8099** so it does not take Role 1’s port 8000. `AISSTREAM_API_KEY` comes from the environment. The empty placeholder is in `.env.example`. A real key stays out of git.

`testdata/ais_sample.jsonl` is a recorded sample for `ships/tests` only. The helper that loads it sets `fixture: true` and `source: "recorded-sample"`. The HTTP routes must not serve that file and must report `using_sample: false`.

Read `tasks/4-ships.md` and the **Task 4: Ships** section of `API.md`. Do not edit `backend/`, `model/`, `frontend/`, `fixtures/`, `tasks/`, `PLAN.md`, `API.md`, or the root `README.md`.

## Contract change requests

_Empty unless the checkpoint agrees a change. Do not edit API.md yourself._
