# Ship layer

Standalone Three.js layer. It polls `GET /ships` and does not import the globe app.

```javascript
import { createShipLayer } from "../../ships/web/shipLayer.js";
const ships = createShipLayer({ scene, latLonToVector3, apiBase, globeRadius });
ships.onPick = (ship) => console.log(ship.name, ship.mmsi, ship.ice_risk);
```

`latLonToVector3(lat, lon, radius)` must return a `THREE.Vector3` on the globe. Call `ships.update(time)` from the render loop so heavy-ice rings pulse, and `ships.pick(camera, pointerEvent)` from the click handler. `three` is a peer dependency.

The globe already does this in `frontend/src/globe.js`. `ships/web/demo.html` is a bare sphere for trying the layer by itself. Preview markers in that file are not a live feed and are never used by the app.

Live ships need the API on `apiBase` (the globe uses `VITE_API_BASE`, default `http://127.0.0.1:8000`). With no `AISSTREAM_API_KEY`, `/ships` is an empty list and health status is `down`.
