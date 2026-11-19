# model/

Role 2. Branch `feat/model`. You are the only person who writes here.

Pure Python package. No HTTP, no sockets, no reads outside this folder. The backend constructs a `TrainingBundle` and calls `IceModel` as specified in `API.md` under “Model module”.

Agreed layout (you create it):

```
model/
  __init__.py          # re-export IceModel
  ice_model.py         # dataclasses + IceModel
  pyproject.toml       # pytest extra
  tests/test_ice_model.py
```

Import path: `model.ice_model`, repository root on `PYTHONPATH`.

Read `tasks/2-forecast-model.md`. Fit per calendar month and per cell against cumulative CO2 (or the labeled warming fallback the bundle already selected). Train-on-or-before-2010 backtest is a second fit and must not clobber the main one.

Do not edit `backend/`, `frontend/`, `fixtures/`, `tasks/`, `PLAN.md`, `API.md`, or the root `README.md`.

## Contract change requests

_Empty unless the checkpoint agrees a change. Do not edit API.md yourself._
