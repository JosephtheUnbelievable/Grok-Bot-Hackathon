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

## Assumptions

The dataclasses and method names match `API.md`. These are the choices made where the contract left the mechanics open:

- Forecast concentration `low` / `median` / `high` are the 10th / 50th / 90th percentiles of `round(100 * p)` across 40 year-resamples (`numpy` Generator seed 0), then rounded to a byte. `extent_km2` on a forecast grid is the scaled cell-sum of **that band's grid**. On `extent_series`, `extent_km2` matches the median grid; `low_km2` / `high_km2` are the 10th / 90th percentiles of the 40 bootstrap cell-sums after the same scale.
- The scale is one number per pole per month: mean CSV extent over 2000–2010 (nulls ignored) divided by the mean unscaled median-grid cell-sum over those years. It is 1.0 when there is nothing to scale against. Observed months report `extent_scale` 1.0 because they return the CSV extent.
- A year present in `cumulative_co2_gt_by_year` uses that value, including backtest years after 2010. Later years use the blended SSP cumulative. A missing year holds the latest known value at or before it. The warming fallback fits on the mean of the pathways that provide each training year (they are one historical path) and predicts with the blended pathway.
- Backtest September scores use scenario `ssp245` only as the warming-fallback lookup. The scored regressor for cumulative CO2 is the historical series.
- Bootstrap random draws are taken for north then south, months 1–12, and only for a month that has at least one cell with 8 unmasked years.
- With no GRACE samples, mass starts at 0 in 2002 and accumulates at −264 Gt/yr (Greenland) and −135 Gt/yr (Antarctica). Through 2025 the rate is constant. After 2025, `M = rate * (2025 - 2002) + rate * max(W(year) / W(2025), 0) * (year - 2025)`. `uncertainty_gt` is not used; the interval is an empirical residual bootstrap. If only one sheet has samples, the other uses the NASA rate and `sea_level_source` stays `grace-gracefo`.
- Inside the GRACE record the reported millimetres use the nearest sample (an exact time tie keeps the earlier one). After the last sample the sheet keeps the mission-mean loss rate, `(mass_last - mass_first) / (t_last - t_first)` when that rate is negative, else zero. Each later year multiplies that rate by `max(W(year) / W(last), 0)`. The quadratic acceleration is fit and stored but not extrapolated: on real mascons it is often positive, and scaling that term by warming made hotter scenarios fall. `low_mm` / `high_mm` are the residual-bootstrap p10 / p90 of the same projection, widened if needed so they contain `total_mm`.
- An empty grid list (or a request before `update`) returns a 1×1 grid of 255 with source `model-not-ready`. Shipping open days interpolate the monthly corridor mean to each calendar day, including 29 February in leap years. Town ice (Utqiagvik, Kivalina, Shishmaref, Tuktoyaktuk) is the mean of unmasked cells within 100 km on the 304×448 north grid.
- North lat/lon is computed only for a 304×448 grid, using the NSIDC 25 km definition documented in `ice_model.py`. Other shapes leave the winter index `None` and both shipping seasons at 0 / `None`.
