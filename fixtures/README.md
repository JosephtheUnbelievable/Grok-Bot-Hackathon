# Dev fixtures

These JSON files match the success and error shapes in `API.md`. They exist so the globe can be built before the API is listening.

**DEV FIXTURE — not an observation and not a forecast.** Every file has `"fixture": true` and a `fixture_notice`. The UI must show that notice while it is on this data. Do not screenshot these numbers as satellite ice.

The live service sets `"fixture": false`. `GET /ice/...?...&format=png` is a binary image and has no fixture file; use the JSON grids here for logic, and a flat overlay until the API returns a PNG.

Grids are 8×8 on purpose. The live grid is the NSIDC polar-stereographic raster (hundreds of cells on a side). Decode `values_b64` as row-major uint8; do not assume 8×8 in production code.

`extent_north.json` is a September series, so 1987 is present there. The instrument gap is December 1987 and January 1988, and it is represented by `ice_gap.json`.

Nobody edits this folder during the sprint.
