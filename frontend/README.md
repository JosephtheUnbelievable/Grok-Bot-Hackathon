# frontend/

Role 3. Branch `feat/frontend`. You are the only person who writes here.

Vite + Three.js globe on port 5173. A year slider (1979–2100), a scenario blend slider (0–4 across the five SSPs), a median/low/high control, and an impact panel. Texture for our ice is the API’s equirectangular PNG (1024×512, north up, −180 at the left). Basemap and coastlines may come straight from NASA GIBS and Natural Earth; URLs are in `PLAN.md`.

Agreed layout (you create it):

```
frontend/
  index.html
  package.json
  vite.config.js
  src/main.js
```

`VITE_USE_FIXTURES=true` (the default until minute 20) imports the JSON in `../fixtures/` and shows `fixture_notice` on screen. `VITE_USE_FIXTURES=false` calls `VITE_API_BASE` (default `http://127.0.0.1:8000`). A down API must not blank the canvas.

Read `tasks/3-frontend-globe.md` and `API.md`. You may import `fixtures/`. You may not edit it, or edit `backend/`, `model/`, `tasks/`, `PLAN.md`, `API.md`, or the root `README.md`.

## Contract change requests

_Empty unless the checkpoint agrees a change. Do not edit API.md yourself._
