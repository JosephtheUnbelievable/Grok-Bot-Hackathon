# Role 3 — Globe and impact UI

You are one of three people in a ~40-minute hackathon. Build only the frontend. Two other assistants own `backend/` and `model/`. Stay inside `frontend/` or you will collide.

## What you build

A Vite + Three.js page:

- A globe that can be rotated and shows **both poles** (the user can turn it; do not lock the camera to one hemisphere).
- Greenland and Antarctica readable as the ice sheets (land ice on the basemap, plus the sea-level numbers in the panel — you do not invent a thickness raster).
- A year slider from **1979** to **2100**, and a month control from 1 to 12. Default month 9 in the north and 2 in the south. December 1987 and January 1988 have to be selectable so the instrument gap can be shown. The extent chart stays on the selected month.
- A scenario slider with stops at SSP1-1.9, SSP1-2.6, SSP2-4.5, SSP3-7.0, SSP5-8.5. Positions between stops are legal. Send them as `blend`, a float from 0 to 4 (`0` = SSP1-1.9 … `4` = SSP5-8.5).
- A median / low / high control. Send `band=median|low|high`.
- An impact panel: Northern Sea Route and Northwest Passage open-season days and opening date, the first ice-free September year, the winter sea-ice loss index, and sea level in millimetres (Greenland, Antarctica, total, with the low–high total).
- A clear observed-versus-forecast state, a stale-feed state, a gap state (no ice drawn, “instrument gap”, not an empty ocean sold as data), and a fixture banner.

Our ice overlay comes from the API as an equirectangular PNG, 1024×512, north at the top, longitude −180 on the left, transparent where there is no ice. Put that image on the sphere. The photographic earth can be NASA GIBS, called **from the browser** (CORS is open). Coastlines can be Natural Earth. URLs are in `PLAN.md` and repeated below.

## Folder ownership

- Branch: `feat/frontend`
- You write: `frontend/**` only.
- You do not write: `backend/`, `model/`, `fixtures/`, `tasks/`, `PLAN.md`, `API.md`, root `README.md`, `LICENSE`, `.gitignore`.
- You may import JSON from `../fixtures/`. Importing is not editing. Do not copy the fixtures into `frontend/` “so they are easier”; that would fork the contract.

## Contract you consume

Base URL `import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000"`.

| Call | Use |
| --- | --- |
| `GET /health` | Feed pills and the overall `status`. `observed_through` is “today” on the slider. `model_ready` false means forecasts are not available yet. |
| `GET /ice/{pole}/{year}/{month}?scenario=&blend=&band=&format=png` | Globe texture. Read the `X-Ice-*` headers. `format=json` is optional and is the native polar grid; you do not need it for the hackathon scene. |
| `GET /extent/{pole}?scenario=&blend=&month=` | Small chart under the globe. Default month is 9 in the north and 2 in the south if you omit it. Break the line where `extent_km2` is null. |
| `GET /impact?year=&scenario=&blend=` | The panel. Show `shipping.*.definition` somewhere discoverable (tooltip or a one-line caption) so the proxy is not presented as a route solver. |
| `GET /backtest` | One line of text: train through 2010, September RMSE north and south, skill. Hide it if `trained` is false. |

Query rules:

- On an observed year (`year/month` ≤ `observed_through`), you may omit `blend`. The server ignores scenario for those pixels. Still label the slider so people see it will matter after the record.
- On a forecast year, send `blend` from the slider (one decimal is plenty). You can omit `scenario` when `blend` is set; if you send both, the server uses `blend`.
- `band` applies to `/ice` and is reflected by asking `/extent` to draw `low_km2` / `extent_km2` / `high_km2`. Impact sea level always has its own `low_mm` / `high_mm`.

Error body is `{ "error": { "code", "message", "param" } }`. 400 means fix the query. 503 and network failures mean: keep the last good texture, show “feed down”, leave the sliders usable.

Headers that matter on the PNG: `X-Ice-Is-Forecast`, `X-Ice-Stale`, `X-Ice-Gap-Masked`, `X-Ice-Extent-Km2` (empty means null — do not draw 0), `X-Ice-Source`, `X-Ice-Observed-Through`, `X-Ice-Fixture`.

Units to display: extent in million km² (divide the API’s km² by 1e6, one decimal), sea level in mm, open season in days, winter index as a percent, opening date as a date or “does not open”.

`regressor` of `ipcc_ar6_warming_c_fallback` gets a caption that the CO2 pathway feed is down and the forecast is using IPCC warming anchors. `sea_level.source` of `nasa-science-summary-fallback` gets a caption that GRACE needs an Earthdata login and the rates are the NASA Science summary (−264 Gt/yr Greenland, −135 Gt/yr Antarctica), not a live mass file.

## Fixtures until the API exists

`VITE_USE_FIXTURES` defaults to `true` for you until minute 20.

| File | Stands in for |
| --- | --- |
| `fixtures/health.json` | `GET /health` (already `degraded`, so you can design that state) |
| `fixtures/ice_observed.json` | September 2024 JSON grid. 8×8, not the live size. |
| `fixtures/ice_forecast.json` | 2050, `blend` 2.4, less ice |
| `fixtures/ice_gap.json` | December 1987, `extent_km2: null`, `gap_masked: true` |
| `fixtures/extent_north.json` | A **short** series. Live series have every year 1979–2100. Do not assume length 6. |
| `fixtures/impact.json` | 2050 SSP2-4.5 panel |
| `fixtures/backtest.json` | Backtest line |
| `fixtures/error.json` | Error shape |

Every fixture has `fixture: true` and `fixture_notice`. Render that notice as a banner. Do not present the numbers as real ice.

There is no fixture PNG. In fixture mode, paint a simple polar cap from the decoded `values_b64` (255 = draw nothing, `>= 15` = white-blue) or a solid overlay, and switch to the real PNG as soon as `VITE_USE_FIXTURES=false`.

Decode:

```js
const bytes = Uint8Array.from(atob(values_b64), (c) => c.charCodeAt(0));
// row-major, index = y * width + x, 255 masked, 0–100 percent
```

Vite needs the repo root readable so the import works. In `frontend/vite.config.js` set `server.fs.allow` to include the parent directory, then `import health from "../../fixtures/health.json"`.

## Basemap (browser → NASA), no login

Verified whole-Earth image, JPEG, 1024×512, CORS `*`:

```
https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS=MODIS_Terra_CorrectedReflectance_TrueColor&FORMAT=image/jpeg&WIDTH=1024&HEIGHT=512&CRS=EPSG:4326&BBOX=-90,-180,90,180&TIME=2024-09-15
```

Use a recent day at or before `observed_through` when you have it. If GIBS fails, a dark sphere plus the ice PNG is an acceptable degraded globe. Coastlines, optional:

`https://naciscdn.org/naturalearth/110m/physical/ne_110m_coastline.zip`

GIBS sea ice (`LAYERS=AMSRU2_Sea_Ice_Concentration_25km`, `FORMAT=image/png`, `TRANSPARENT=true`) only covers roughly 2012 through the layer’s end date. Use it as a “latest satellite picture” toggle if you have time. The slider’s historical and future ice is **our** PNG, not GIBS.

## How to test alone

```bash
cd frontend
npm install
VITE_USE_FIXTURES=true npm run dev
```

Open `http://127.0.0.1:5173` and do this by hand:

- The fixture banner is visible and names the data as a dev fixture.
- Dragging the year from 1979 to 2100 updates the caption. Crossing `observed_through` (2024-09 in the fixture) switches the label from observed to forecast and swaps in `ice_forecast.json` when the year is 2050 or later.
- December 1987 shows the gap state, not a number 0.
- The scenario slider moves between the five named stops. In fixture mode, any blend other than the default can keep showing `impact.json`, but the caption must show the blend value you would send.
- Median / low / high changes which extent field you would plot (`low_km2` is null on observed points; do not draw those as zero).
- The impact panel shows route days, opening dates, ice-free year 2052, winter index 0.42, and 82 / 41 / 123 mm, and it says the sea-level source is the NASA summary fallback.
- `/health` fixture status `degraded` is visible (GIBS stale, GRACE stale).
- Set `VITE_USE_FIXTURES=false` with no server running. The page stays up, sliders move, and a “feed down” state replaces the panel instead of an empty document.

Suggested `package.json` dependencies: `three`, `vite`. No UI framework is required.

## Acceptance criteria

- Both poles are reachable by rotating the globe.
- Year range is 1979–2100 and the five SSP names are on the scenario control.
- Fixture mode never looks like a live NSIDC product.
- Gap and null extent do not render as zero ice.
- A dead API does not crash the page.
- `git status` shows changes only under `frontend/`.

## Minute 20

Push `feat/frontend`. You merge last, after `feat/model` and `feat/backend`. Until then stay on fixtures. At the checkpoint, set `VITE_USE_FIXTURES=false` and confirm one PNG from `GET /ice/north/2020/9?format=png` appears on the globe. Rebase onto `main` before you merge. After minute 35, do not start a new scene graph; the last five minutes belong to a single person editing the root README and recording the video.
