# Role 4 — Arctic ship markers

You are the fourth person in a ~40-minute hackathon. Three other assistants are already writing `backend/`, `model/`, and `frontend/`. Build only the ships package. Stay inside `ships/` or you will collide.

## What you build

A Python worker the API can mount with one hook. It shows live ships on the Arctic sea-ice map.

1. Connect to the AISStream.io websocket. Read the key from `AISSTREAM_API_KEY`. The placeholder is already an empty line in `.env.example`. Never commit a real key, and never print the key in a log or in `/ships/health`.
2. Subscribe to the Arctic box, latitude **60 N to 90 N**, longitude **−180 to 180**.
3. Keep each vessel’s latest position, speed, heading, type, name, MMSI, and a short recent track.
4. Clean messages before they enter that state.
5. Stay up when the socket drops: reconnect with backoff, keep the last positions, mark them stale. Do not crash the process.
6. Sample the current NSIDC north concentration grid at the ship and along the heading it will travel in the next 6–12 hours. Flag heavy ice and whether the ship is an icebreaker.
7. Serve `GET /ships` and `GET /ships/health` from `ships.router`.

Optional, and off unless a token is set: Global Fishing Watch, delayed, history only. It must not become the live position.

Entry point the backend adds, and the only edit outside `ships/`:

```python
from ships.router import install
install(app, concentration_at=north_concentration_at)
```

`concentration_at(lat, lon)` returns a percent (`0–100`) or `None` when the cell is masked or the grid is missing. You define `install`. Role 1 passes the sampler. You do not open NSIDC yourself and you do not import `backend`.

`ships/router.py` also builds a standalone `app` so you can run without their process:

```bash
PYTHONPATH=. uvicorn ships.router:app --port 8099
```

Port **8099** on purpose. Role 1 already owns port 8000.

## Folder ownership

- Branch: `feat/ships`
- You write: `ships/**` only.
- You do not write: `backend/`, `model/`, `frontend/`, `fixtures/`, `tasks/`, `PLAN.md`, `API.md`, root `README.md`, `LICENSE`, `.gitignore`, `.env.example`.
- You may read `API.md`. The section **Task 4: Ships** is the contract. If this brief and that section disagree, `API.md` wins.
- The empty `AISSTREAM_API_KEY=` line is already in `.env.example`. Put a real key only in the shell or in a gitignored `.env`.

Agreed files:

```
ships/
  __init__.py
  router.py            # install(), router, and a standalone app
  worker.py            # websocket loop, latest positions, tracks
  ice.py               # project ahead, sample concentration, risk level
  requirements.txt
  testdata/ais_sample.jsonl   # recorded sample for unit tests only
  tests/test_ships.py
```

## Feed

Public documented endpoint: `wss://stream.aisstream.io/v0/stream`. Docs: `https://aisstream.io/documentation`.

First message after connect, and the only place the key goes:

```json
{
  "APIKey": "<AISSTREAM_API_KEY>",
  "BoundingBoxes": [[[60.0, -180.0], [90.0, 180.0]]],
  "FilterMessageTypes": [
    "PositionReport",
    "StandardClassBPositionReport",
    "ExtendedClassBPositionReport",
    "ShipStaticData"
  ]
}
```

If the provider rejects one box that wide, resubscribe as six boxes of 60° of longitude, same 60–90 N band. Class B reports matter: a lot of fishing ships never send `PositionReport`.

Use `MetaData.time_utc` as the message time. Static fields (`Name`, `Type`) update the vessel and do not move it. Position fields are `Latitude`, `Longitude`, `Sog` (knots), `Cog` (degrees), `TrueHeading`.

**Global Fishing Watch, optional.** Gateway `https://gateway.api.globalfishingwatch.org/v3` (HTTP 401 without a token, so the host is the right one). Docs: `https://globalfishingwatch.org/our-apis/`. Token in `GFW_API_TOKEN`, also an empty placeholder in `.env.example`. Public positions are delayed by about **72 hours**. Append them to `track` with `"source": "gfw"`. Never copy a GFW point into `lat` / `lon`. No token means skip the call, leave the tracks AIS-only, and say `gfw: off` in the health `detail`. A GFW failure does not mark the AIS feed down.

## Cleaning

Drop the message (do not update `lat` / `lon`) when any of these is true:

- Latitude and longitude are both **0** (null island), or latitude is outside **−90–90**, or longitude is outside **−180–180**, or latitude is below **60**.
- MMSI is not a 9-digit integer whose first digit is **2–7** (ship station). Drop coast stations, AtoN, and aircraft.
- Speed over ground is a real number **greater than 50 knots** and **below 102.2**. That is not a ship we can plot.
- The message time is **older** than `last_seen` for that MMSI (out of order).
- Same MMSI, same `time_utc`, and the same position to **0.0001°** (duplicate).

AIS sentinels are not speeds and not headings. Keep the position and store null:

| Raw value | Field | Store |
| --- | --- | --- |
| SOG **102.2** or **102.3** | `sog_kn` | `null` (102.2 means “102.2 kn or more”, 102.3 means not available) |
| COG **360** | `cog_deg` | `null` |
| True heading **511** | `heading` | `null` |

Names are space-padded. Strip, and strip trailing `@`. An empty name is `null`.

`vessel_type` is a group, from the AIS type code: `fishing` (30), `tug` (31, 32, 52), `pleasure` (36, 37), `high_speed` (40–49), `passenger` (60–69), `cargo` (70–79), `tanker` (80–89), `special` (any other code 1–99), `unknown` when static data has not arrived. Keep the numeric code on `vessel_type_code` (`null` if unknown).

Track: the current position plus older ones, oldest first, **at most 12** points, each `{ "lat", "lon", "t", "source" }`. `source` is `aisstream` unless the point came from GFW. A new accepted position appends; the cap drops the oldest.

Forget a vessel **6 hours** after `last_seen`. Until then it stays in the list.

## Ice risk

Call `concentration_at` at the current position and at the points **6 h** and **12 h** ahead. Course is `cog_deg` when it is set, otherwise `heading`. Distance is `sog_kn * hours` nautical miles (great-circle; 60 nautical miles is one degree of latitude). `concentration_ahead` is the **maximum** of the two future samples.

No speed (`sog_kn` null or 0): do not project. `concentration_ahead` equals `concentration_here` when the ship is stopped, and is `null` when speed is unknown.

`255` / masked / no grid → that sample is `null`. Do not treat land as ice.

`ice_risk.level`:

| Level | When |
| --- | --- |
| `heavy` | either sample is a number and `>= 50` |
| `marginal` | either sample is `>= 15` and the row above did not match |
| `clear` | every non-null sample is `< 15` |
| `unknown` | both samples are `null` |

`entering` is true only when both samples are numbers and the ship is crossing upward: ahead `>= 15` while here `< 15`, or ahead `>= 50` while here `< 50`.

AIS has no ice-class field. `icebreaker` is true only when the stripped name contains `ICEBREAKER` (case-insensitive). Otherwise false. Do not guess a Polar Class from the type code. The map treats `icebreaker: false` together with `marginal` or `heavy` as a ship that is not marked ice-strengthened.

Thresholds match the rest of the repo: **15%** is the NSIDC extent line, **50%** is heavy ice.

## Robustness

- One background task. Reconnect forever. Backoff **1, 2, 4, 8, 16, 30** seconds, then stay at 30, with up to 25% jitter. A clean server close resets the delay to 1 s.
- On disconnect or on **60 s** with no message: keep every stored ship, set each `is_stale` to true, set the envelope `stale` to true. A ship is also `is_stale` when its own `last_seen` is older than **15 minutes**, even if the socket is open.
- A bad JSON frame, a missing field, or a sampler exception is counted and skipped. It must not kill the loop or the request.
- Missing `AISSTREAM_API_KEY`: do not open the socket. Both routes still return HTTP 200. Health `status` is `down`, `detail` explains that the key is unset, `ships` is `[]`.
- `/ships` and `/ships/health` never return 500 because the feed is down.

## Contract you implement

Shapes, units, and the replay rule are in `API.md` under **Task 4: Ships**. Implement both routes on the router with no prefix, so the mounted app serves `GET /ships` and `GET /ships/health`.

Live responses: `fixture: false`, `source` `aisstream` or `aisstream-stale`, `using_sample: false`. `Cache-Control: no-cache`.

### Recorded sample, tests only

`ships/testdata/ais_sample.jsonl` is a short capture you record yourself (a handful of lines is enough). The unit tests load it through a helper, for example `load_recorded_sample(path)`, which sets `fixture: true`, `source: "recorded-sample"`, and a `fixture_notice` that says it is a recorded sample for offline tests and not a live feed.

`install`, the standalone `app`, and both HTTP routes must not call that helper. There is no query parameter that selects the file. `GET /ships/health` on the server reports `using_sample: false` always. Do not point the globe at the sample and do not leave the notice off.

### What the globe should draw

Role 3 owns `frontend/`. You do not edit it. The JSON above is enough for them to add, after the checkpoint:

- A marker per ship, position `lat` / `lon`, colour by `vessel_type` (cargo `#f4d03f`, tanker `#e74c3c`, passenger `#5dade2`, fishing `#27ae60`, tug `#af7ac5`, pleasure `#85929e`, high-speed `#e67e22`, special `#d5d8dc`, unknown `#7f8c8d`).
- A polyline through `track`.
- A ring when `ice_risk.level` is `marginal` (`#f5b041`) or `heavy` (`#c0392b`). Stale markers at about 40% opacity.
- A click popup: name, MMSI, type, speed, course, heading, `last_seen`, ice level, both concentrations, and icebreaker yes or no. If `stale` is true, say the position is last known and the feed is interrupted.

## How to test alone

No key and no socket for the unit tests. Do not start `backend/` or `frontend/`.

```bash
cd ships
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd ..
PYTHONPATH=. ships/.venv/bin/pytest ships/tests
```

Suggested `requirements.txt`: `fastapi`, `uvicorn`, `websockets`, `httpx`, `pytest`.

Minimum tests, all offline. Drive them with `load_recorded_sample` or with hand-built frames. The HTTP app is not in this list:

1. A `0,0` position is dropped. A position at 70 N is kept.
2. SOG 80 is dropped. SOG 102.3 is kept with `sog_kn: null`. Heading 511 becomes `null`.
3. MMSI `123` and MMSI `003456789` are dropped. A 9-digit MMSI starting with 2–7 is kept.
4. A duplicate and an older timestamp do not move the ship and do not grow the track. The track stays time-ordered and stops at 12 points.
5. The sample helper reports `fixture: true`, `source: "recorded-sample"`, and a notice. A fresh router reports `using_sample: false` and does not read `testdata/`.
6. After a simulated disconnect the last `lat` / `lon` are unchanged and `is_stale` is true. The backoff sequence is 1, 2, 4, 8, 16, 30 and the helper does not raise.
7. A ship at 70 N, course 0°, 12 kn, has its 6 h point about 1.2° further north. Concentration ahead 60 → `heavy`. Ahead 20 with here 0 → `marginal` and `entering: true`. Both under 15 → `clear`. Sampler returning `None` → `unknown`, and the ship is still in the list. Name `EXAMPLE ICEBREAKER` sets `icebreaker: true`; `EXAMPLE` does not.

Live smoke, only if you have a key, still on port 8099:

```bash
AISSTREAM_API_KEY=... PYTHONPATH=. ships/.venv/bin/uvicorn ships.router:app --port 8099
curl -s localhost:8099/ships/health
curl -s localhost:8099/ships
```

Health is JSON and HTTP 200 before the first message. With the key unset, `status` is `down` and the body is JSON, not a traceback. Ships that appear have latitudes `>= 60`. Nothing in that body says `recorded-sample`.

## Acceptance criteria

- `GET /ships` and `GET /ships/health` match the **Task 4: Ships** section of `API.md`.
- The socket task reconnects and a dead feed still returns HTTP 200 with the last positions and `stale: true`.
- `0,0`, speeds over 50 kn, bad MMSI, duplicates, and out-of-order messages never move a marker.
- Ice levels use 15% and 50%, and a missing grid yields `unknown` rather than a crash or a fake 0.
- `AISSTREAM_API_KEY` and `GFW_API_TOKEN` are not in git. `.env.example` stays a placeholder.
- The recorded sample is labeled and is reachable only from `ships/tests`. The live routes stay `fixture: false` and `using_sample: false`.
- `pytest` passes with the network off.
- `git status` shows changes only under `ships/`.

## Minute 20

Push `feat/ships`. Do not merge it yourself and do not rebase onto someone else’s feature branch. At the checkpoint one person adds the `install(app, concentration_at=...)` hook in `backend/app/main.py` and Role 3 adds the markers. Until that hook exists, the demo of your work is port 8099.
