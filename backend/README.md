# backend/

Role 1. Branch `feat/backend`. You are the only person who writes here.

FastAPI process on port 8000. It downloads and caches the public feeds, masks sentinels, calls `model.ice_model.IceModel`, and serves the routes in `API.md`.

Agreed layout (you create it):

```
backend/
  app/main.py          # object name: app
  app/feeds.py         # fetch, cache, clean
  requirements.txt
  .env                 # gitignored; EARTHDATA_USERNAME / EARTHDATA_PASSWORD
  cache/               # gitignored last-good copies
```

Run from this folder with `PYTHONPATH=..` so `import model.ice_model` works once Role 2 has landed. Until then, catch `ImportError` and keep `/health` plus observed-only responses alive (`model_ready: false`, forecast grids masked, `source: "model-not-ready"`).

Read `tasks/1-backend-data.md` and `API.md`. Do not edit `model/`, `frontend/`, `fixtures/`, `tasks/`, `PLAN.md`, `API.md`, or the root `README.md`.

## Contract change requests

_Empty unless the checkpoint agrees a change. Do not edit API.md yourself._
