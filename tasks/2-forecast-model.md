# Role 2 — Forecast model

You are one of three people in a ~40-minute hackathon. Build only the model package. Two other assistants own `backend/` and `frontend/`. Stay inside `model/` or you will collide.

## What you build

`model/ice_model.py`, importable as `model.ice_model` when the repo root is on `PYTHONPATH`.

The scientific core:

- For each pole, each calendar month, and each grid cell, a logistic curve of ice concentration against **cumulative CO2** (the bundle’s regressor), not against the calendar year.
- Bootstrap uncertainty (40 resamples, seed 0, percentiles 10 / 50 / 90).
- A backtest that trains on years `<= 2010` and scores 2011 through the last observed September, without replacing the main fit.
- Ice-sheet mass: GRACE trend plus an acceleration term, the acceleration scaled by scenario warming after the record ends, converted at **362 Gt ice per mm** of sea level.
- Impact numbers the panel needs: a shipping-season proxy, the first September the Arctic extent drops below 1 million km², and a winter ice-loss index.

You do not download anything. The backend passes a `TrainingBundle`. You do not open sockets, and you do not read `backend/cache/`.

## Folder ownership

- Branch: `feat/model`
- You write: `model/**` only.
- You do not write: `backend/`, `frontend/`, `fixtures/`, `tasks/`, `PLAN.md`, `API.md`, root `README.md`, `LICENSE`, `.gitignore`.
- The dataclasses, method names, and formulas in `API.md` (“Model module”) are the contract. Copy them. If you need a different signature, stop and write the proposal in `model/README.md` under `Contract change requests`. Do not edit `API.md` during the sprint.

Agreed files:

```
model/__init__.py          # from model.ice_model import IceModel
model/ice_model.py
model/pyproject.toml       # requires-python >= 3.11, optional extra "dev" with pytest
model/tests/test_ice_model.py
```

## Contract you implement

Class `IceModel` with:

- `update(self, bundle: TrainingBundle) -> None`
- `concentration(self, req: GridRequest) -> ConcentrationGrid`
- `extent_series(self, pole, month, scenario, blend) -> list[ExtentPoint]` covering years 1979 through 2100 inclusive
- `impact(self, year, scenario, blend) -> ImpactResult`
- `backtest(self) -> BacktestResult`

Behaviour the backend relies on:

- `update` on an empty grid list does not raise. Later `concentration` returns a masked grid (`values_u8` all 255, `extent_km2 is None`, `source="model-not-ready"`).
- For a year-month that exists in `bundle.grids` and is not `gap_masked`, `concentration` returns those satellite bytes for **every** band. `is_forecast` is false. `source` is `nsidc-g02135`.
- A `gap_masked` grid stays all 255 with `extent_km2 is None`.
- A later year uses the logistic forecast. `source` is `model-forecast`. `is_forecast` is true.
- `backtest` builds a second fit on `year <= 2010` and must leave `concentration` results unchanged.
- `blend is None` means the named scenario. Otherwise `blend` is a float in `[0, 4]` mapping `ssp119, ssp126, ssp245, ssp370, ssp585`. Interpolate the regressor linearly between the two neighbouring pathways (formula in `API.md`). Do not interpolate the ice grids in time; interpolate the driver, then evaluate the curve.

### Logistic

Per pole, per month, per cell with at least 8 unmasked training years:

- `y = clip(percent / 100, 0.001, 0.999)`
- `x` = that year’s regressor, centered by the training-set mean
- Fractional logit, IRLS is enough: `p = 1 / (1 + exp(-(a + b * x)))`
- Under 8 years: training mean, slope 0
- Always-masked cells stay 255
- Bootstrap over years, 40 draws, `numpy` Generator seed 0. `low` / `median` / `high` = percentiles 10 / 50 / 90 of `round(100 * p)` clipped to 0–100

Regressor choice is already made by the backend:

- `bundle.regressor == "cumulative_co2_gt"` → `cumulative_co2_gt_by_year` for historical years, and `ssp_cumulative_co2_gt` (blended) for forecast years. Forecast cumulative values are absolute Gt, already cumulated by the backend. Use them as `x`. If a forecast year is missing from the dict, hold the last known value and do not switch units.
- `bundle.regressor == "ipcc_ar6_warming_c_fallback"` → `x` is `ssp_warming_c`, in °C. Do not add a fake CO2 series.

### Extent

Observed points in `extent_series` copy `MonthlyGrid.extent_km2` and use `extent_method="nsidc_monthly_csv"`. `low_km2` and `high_km2` are `None`.

Forecast extent: cells `>= 15` count as ice (pole hole is already stored as 100). Area is `bundle.cell_area_km2[pole]` row-major, or **625 km²** when that map is missing. Multiply the sum by one scale so, for this pole and month, the mean modeled extent over years 2000–2010 equals the mean CSV extent over the same years (ignore nulls). That factor is `extent_scale`. Method string `model_cell_sum_scaled`. If there is nothing to scale against, use scale 1.0.

### Ice sheets

Real points: least squares `M(t) = c + r (t - t0) + 0.5 a (t - t0)^2` with `t0` the mean decimal year. `mass_gt` is an anomaly, negative means loss.

After the last sample, `scale = max(W(Y) / W(Y_last), 0)` and the acceleration term is multiplied by `scale`. Inside the record, use the observed anomaly (nearest sample), unscaled.

```
mm = -(M(Y) - M_first) / 362
```

`low_mm` and `high_mm` are p10 and p90 of the **sum** of both ice sheets across 40 residual bootstraps, seed 0. `total_mm = greenland_mm + antarctica_mm` on the median fit.

No GRACE points: rates −264 Gt/yr (Greenland) and −135 Gt/yr (Antarctica) from year 2002, acceleration 0, `sea_level_source="nasa-science-summary-fallback"`. After 2025, multiply the rate by warming(Y) / warming(2025) when warming is present. These rates are the NASA Science summary, not a measurement you took.

### Impact proxies

Hard-code the boxes in `model/` (drop masked cells):

- Northern Sea Route: lat 66–80 N, lon 30–180 E
- Northwest Passage: lat 68–75 N, lon 140–60 W

A month is open when the mean concentration in the box is `< 15`. `open_season_days` uses 30 days per open month and 28 for February. `opening_date` is `YYYY-MM-01` of the first open month **on or after June**, or `None`. This is a proxy. The definition strings are added by the backend from `API.md`; you return the numbers.

`ice_free_september_year`: first year 1979–2100 whose September **north** median `extent_km2` is not None and `< 1_000_000`, else None.

`winter_ice_loss_index`: among unmasked cells at lat ≥ 60 N whose 1981–2010 February mean was `>= 15`, the fraction whose February value in the requested year is `< 15`. `None` if that climatology has no cells. You need cell latitudes. Derive them once from grid width, height, and the polar stereographic of the bundle’s grids if the backend did not pass coordinates: assume the NSIDC 25 km north grid only when `width, height` match the first grid you actually received, and skip the index (`None`) rather than apply a wrong georeference. A simple accepted approach: store lat/lon of each cell while `update` runs by reading nothing extra — if you cannot place cells, return `None` for the index and still return shipping seasons as 0 / `None` rather than raising. Prefer computing lat/lon from the grid’s known NSIDC north dimensions when they match 304×448, using the standard 25 km polar stereographic. Document the assumption in a module comment.

Cell centres for a non-matching fixture-sized grid (tests will use tiny grids): accept an optional attribute. To keep the published dataclass frozen and unchanged, derive a test-only path inside `model/tests` by building grids whose values you fully control and assert extent and sea level, and assert the winter index is `None` on a grid that has no geographic metadata. Do not change `TrainingBundle` to add lat/lon during the sprint unless you file a contract request. Shipping seasons may also return zeros on a grid with no coordinates; that is acceptable for the unit tests. The backend’s real grids are full NSIDC size; special-case `height == 448 and width == 304` for the north geographic boxes, and the south grid does not feed the shipping proxy (shipping is Arctic-only).

## How to test alone

No network. Synthetic bytes only.

```bash
cd model
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cd ..
PYTHONPATH=. model/.venv/bin/pytest model/tests
```

Minimum tests:

1. A cell that goes from 90% to 10% as the regressor rises gets a negative slope, and a higher regressor predicts a lower concentration.
2. An observed year-month is returned byte-for-byte for `band="low"` and `band="high"`.
3. A gap grid stays masked and `extent_km2 is None`.
4. `backtest().train_end_year == 2010`, and calling `backtest` does not change a later year’s `concentration` median bytes.
5. Two mass points with a negative rate produce positive sea level, and `-1000 Gt` is about `1000 / 362` mm above the first sample.
6. An empty `update` does not raise; `concentration(...).source == "model-not-ready"`.
7. `extent_series` for a month returns 122 points (1979 through 2100).

`numpy` is the only runtime dependency you need. Do not add sklearn unless IRLS is somehow slower than the rest of the hour; it will not be.

## Acceptance criteria

- `pytest` passes with the network off.
- Public names match `API.md` so Role 1 can import them without an adapter.
- Historical months are the satellite grid, not the curve.
- The driver is the bundle’s regressor, never the integer year as `x`.
- Bootstrap is seeded (same bundle → same `low` and `high`).
- `git status` shows changes only under `model/`.

## Minute 20

Push `feat/model`. You merge first. Rebase onto `main`, merge, then Role 1 rebases and imports you. Do not wait for the globe.
