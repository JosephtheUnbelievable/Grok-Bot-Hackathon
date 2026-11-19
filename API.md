# API contract

Source of truth for the hackathon sprint. Roles implement this document as written. Sample bodies in `fixtures/` match the JSON shapes and are **dev fixtures**, not observations and not forecasts.

- Backend base URL: `http://127.0.0.1:8000`
- Frontend dev server: `http://127.0.0.1:5173`
- Browser calls the API directly. Backend sends `Access-Control-Allow-Origin: *` for local dev.
- No authentication on our API.
- Times are UTC ISO-8601 (`2026-11-19T23:00:00Z`).
- Years run from **1979** through **2100** inclusive. Months are integers **1–12**.

## Shared enums

| Name | Values |
| --- | --- |
| `pole` | `north`, `south` |
| `scenario` | `ssp119`, `ssp126`, `ssp245`, `ssp370`, `ssp585` |
| `band` | `median`, `low`, `high` |
| `format` | `png`, `json` |
| feed `status` | `ok`, `stale`, `down` |
| overall `status` | `ok`, `degraded`, `down` |

Canonical scenario ids are the short form. Also accept `ssp1-1.9`, `SSP1-1.9`, `ssp2-4.5`, and the same pattern for the other three, and normalize to the canonical id in every response.

`blend` is a float from **0 to 4** inclusive. Index 0 is `ssp119`, 1 is `ssp126`, 2 is `ssp245`, 3 is `ssp370`, 4 is `ssp585`. Let `i = floor(blend)` and `f = blend - i`. For a year-valued pathway `P`:

```
value(year) = (1 - f) * P[i](year) + f * P[min(i + 1, 4)](year)
```

When `blend` is present it wins over `scenario`. When it is absent, the named scenario is a whole number on that same line (`ssp245` means `blend` 2). Echo both fields: `scenario` is the canonical id of the left-hand pathway when `f == 0`, otherwise the string `blend`, and `blend` is the float or `null` if the client did not send it.

`low` / `high` are the 10th and 90th percentiles of a 40-draw bootstrap (`seed = 0`). `median` is the 50th. On observed year-months the three bands are the satellite grid; the bootstrap applies to forecast years.

## Regressor

The fit is ice concentration against **cumulative CO2**, not against calendar year.

- Preferred historical series: running sum of Global Carbon Budget annual CO2 (fossil + land use) from 1850, in **GtCO2**.
- Preferred future series: that cumulative, plus the cumulated marker-scenario World `Emissions|CO2` from IIASA, blended with `blend`.
- If those numbers are unavailable, use IPCC AR6 WG1 Table SPM.1 best-estimate warming (°C vs 1850–1900), linearly interpolated between the near-term anchor (2021–2040 = 1.5 °C for every SSP) and the 2081–2100 anchors (1.4, 1.8, 2.7, 3.6, 4.4). Set `regressor` to `ipcc_ar6_warming_c_fallback`.
- When the real cumulative series is in use, `regressor` is `cumulative_co2_gt`.
- Never report one series under the other series’ name.

`observed_through` is the latest year-month whose NSIDC monthly extent is present and is not the `-9999` sentinel. The frontend uses this, not the wall clock, to label “today”. A year-month after `observed_through` is a forecast (`is_forecast: true`).

## Error shape

All errors are JSON, including when the client asked for `format=png`.

```json
{
  "error": {
    "code": "invalid_parameter",
    "message": "year must be an integer from 1979 to 2100",
    "param": "year"
  }
}
```

| HTTP | `code` | When |
| --- | --- | --- |
| 400 | `invalid_parameter` | Bad year, month, scenario, blend, band, or format. `param` is the query or path field. |
| 404 | `not_found` | `pole` is not `north` or `south`, or the path does not exist. `param` is `pole` or `path`. |
| 503 | `feed_unavailable` | This response cannot be built: no observation, no fitted model, and no cached copy. |

A stale cache is **HTTP 200**, not 503. `stale: true` and `source` ending in `-cache` carry the meaning. The UI must keep working.

## Stale and missing

| Signal | Where |
| --- | --- |
| Per-feed `status` | `GET /health` |
| `stale` boolean | every success JSON |
| `X-Ice-Stale: true` or `false` | `GET /ice/...` response header |
| `gap_masked: true` | 1987-12 and 1988-01, and any daily date from 1987-12-03 through 1988-01-12 |
| `extent_km2: null` | gap or no grid. This is not zero ice. |
| `using_cache: true` | health feed object, when bytes came from `backend/cache/` |
| `source` | `nsidc-g02135`, `nsidc-g02135-cache`, `model-forecast`, `model-forecast-cache`, `model-not-ready`, `nasa-science-summary-fallback` |

Freshness windows used to set a feed to `stale` even after a successful download:

| Feed id | Stale when |
| --- | --- |
| `nsidc_daily_extent` | Newest daily date is more than 5 days behind today (UTC) |
| `nsidc_monthly_extent` | Newest real month is more than 45 days behind today |
| `nsidc_monthly_concentration` | Same 45-day rule, tracked separately because the GeoTIFF tree can lag the CSV |
| `gibs` | Service down, or the advertised AMSR2 end date is more than 14 days behind today. A successful fetch of a historical day still counts as the service being up. |
| `grace_icesheet` | Last sample older than 180 days, or the constant-rate fallback is in use |
| `ssp_pathways` | IIASA query failed and the warming-anchor fallback is in use |
| `natural_earth` | HEAD failed and a cached zip is being served. A successful fetch of the static zip is `ok` regardless of `Last-Modified`. |

Overall `/health` status:

- `ok` — every feed is `ok`
- `degraded` — at least one feed is `stale` or `down`, and `/ice` can still answer from cache, observations, or the model
- `down` — `/ice` cannot answer. HTTP status of `/health` is still 200.

Sentinel mapping before the values leave the server. These inputs never appear in JSON or in the PNG:

| Input | Meaning | API uint8 in the JSON grid |
| --- | --- | --- |
| GeoTIFF `0–1000` | percent × 10 | `round(value / 10)` clipped to `0–100`. Store `1–14` as computed; clients treat `< 15` as open water for extent. |
| GeoTIFF `2510` | Arctic pole hole | `100` (NSIDC assumes this hole is ice when it computes extent). Count those cells in `pole_hole_cells`. |
| GeoTIFF `2530` coast, `2540` land, `2550` missing | not ice | `255` masked |
| Monthly CSV `-9999`, or a missing file for the 1987-12 / 1988-01 gap | instrument gap | entire grid `255`, `extent_km2: null`, `gap_masked: true` |
| Any other value outside `0–100` after scaling | bad cell | `255` |

## `GET /health`

`GET /health`

```json
{
  "status": "degraded",
  "fixture": false,
  "generated_at": "2026-11-19T23:00:00Z",
  "observed_through": { "year": 2026, "month": 10 },
  "regressor": "cumulative_co2_gt",
  "model_ready": true,
  "feeds": {
    "nsidc_daily_extent": {
      "status": "ok",
      "last_updated": "2026-10-18T00:00:00Z",
      "last_success": "2026-11-19T22:50:00Z",
      "age_seconds": 2800000,
      "stale": false,
      "using_cache": false,
      "detail": "N_seaice_extent_daily_v4.0.csv"
    },
    "nsidc_monthly_extent": {},
    "nsidc_monthly_concentration": {},
    "gibs": {},
    "grace_icesheet": {},
    "ssp_pathways": {},
    "natural_earth": {}
  }
}
```

Every feed object has the same keys as `nsidc_daily_extent`. `last_updated` is the data’s own time (newest observation, or the zip’s `Last-Modified`). `last_success` is when our process last stored good bytes. Both are `null` when the feed has never succeeded. `age_seconds` is `null` in that same case. `detail` is a short human string (file name, HTTP status, or `earthdata-login-required`).

`model_ready` is false until the first fit finishes. The process must still answer `/health`.

## `GET /ice/{pole}/{year}/{month}`

Query:

| Param | Default | Notes |
| --- | --- | --- |
| `scenario` | `ssp245` | Ignored for the pixel values of an observed month. Still echoed. |
| `blend` | absent | Overrides `scenario` when present. |
| `band` | `median` | |
| `format` | `png` | `png` or `json` |

### `format=png`

- `Content-Type: image/png`
- Equirectangular overlay, **1024 × 512**, north at the top, longitude −180 at the left edge, matching the GIBS WMS `BBOX=-90,-180,90,180` layout verified in `PLAN.md`.
- RGBA. Masked cells and open water (`< 15%`) are alpha 0 so the basemap shows through.
- Ice from 15% to 100% is opaque, colour-mixed from `#5dade2` at 15% to `#ffffff` at 100%.
- A gap month is a fully transparent PNG, HTTP 200, with `X-Ice-Gap-Masked: true` and an empty `X-Ice-Extent-Km2`.

Response headers (also on the JSON response):

| Header | Example |
| --- | --- |
| `X-Ice-Pole` | `north` |
| `X-Ice-Year` | `2024` |
| `X-Ice-Month` | `9` |
| `X-Ice-Scenario` | `ssp245` |
| `X-Ice-Blend` | empty if null, otherwise the float |
| `X-Ice-Band` | `median` |
| `X-Ice-Is-Forecast` | `true` or `false` |
| `X-Ice-Source` | `nsidc-g02135` |
| `X-Ice-Stale` | `true` or `false` |
| `X-Ice-Gap-Masked` | `true` or `false` |
| `X-Ice-Extent-Km2` | kilometres squared, or empty if null |
| `X-Ice-Extent-Method` | `nsidc_monthly_csv` or `model_cell_sum_scaled` |
| `X-Ice-Regressor` | `cumulative_co2_gt` or `ipcc_ar6_warming_c_fallback` |
| `X-Ice-Image-Layout` | `equirectangular-1024x512` |
| `X-Ice-Fixture` | `false` on the live API |
| `X-Ice-Observed-Through` | `2026-10` |
| `Cache-Control` | `public, max-age=300` when `stale` is false; `no-cache` when `stale` is true |

### `format=json`

Native polar-stereographic grid from the NSIDC GeoTIFF (not the 1024×512 warp). `width` and `height` come from the file. `values_b64` is standard base64 of `width * height` bytes, row-major, **upper-left first**, no compression. `255` means masked. `0–100` means concentration in percent.

```json
{
  "pole": "north",
  "year": 2024,
  "month": 9,
  "scenario": "ssp245",
  "blend": null,
  "band": "median",
  "fixture": false,
  "generated_at": "2026-11-19T23:00:00Z",
  "is_forecast": false,
  "source": "nsidc-g02135",
  "stale": false,
  "gap_masked": false,
  "regressor": "cumulative_co2_gt",
  "observed_through": { "year": 2026, "month": 10 },
  "extent_km2": 4280000,
  "area_km2": 2910000,
  "extent_method": "nsidc_monthly_csv",
  "extent_scale": 1.0,
  "pole_hole_cells": 0,
  "units": {
    "concentration": "percent",
    "extent": "km2",
    "area": "km2"
  },
  "grid": {
    "projection": "EPSG:3413",
    "nominal_resolution_m": 25000,
    "width": 304,
    "height": 448,
    "origin": "upper-left",
    "encoding": "row-major-uint8",
    "masked_value": 255
  },
  "values_b64": "<base64>"
}
```

`area_km2` is the concentration-weighted area from the monthly CSV when that row is real, otherwise `null` on forecasts (the hackathon does not invent an area product). `extent_km2` on observed months is the CSV extent converted to km² (`csv_value * 1_000_000`). The CSV column is **millions of km²**. Do this conversion once.

`extent_method` `model_cell_sum_scaled` means: count cells with value `>= 15` (pole-hole cells already stored as 100), multiply by cell area (the NSIDC cell-area grid when the backend has it, otherwise **625 km²**), then multiply by one scale factor per pole per month so the mean modeled extent over 2000–2010 matches the mean CSV extent. `extent_scale` is that factor (1.0 when the CSV itself was used).

`projection` is the GeoTIFF CRS. The user guide says EPSG:3411 / EPSG:3412; a file may say EPSG:3413 / EPSG:3031. Report the file.

## `GET /extent/{pole}`

Query: `scenario` (default `ssp245`), `blend` (optional), `month` (default **9** for `north` and **2** for `south`, the usual annual minimum).

One point per year from 1979 through 2100 for that calendar month. Historical `low_km2` and `high_km2` are `null`. Forecast bands are the bootstrap extent after the same cell-sum scaling. Gap years stay in the array with null extent so the chart can break the line.

```json
{
  "pole": "north",
  "month": 9,
  "scenario": "ssp245",
  "blend": null,
  "band_note": "low is p10 and high is p90 of bootstrap extent; null on observed years",
  "fixture": false,
  "generated_at": "2026-11-19T23:00:00Z",
  "stale": false,
  "regressor": "cumulative_co2_gt",
  "observed_through": { "year": 2026, "month": 10 },
  "units": { "extent": "km2" },
  "climatology_1981_2010_km2": 6200000,
  "series": [
    {
      "year": 1979,
      "extent_km2": 7050000,
      "low_km2": null,
      "high_km2": null,
      "is_forecast": false,
      "gap_masked": false,
      "source": "nsidc-g02135",
      "extent_method": "nsidc_monthly_csv"
    }
  ]
}
```

`climatology_1981_2010_km2` is the mean of non-null extents in that month over 1981–2010, or `null` if fewer than 20 years exist. It is a single number, not a band.

## `GET /impact`

Query: `year` (required), `scenario` (default `ssp245`), `blend` (optional).

```json
{
  "year": 2050,
  "scenario": "ssp245",
  "blend": null,
  "fixture": false,
  "generated_at": "2026-11-19T23:00:00Z",
  "stale": false,
  "is_forecast": true,
  "regressor": "cumulative_co2_gt",
  "observed_through": { "year": 2026, "month": 10 },
  "shipping": {
    "northern_sea_route": {
      "open_season_days": 120,
      "opening_date": "2050-07-01",
      "open_month_count": 4,
      "definition": "Proxy, not a route solver. A month is open when the mean concentration of unmasked cells in lat 66–80 N, lon 30–180 E is below 15%. Days = 30 per open month, 28 in February. opening_date is the first day of the first open month on or after June, else null when no month from June onward is open."
    },
    "northwest_passage": {
      "open_season_days": 60,
      "opening_date": "2050-08-01",
      "open_month_count": 2,
      "definition": "Same proxy for lat 68–75 N, lon 140 W–60 W."
    }
  },
  "communities": {
    "ice_free_september_year": 2052,
    "winter_ice_loss_index": 0.42,
    "definitions": {
      "ice_free_september_year": "First year in 1979–2100 whose September northern extent_km2 is below 1000000 under this scenario's median band. null if none.",
      "winter_ice_loss_index": "Share of unmasked cells at lat >= 60 N whose 1981–2010 February mean was >= 15% and whose February concentration in the selected year is < 15%. Range 0–1. null if that climatology cannot be built."
    }
  },
  "sea_level": {
    "greenland_mm": 82.0,
    "antarctica_mm": 41.0,
    "total_mm": 123.0,
    "low_mm": 90.0,
    "high_mm": 160.0,
    "conversion_gt_per_mm": 362,
    "baseline": "Mass anomaly relative to the first GRACE sample, converted as mm = -mass_gt / 362. total_mm = greenland_mm + antarctica_mm. low_mm and high_mm are totals.",
    "source": "grace-gracefo"
  }
}
```

`sea_level.source` is `grace-gracefo` or `nasa-science-summary-fallback`. Fallback rates: Greenland **−264 Gt/yr**, Antarctica **−135 Gt/yr**, applied from 2002, then for years after the last real sample multiplied by `warming(year) / warming(last real year)` when a warming pathway exists. Acceleration scaling for a real GRACE fit is specified in the model interface below.

`opening_date` is `YYYY-MM-DD` or `null`. `open_season_days` is an integer, 0 when the season does not open. The definitions must be copied into the response so the panel can show them.

Shipping and the winter index for a year after `observed_through` use the median forecast grids (or `band` is not a parameter here; impact always uses the median ice, while sea-level `low_mm` / `high_mm` still span the mass uncertainty).

## `GET /backtest`

No query parameters. Train on observations with `year <= 2010`. Score months with `year >= 2011` up to `observed_through`. Do not fit on the evaluation years. The primary model used by `/ice` may train on all observed years; this endpoint builds its own fit.

```json
{
  "fixture": false,
  "generated_at": "2026-11-19T23:00:00Z",
  "stale": false,
  "trained": true,
  "train_end_year": 2010,
  "eval_start_year": 2011,
  "eval_end_year": 2026,
  "regressor": "cumulative_co2_gt",
  "metric": "rmse of scaled modeled September extent against the NSIDC monthly CSV",
  "by_pole": {
    "north": {
      "rmse_km2": 0,
      "bias_km2": 0,
      "n_months": 0,
      "skill_vs_climatology": 0
    },
    "south": {
      "rmse_km2": 0,
      "bias_km2": 0,
      "n_months": 0,
      "skill_vs_climatology": 0
    }
  }
}
```

`bias_km2` is mean(model − observed). `skill_vs_climatology` is `1 - rmse_model / rmse_climatology` where the climatology forecast is the 1981–2010 mean of that calendar month repeated. `n_months` counts September rows in the evaluation window with a non-null CSV extent (the scored series is September, one number per year). If there is nothing to fit, return HTTP 200 with `trained: false` and zeros, and `/health` `model_ready: false`.

## Model module

Path: `model/ice_model.py`. Importable as `model.ice_model` when the repository root is on `PYTHONPATH`.

```bash
cd backend
PYTHONPATH=.. uvicorn app.main:app --reload --port 8000
```

The model does not open network or files outside `model/`. The backend parses NSIDC, GRACE, and SSP and passes arrays in. Dataclasses live in `model/ice_model.py` (a split inside `model/` is fine). Python 3.11+.

```python
from dataclasses import dataclass
from typing import Literal

Pole = Literal["north", "south"]
Band = Literal["median", "low", "high"]
ScenarioId = Literal["ssp119", "ssp126", "ssp245", "ssp370", "ssp585"]


@dataclass(frozen=True)
class MonthlyGrid:
    pole: Pole
    year: int
    month: int
    width: int
    height: int
    values_u8: bytes          # row-major, 0-100 percent, 255 masked
    extent_km2: float | None  # CSV extent already converted to km^2; None on a gap
    area_km2: float | None
    gap_masked: bool
    pole_hole_cells: int


@dataclass(frozen=True)
class MassPoint:
    decimal_year: float
    mass_gt: float            # anomaly, negative = loss
    uncertainty_gt: float | None


@dataclass(frozen=True)
class TrainingBundle:
    grids: list[MonthlyGrid]
    cumulative_co2_gt_by_year: dict[int, float]          # historical, empty if missing
    ssp_cumulative_co2_gt: dict[str, dict[int, float]]   # canonical scenario -> year -> Gt
    ssp_warming_c: dict[str, dict[int, float]]           # canonical scenario -> year -> °C
    grace_greenland: list[MassPoint]
    grace_antarctica: list[MassPoint]
    cell_area_km2: dict[str, list[float]] | None         # pole -> row-major km^2, or None
    regressor: Literal["cumulative_co2_gt", "ipcc_ar6_warming_c_fallback"]


@dataclass(frozen=True)
class GridRequest:
    pole: Pole
    year: int
    month: int
    scenario: ScenarioId
    blend: float | None
    band: Band


@dataclass(frozen=True)
class ConcentrationGrid:
    width: int
    height: int
    values_u8: bytes
    extent_km2: float | None
    extent_scale: float
    is_forecast: bool
    gap_masked: bool
    pole_hole_cells: int
    source: str


@dataclass(frozen=True)
class ExtentPoint:
    year: int
    extent_km2: float | None
    low_km2: float | None
    high_km2: float | None
    is_forecast: bool
    gap_masked: bool
    source: str
    extent_method: Literal["nsidc_monthly_csv", "model_cell_sum_scaled"]


@dataclass(frozen=True)
class ShippingSeason:
    open_season_days: int
    opening_date: str | None
    open_month_count: int


@dataclass(frozen=True)
class ImpactResult:
    is_forecast: bool
    northern_sea_route: ShippingSeason
    northwest_passage: ShippingSeason
    ice_free_september_year: int | None
    winter_ice_loss_index: float | None
    greenland_mm: float
    antarctica_mm: float
    total_mm: float
    low_mm: float
    high_mm: float
    sea_level_source: Literal["grace-gracefo", "nasa-science-summary-fallback"]


@dataclass(frozen=True)
class PoleScore:
    rmse_km2: float
    bias_km2: float
    n_months: int
    skill_vs_climatology: float


@dataclass(frozen=True)
class BacktestResult:
    trained: bool
    train_end_year: int
    eval_start_year: int
    eval_end_year: int
    north: PoleScore
    south: PoleScore


class IceModel:
    def update(self, bundle: TrainingBundle) -> None:
        """Fit per-cell, per-month logistic curves and the ice-sheet quadratic.
        Must not raise because a list is empty. An empty grid list leaves
        concentration() returning a masked grid with source 'model-not-ready'.
        """

    def concentration(self, req: GridRequest) -> ConcentrationGrid:
        """Observed year-month: return the satellite bytes for every band.
        Later year-month: logistic prediction at the blended regressor.
        Gap: all 255, extent_km2 None, gap_masked True.
        """

    def extent_series(self, pole: Pole, month: int, scenario: ScenarioId,
                      blend: float | None) -> list[ExtentPoint]:
        """1979..2100 inclusive for that pole and calendar month."""

    def impact(self, year: int, scenario: ScenarioId, blend: float | None) -> ImpactResult:
        """Median ice. Sea-level low/high from the mass residual bootstrap."""

    def backtest(self) -> BacktestResult:
        """Second fit on year <= 2010. Does not replace the model update() stored."""
```

`update` may be called again when a feed refresh finishes. It replaces the fit. `backtest` must not change what `concentration` returns.

### Logistic fit

For each pole, calendar month, and cell with at least 8 unmasked training years:

- `y = clip(percent / 100, 0.001, 0.999)`
- `x` is the regressor for that year (cumulative GtCO2, or the fallback °C), centered by its training mean
- Fractional logit via IRLS: `p = 1 / (1 + exp(-(a + b x)))`
- Fewer than 8 years: predict that cell’s training mean and a zero slope

Bootstrap: resample training years with replacement, refit, 40 draws, seed 0. Forecast `low` / `median` / `high` are percentiles 10 / 50 / 90 of `round(100 * p)` clipped to 0–100. Cells masked in every training year stay 255.

Extent for a forecast: cell area defaults to 625 km² when `cell_area_km2` is None. Scale as specified under `GET /ice`. Pole-hole cells are already 100 in `values_u8` and count as ice.

### Ice sheets

On real GRACE points, fit `M(t) = c + r (t - t0) + 0.5 a (t - t0)^2` in Gt, `t0` = mean decimal year of the samples.

For a year `Y` after the last sample, with `W` the blended warming:

```
scale = max(W(Y) / W(Y_last), 0)
M(Y) = c + r (Y - t0) + 0.5 * a * (Y - t0)^2 * scale
```

For a year inside the GRACE record, use the observed anomaly (nearest sample) and do not apply `scale`. Contribution in millimetres:

```
mm = -(M(Y) - M_first) / 362
```

If `M` is already relative to the first sample, `M_first` is about 0; still subtract the first sample so the baseline is explicit. `low_mm` and `high_mm` come from refitting on residual-resampled series (same 40 / seed 0 / p10 / p90) and summing both ice sheets.

With no GRACE points, use −264 Gt/yr and −135 Gt/yr from 2002 and set `sea_level_source` to `nasa-science-summary-fallback`. After 2025, multiply the rate by the warming scale against warming(2025). Set acceleration to 0.

### Corridors

Hard-code these boxes inside `model/` (ocean cells only, masked cells dropped):

- Northern Sea Route: latitude 66–80 °N, longitude 30–180 °E
- Northwest Passage: latitude 68–75 °N, longitude 140–60 °W

A month is open when the mean remaining concentration is `< 15`. This is a hackathon proxy for a navigable season, not a shipping analysis. The wording in the impact JSON definition is the wording the panel shows.

## Fixture banner

Live responses set `"fixture": false` and `X-Ice-Fixture: false`.

Files in `fixtures/` set `"fixture": true` and include:

```json
"fixture_notice": "DEV FIXTURE — not an observation and not a forecast. Generated for parallel UI work."
```

The frontend shows that notice whenever it is reading fixtures or whenever `fixture` is true. It does not show fixture numbers as satellite data.

## Task 4: Ships

Role 4 owns this section. Routes live on `ships.router` and are mounted by one hook, which is the only backend edit:

```python
from ships.router import install
install(app, concentration_at=north_concentration_at)
```

`concentration_at(lat: float, lon: float) -> float | None` returns ice concentration in percent from the latest observed north grid, or `None` when the cell is masked or no grid is loaded. The ships package does not download NSIDC. Omitting the sampler is legal: ships still stream and `ice_risk.level` is `unknown`.

Standalone process for Role 4, so port 8000 stays with Role 1: `PYTHONPATH=. uvicorn ships.router:app --port 8099`.

`AISSTREAM_API_KEY` is required for a live socket. `GFW_API_TOKEN` is optional. Both are empty placeholders in `.env.example`. A missing AIS key is HTTP 200 with health `down`, not a 500 and not the recorded sample.

### `GET /ships/health`

No query parameters. Always HTTP 200.

`status` uses the feed enum: `ok`, `stale`, `down`.

| `status` | When |
| --- | --- |
| `ok` | Socket open, and either a message arrived within the last 60 seconds or the socket has been open for under 60 seconds. |
| `stale` | Socket down or silent for more than 60 seconds, and at least one vessel is still stored. |
| `down` | Key unset, or socket down and no vessel has ever been stored. |

```json
{
  "status": "ok",
  "fixture": false,
  "generated_at": "2026-11-19T23:00:00Z",
  "stale": false,
  "connected": true,
  "using_sample": false,
  "source": "aisstream",
  "last_message_at": "2026-11-19T23:00:00Z",
  "vessel_count": 1,
  "reconnect_attempt": 0,
  "decode_errors": 0,
  "detail": "arctic 60–90 N; gfw: off"
}
```

`using_sample` is **false** on this route always. `fixture` is **false**. `last_message_at` is `null` until the first accepted message. `detail` is a short human string and must not contain the API key. `stale` is true when `status` is `stale`.

`Cache-Control: no-cache`.

### `GET /ships`

No query parameters. Always HTTP 200. One object per vessel currently stored (dropped 6 hours after `last_seen`).

```json
{
  "fixture": false,
  "generated_at": "2026-11-19T23:00:00Z",
  "stale": false,
  "source": "aisstream",
  "bounding_box": { "min_lat": 60, "max_lat": 90, "min_lon": -180, "max_lon": 180 },
  "count": 1,
  "ships": [
    {
      "mmsi": "273456789",
      "name": "EXAMPLE",
      "vessel_type": "cargo",
      "vessel_type_code": 70,
      "lat": 72.1,
      "lon": 40.2,
      "sog_kn": 12.4,
      "cog_deg": 35.0,
      "heading": 40,
      "last_seen": "2026-11-19T23:00:00Z",
      "is_stale": false,
      "icebreaker": false,
      "track": [
        {
          "lat": 72.0,
          "lon": 40.0,
          "t": "2026-11-19T22:40:00Z",
          "source": "aisstream"
        }
      ],
      "ice_risk": {
        "level": "marginal",
        "concentration_here": 8.0,
        "concentration_ahead": 22.0,
        "entering": true
      }
    }
  ]
}
```

| Field | Unit / values |
| --- | --- |
| `mmsi` | String, 9 digits, first digit 2–7. |
| `name` | String or `null`. |
| `vessel_type` | `fishing`, `tug`, `pleasure`, `high_speed`, `passenger`, `cargo`, `tanker`, `special`, `unknown`. |
| `vessel_type_code` | AIS integer or `null`. |
| `lat`, `lon` | Degrees. Latest accepted AIS position. Never a Global Fishing Watch point. |
| `sog_kn` | Knots, or `null` when AIS sent 102.2 or 102.3. |
| `cog_deg` | Degrees, or `null` when AIS sent 360. |
| `heading` | Degrees, or `null` when AIS sent 511. |
| `last_seen` | UTC ISO-8601 of the message that set the current position. |
| `is_stale` | True when the feed is stale or this vessel’s `last_seen` is older than 15 minutes. |
| `icebreaker` | True only when the name contains `ICEBREAKER`. AIS has no ice class. |
| `track` | Oldest first, at most 12 points, including the current position as the last AIS point. `source` is `aisstream` or `gfw`. |
| `ice_risk.level` | `clear`, `marginal`, `heavy`, `unknown`. |
| `ice_risk.concentration_here` | Percent, or `null`. |
| `ice_risk.concentration_ahead` | Percent, or `null`. Max of the samples at 6 h and 12 h along `cog_deg`, or along `heading` when course is null. Equals `concentration_here` when `sog_kn` is 0. `null` when `sog_kn` is null. |
| `ice_risk.entering` | True when both concentrations are numbers and the ship crosses 15% or 50% upward. |

`source` on the envelope is `aisstream` while the socket is healthy and `aisstream-stale` while last positions are being served. `stale` on the envelope matches health. `count` is `ships.length`.

`level` is `heavy` if either concentration is `>= 50`, else `marginal` if either is `>= 15`, else `clear` if any sample is a number, else `unknown`. A missing sampler leaves both concentrations `null` and the ship in the list.

### Recorded sample

`ships/testdata/ais_sample.jsonl` is an offline unit-test input. The test helper that reads it must set:

```json
{
  "fixture": true,
  "source": "recorded-sample",
  "fixture_notice": "RECORDED SAMPLE — not a live AIS feed. Offline unit-test input only."
}
```

No HTTP route reads that file. Do not add a query switch for it. The globe must not present the sample as live traffic.

