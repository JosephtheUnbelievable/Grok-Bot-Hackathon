"""``GET /ships`` and ``GET /ships/health``, plus the one-line mount.

    from ships.router import install
    install(app, concentration_at=north_concentration_at)

Standalone, so Role 1 keeps port 8000::

    PYTHONPATH=. uvicorn ships.router:app --port 8099
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ships.worker import Fleet, iso_z, run_forever, run_gfw, utcnow

logger = logging.getLogger("ships")

NO_CACHE = {"Cache-Control": "no-cache"}


def _load_dotenv(path: Path) -> None:
    """Fill empty placeholders from a gitignored ``.env``. Never overrides the shell."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _load_local_env() -> None:
    root = Path(__file__).resolve().parents[1]
    here = Path(__file__).resolve().parent
    _load_dotenv(root / ".env")
    _load_dotenv(here / ".env")


_load_local_env()


def install(app, concentration_at=None, start_worker: bool = True) -> Fleet:
    """Mount the ship routes and, by default, the AIS background task.

    ``concentration_at(lat, lon)`` returns percent or None. Omitting it is
    legal: ships still stream and ``ice_risk.level`` is ``unknown``.
    ``start_worker`` is false only in tests that must not open a socket.
    """
    fleet = Fleet()
    app.state.ships_fleet = fleet
    app.state.ships_concentration_at = concentration_at

    @app.get("/ships/health")
    def ships_health():
        try:
            body = fleet.health_payload()
        except Exception:
            logger.warning("ships health failed")
            body = {
                "status": "down",
                "fixture": False,
                "generated_at": iso_z(utcnow()),
                "stale": False,
                "connected": False,
                "using_sample": False,
                "source": "aisstream",
                "last_message_at": None,
                "vessel_count": 0,
                "reconnect_attempt": 0,
                "decode_errors": 0,
                "detail": "arctic 60–90 N; gfw: off",
            }
        body["using_sample"] = False
        body["fixture"] = False
        return JSONResponse(body, headers=NO_CACHE)

    @app.get("/ships")
    def ships():
        try:
            body = fleet.ships_payload(concentration_at=getattr(app.state, "ships_concentration_at", None))
        except Exception:
            logger.warning("ships list failed")
            fleet._bump_decode()
            body = {
                "fixture": False,
                "generated_at": iso_z(utcnow()),
                "stale": False,
                "source": "aisstream",
                "bounding_box": {"min_lat": 60, "max_lat": 90, "min_lon": -180, "max_lon": 180},
                "count": 0,
                "ships": [],
            }
        body["fixture"] = False
        return JSONResponse(body, headers=NO_CACHE)

    if start_worker:

        @app.on_event("startup")
        async def _start_ships() -> None:
            app.state.ships_task = asyncio.create_task(run_forever(fleet), name="aisstream")
            app.state.gfw_task = asyncio.create_task(run_gfw(fleet), name="gfw")

        @app.on_event("shutdown")
        async def _stop_ships() -> None:
            for name in ("ships_task", "gfw_task"):
                task = getattr(app.state, name, None)
                if task is None or task.done():
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return fleet


def create_app(concentration_at=None, start_worker: bool = True) -> FastAPI:
    application = FastAPI(title="Arctic ships", docs_url="/docs", redoc_url=None)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install(application, concentration_at=concentration_at, start_worker=start_worker)
    return application


app = create_app()
