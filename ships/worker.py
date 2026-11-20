"""AISStream client, message cleaning, and the in-memory fleet.

The recorded sample in ``testdata/`` is not read here. ``ships.sample`` is the
only loader, and the HTTP routes do not import it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ships.ice import assess, icebreaker_name

logger = logging.getLogger("ships")

WS_URL = "wss://stream.aisstream.io/v0/stream"
BACKOFF_SECONDS = (1, 2, 4, 8, 16, 30)
POSITION_TYPES = (
    "PositionReport",
    "StandardClassBPositionReport",
    "ExtendedClassBPositionReport",
)
STATIC_TYPES = ("ShipStaticData",)
TRACK_LIMIT = 12
FORGET_AFTER = timedelta(hours=6)
SHIP_STALE_AFTER = timedelta(minutes=15)
FEED_SILENCE = timedelta(seconds=60)
POSITION_DECIMALS = 0.0001
BBOX = {"min_lat": 60.0, "max_lat": 90.0, "min_lon": -180.0, "max_lon": 180.0}

ConcentrationAt = Callable[[float, float], float | None]


def backoff_seconds(attempt: int) -> float:
    """Reconnect delay for this attempt. Stays at 30 s. Does not raise."""
    try:
        index = int(attempt)
    except (TypeError, ValueError):
        index = 0
    if index < 0:
        index = 0
    if index >= len(BACKOFF_SECONDS):
        return float(BACKOFF_SECONDS[-1])
    return float(BACKOFF_SECONDS[index])


def jittered(delay: float, roll: float | None = None) -> float:
    """Add up to 25% jitter. ``roll`` is in 0..1 so tests can pin it."""
    try:
        base = float(delay)
    except (TypeError, ValueError):
        base = 1.0
    if base < 0:
        base = 0.0
    if roll is None:
        roll = random.random()
    try:
        fraction = float(roll)
    except (TypeError, ValueError):
        fraction = 0.0
    fraction = min(1.0, max(0.0, fraction))
    return base * (1.0 + 0.25 * fraction)


def bounding_boxes(split: bool = False) -> list:
    """One Arctic box, or six 60° longitude slices if the provider rejects it."""
    if not split:
        return [[[60.0, -180.0], [90.0, 180.0]]]
    return [[[60.0, float(lon)], [90.0, float(lon + 60)]] for lon in range(-180, 180, 60)]


def subscription_message(api_key: str, split: bool = False) -> dict:
    return {
        "APIKey": api_key,
        "BoundingBoxes": bounding_boxes(split),
        "FilterMessageTypes": [
            "PositionReport",
            "StandardClassBPositionReport",
            "ExtendedClassBPositionReport",
            "ShipStaticData",
        ],
    }


def api_key_configured() -> bool:
    return bool(os.environ.get("AISSTREAM_API_KEY", "").strip())


def gfw_token() -> str:
    return os.environ.get("GFW_API_TOKEN", "").strip()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value) -> datetime | None:
    """AISStream ``time_utc`` (``YYYY-MM-DD HH:MM:SS.fff +0000 UTC``) or ISO-8601."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if len(text) < 19:
        return None
    day = text[0:10]
    clock = text[11:19]
    if day[4] != "-" or day[7] != "-" or clock[2] != ":" or clock[5] != ":":
        return None
    try:
        parsed = datetime.strptime(day + " " + clock, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def clean_name(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().rstrip("@").strip()
    return text or None


def valid_mmsi(value) -> str | None:
    """9-digit ship station. First digit 2–7. No zero-padding of shorter ids."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int):
        text = str(value)
    else:
        text = str(value).strip()
    if len(text) != 9 or not text.isdigit():
        return None
    if text[0] not in "234567":
        return None
    return text


def vessel_group(code) -> tuple[str, int | None]:
    if code is None or isinstance(code, bool):
        return "unknown", None
    try:
        number = int(code)
    except (TypeError, ValueError):
        return "unknown", None
    if number == 30:
        group = "fishing"
    elif number in (31, 32, 52):
        group = "tug"
    elif number in (36, 37):
        group = "pleasure"
    elif 40 <= number <= 49:
        group = "high_speed"
    elif 60 <= number <= 69:
        group = "passenger"
    elif 70 <= number <= 79:
        group = "cargo"
    elif 80 <= number <= 89:
        group = "tanker"
    elif 1 <= number <= 99:
        group = "special"
    else:
        return "unknown", None
    return group, number


def _as_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if not math.isfinite(number):
        return None
    return number


def _same_position(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> bool:
    return abs(lat_a - lat_b) <= POSITION_DECIMALS and abs(lon_a - lon_b) <= POSITION_DECIMALS


def _position_rejected(lat: float, lon: float) -> bool:
    if abs(lat) < 1e-9 and abs(lon) < 1e-9:
        return True
    if lat < -90 or lat > 90 or lon < -180 or lon > 180:
        return True
    if lat < 60:
        return True
    return False


def _speed_rejected(sog: float | None) -> bool:
    """True when SOG is a real speed above 50 kn and below the 102.2 sentinel."""
    if sog is None:
        return False
    tenths = round(sog * 10)
    if tenths in (1022, 1023):
        return False
    return sog > 50 and sog < 102.2


def _stored_sog(sog: float | None) -> float | None:
    if sog is None:
        return None
    if round(sog * 10) in (1022, 1023):
        return None
    return sog


def _stored_cog(cog: float | None) -> float | None:
    if cog is None:
        return None
    if abs(cog - 360.0) <= 0.05:
        return None
    return cog


def _stored_heading(heading: float | None) -> int | None:
    if heading is None:
        return None
    if int(round(heading)) == 511:
        return None
    return int(round(heading))


@dataclass
class _Parsed:
    kind: str
    mmsi: str | None = None
    when: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    sog: float | None = None
    cog: float | None = None
    heading: float | None = None
    name: str | None = None
    type_code: int | None = None
    has_type: bool = False


@dataclass
class _Vessel:
    mmsi: str
    name: str | None = None
    vessel_type: str = "unknown"
    vessel_type_code: int | None = None
    lat: float | None = None
    lon: float | None = None
    sog_kn: float | None = None
    cog_deg: float | None = None
    heading: int | None = None
    last_seen: datetime | None = None
    track: list[dict] = field(default_factory=list)


def parse_ais(payload: dict) -> _Parsed | None:
    """Turn one AISStream frame into a position or static update.

    Returns None when a required field is missing (the caller counts a decode
    error). Returns kind ``drop`` when the frame is well-formed but must not
    move a ship.
    """
    if not isinstance(payload, dict):
        return None
    meta = payload.get("MetaData") or payload.get("Metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    message = payload.get("Message") or {}
    if not isinstance(message, dict):
        message = {}
    message_type = payload.get("MessageType")
    if not isinstance(message_type, str) or not message_type:
        keys = [key for key in message.keys() if isinstance(key, str)]
        message_type = keys[0] if len(keys) == 1 else ""
    body = message.get(message_type) if message_type else None
    if not isinstance(body, dict):
        body = {}
    mmsi = valid_mmsi(meta.get("MMSI"))
    if mmsi is None:
        mmsi = valid_mmsi(body.get("UserID"))
    when = parse_time(meta.get("time_utc"))
    if mmsi is None or when is None:
        # Invalid MMSI is a cleaning drop when the id is present but illegal,
        # and a decode error when the id is missing entirely.
        raw_id = meta.get("MMSI", body.get("UserID"))
        if raw_id is None or when is None:
            return None
        return _Parsed(kind="drop")
    if message_type in STATIC_TYPES or (body.get("Type") is not None and "Latitude" not in body and message_type not in POSITION_TYPES):
        name = clean_name(body.get("Name"))
        if name is None:
            name = clean_name(meta.get("ShipName"))
        has_type = body.get("Type") is not None
        return _Parsed(kind="static", mmsi=mmsi, when=when, name=name, type_code=body.get("Type"), has_type=has_type)
    if message_type not in POSITION_TYPES and "Latitude" not in body:
        return None
    if body.get("Valid") is False:
        return _Parsed(kind="drop")
    lat = _as_float(body.get("Latitude"))
    lon = _as_float(body.get("Longitude"))
    if lat is None or lon is None:
        return None
    sog = _as_float(body.get("Sog"))
    if _position_rejected(lat, lon) or _speed_rejected(sog):
        return _Parsed(kind="drop", mmsi=mmsi, when=when)
    name = clean_name(body.get("Name"))
    if name is None:
        name = clean_name(meta.get("ShipName"))
    return _Parsed(
        kind="position",
        mmsi=mmsi,
        when=when,
        lat=lat,
        lon=lon,
        sog=sog,
        cog=_as_float(body.get("Cog")),
        heading=_as_float(body.get("TrueHeading")),
        name=name,
    )


def _is_box_rejection(payload: dict) -> bool:
    if payload.get("MessageType"):
        return False
    parts = []
    for key in ("error", "Error", "message", "Message", "detail"):
        if key in payload:
            parts.append(str(payload.get(key)))
    blob = " ".join(parts).lower()
    return "bounding" in blob or "bbox" in blob


class Fleet:
    """Latest position per MMSI. Safe to read from request threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._vessels: dict[str, _Vessel] = {}
        self.connected = False
        self.connected_at: datetime | None = None
        self.last_rx_at: datetime | None = None
        self.last_message_at: datetime | None = None
        self.reconnect_attempt = 0
        self.decode_errors = 0
        self.gfw_state = "off"
        self._ever_stored = False

    def mark_connected(self, now: datetime | None = None) -> None:
        with self._lock:
            self.connected = True
            self.connected_at = now or utcnow()
            self.reconnect_attempt = 0

    def mark_disconnected(self) -> None:
        with self._lock:
            self.connected = False
            self.connected_at = None

    def note_rx(self, now: datetime | None = None) -> None:
        with self._lock:
            self.last_rx_at = now or utcnow()

    def consume(self, raw) -> str | None:
        """Parse one websocket frame. Returns ``resplit`` when the box was rejected."""
        self.note_rx()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeError):
            self._bump_decode()
            return None
        if not isinstance(payload, dict):
            self._bump_decode()
            return None
        if _is_box_rejection(payload):
            return "resplit"
        if "error" in payload and "MessageType" not in payload:
            self._bump_decode()
            return None
        try:
            self.ingest(payload)
        except Exception:
            logger.warning("ais frame skipped")
            self._bump_decode()
        return None

    def ingest(self, payload: dict, now: datetime | None = None) -> str:
        """Apply one decoded object. Returns ``accept``, ``drop``, or ``error``."""
        parsed = parse_ais(payload)
        if parsed is None:
            self._bump_decode()
            return "error"
        if parsed.kind == "drop":
            return "drop"
        if parsed.kind == "static":
            self._apply_static(parsed)
            return "accept"
        if parsed.kind == "position":
            return "accept" if self._apply_position(parsed) else "drop"
        self._bump_decode()
        return "error"

    def _apply_static(self, parsed: _Parsed) -> None:
        with self._lock:
            vessel = self._vessels.get(parsed.mmsi)
            if vessel is not None and vessel.last_seen is not None and parsed.when < vessel.last_seen:
                return
            if vessel is None:
                vessel = _Vessel(mmsi=parsed.mmsi)
                self._vessels[parsed.mmsi] = vessel
            if parsed.name:
                vessel.name = parsed.name
            if parsed.has_type:
                group, code = vessel_group(parsed.type_code)
                vessel.vessel_type = group
                vessel.vessel_type_code = code

    def _apply_position(self, parsed: _Parsed) -> bool:
        with self._lock:
            vessel = self._vessels.get(parsed.mmsi)
            if vessel is None:
                vessel = _Vessel(mmsi=parsed.mmsi)
                self._vessels[parsed.mmsi] = vessel
            if vessel.last_seen is not None and parsed.when < vessel.last_seen:
                return False
            if (
                vessel.last_seen is not None
                and parsed.when == vessel.last_seen
                and vessel.lat is not None
                and vessel.lon is not None
                and _same_position(vessel.lat, vessel.lon, parsed.lat, parsed.lon)
            ):
                return False
            vessel.lat = parsed.lat
            vessel.lon = parsed.lon
            vessel.sog_kn = _stored_sog(parsed.sog)
            vessel.cog_deg = _stored_cog(parsed.cog)
            vessel.heading = _stored_heading(parsed.heading)
            vessel.last_seen = parsed.when
            if parsed.name and not vessel.name:
                vessel.name = parsed.name
            vessel.track.append(
                {"lat": parsed.lat, "lon": parsed.lon, "t": parsed.when, "source": "aisstream"}
            )
            _trim_track(vessel)
            self.last_message_at = parsed.when
            self._ever_stored = True
            return True

    def add_gfw_points(self, mmsi: str, points: list[dict]) -> None:
        """Append delayed history. Never copies a point into lat/lon."""
        with self._lock:
            mmsi_key = valid_mmsi(mmsi)
            vessel = None if mmsi_key is None else self._vessels.get(mmsi_key)
            if vessel is None or vessel.lat is None or vessel.last_seen is None:
                return
            for point in points:
                lat = _as_float(point.get("lat"))
                lon = _as_float(point.get("lon"))
                when = point.get("t")
                if not isinstance(when, datetime):
                    when = parse_time(when)
                if lat is None or lon is None or when is None:
                    continue
                # Delayed history may sit south of 60. Still drop null island and impossible coordinates.
                if abs(lat) < 1e-9 and abs(lon) < 1e-9:
                    continue
                if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                    continue
                if any(
                    item["source"] == "gfw"
                    and item["t"] == when
                    and _same_position(item["lat"], item["lon"], lat, lon)
                    for item in vessel.track
                ):
                    continue
                vessel.track.append({"lat": lat, "lon": lon, "t": when, "source": "gfw"})
            vessel.track.sort(key=lambda item: item["t"])
            _trim_track(vessel)

    def ships_payload(
        self,
        concentration_at: ConcentrationAt | None = None,
        now: datetime | None = None,
        require_key: bool = True,
    ) -> dict:
        now = now or utcnow()
        # Live routes keep require_key so a missing AISSTREAM_API_KEY cannot
        # leak stored positions. The recorded-sample helper opts out.
        if require_key and not api_key_configured():
            return _empty_ships(now)
        status, rows = self._rows(now)
        ships = []
        for row in rows:
            try:
                risk = assess(
                    row["lat"],
                    row["lon"],
                    row["sog_kn"],
                    row["cog_deg"],
                    row["heading"],
                    concentration_at,
                    on_error=self._bump_decode,
                )
            except Exception:
                self._bump_decode()
                risk = {
                    "level": "unknown",
                    "concentration_here": None,
                    "concentration_ahead": None,
                    "entering": False,
                }
            ships.append({**row, "icebreaker": icebreaker_name(row["name"]), "ice_risk": risk})
        return {
            "fixture": False,
            "generated_at": iso_z(now),
            "stale": status == "stale",
            "source": "aisstream-stale" if status == "stale" else "aisstream",
            "bounding_box": dict(BBOX),
            "count": len(ships),
            "ships": ships,
        }

    def health_payload(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        gfw = self._gfw_label()
        if not api_key_configured():
            return {
                "status": "down",
                "fixture": False,
                "generated_at": iso_z(now),
                "stale": False,
                "connected": False,
                "using_sample": False,
                "source": "aisstream",
                "last_message_at": None,
                "vessel_count": 0,
                "reconnect_attempt": 0,
                "decode_errors": self.decode_errors,
                "detail": _detail("AISSTREAM_API_KEY is unset", gfw),
            }
        with self._lock:
            self._forget_unlocked(now)
            status = self._status_unlocked(now)
            count = sum(1 for vessel in self._vessels.values() if vessel.lat is not None)
            connected = self.connected
            last_message = self.last_message_at
            attempt = self.reconnect_attempt
            errors = self.decode_errors
        if status == "stale":
            lead = "feed silent; serving last positions"
        elif not connected and attempt:
            lead = "reconnecting"
        else:
            lead = None
        return {
            "status": status,
            "fixture": False,
            "generated_at": iso_z(now),
            "stale": status == "stale",
            "connected": connected,
            "using_sample": False,
            "source": "aisstream-stale" if status == "stale" else "aisstream",
            "last_message_at": iso_z(last_message),
            "vessel_count": count,
            "reconnect_attempt": attempt,
            "decode_errors": errors,
            "detail": _detail(lead, gfw),
        }

    def mmsis(self) -> list[str]:
        with self._lock:
            return [mmsi for mmsi, vessel in self._vessels.items() if vessel.lat is not None]

    def _rows(self, now: datetime) -> tuple[str, list[dict]]:
        with self._lock:
            self._forget_unlocked(now)
            status = self._status_unlocked(now)
            # Envelope stale follows the feed. A ship is also stale on its own
            # clock when this fix is older than 15 minutes.
            feed_stale = status == "stale"
            rows = []
            for vessel in self._vessels.values():
                if vessel.lat is None or vessel.lon is None or vessel.last_seen is None:
                    continue
                own_stale = now - vessel.last_seen > SHIP_STALE_AFTER
                track = [
                    {"lat": item["lat"], "lon": item["lon"], "t": iso_z(item["t"]), "source": item["source"]}
                    for item in vessel.track
                ]
                rows.append(
                    {
                        "mmsi": vessel.mmsi,
                        "name": vessel.name,
                        "vessel_type": vessel.vessel_type,
                        "vessel_type_code": vessel.vessel_type_code,
                        "lat": vessel.lat,
                        "lon": vessel.lon,
                        "sog_kn": vessel.sog_kn,
                        "cog_deg": vessel.cog_deg,
                        "heading": vessel.heading,
                        "last_seen": iso_z(vessel.last_seen),
                        "is_stale": bool(feed_stale or own_stale),
                        "track": track,
                    }
                )
        rows.sort(key=lambda row: row["mmsi"])
        return status, rows

    def _status_unlocked(self, now: datetime) -> str:
        if self.connected and self.connected_at is not None:
            open_for = now - self.connected_at
            if not self._silent_unlocked(now) or open_for < FEED_SILENCE:
                return "ok"
        stored = any(vessel.lat is not None for vessel in self._vessels.values())
        silent_or_down = (not self.connected) or self._silent_unlocked(now)
        if stored and silent_or_down:
            return "stale"
        return "down"

    def _silent_unlocked(self, now: datetime) -> bool:
        latest = self.last_rx_at or self.last_message_at
        if latest is None:
            return True
        return now - latest > FEED_SILENCE

    def _forget_unlocked(self, now: datetime) -> None:
        stale_ids = [
            mmsi
            for mmsi, vessel in self._vessels.items()
            if vessel.last_seen is not None and now - vessel.last_seen > FORGET_AFTER
        ]
        for mmsi in stale_ids:
            del self._vessels[mmsi]
        # Static-only rows never received a position. Drop them on the same clock
        # so a name without a fix cannot linger.
        nameless = [mmsi for mmsi, vessel in self._vessels.items() if vessel.last_seen is None]
        if nameless and self.last_message_at is not None and now - self.last_message_at > FORGET_AFTER:
            for mmsi in nameless:
                self._vessels.pop(mmsi, None)

    def _gfw_label(self) -> str:
        if not gfw_token():
            return "off"
        return self.gfw_state if self.gfw_state in {"on", "error"} else "on"

    def _bump_decode(self) -> None:
        with self._lock:
            self.decode_errors += 1


def _trim_track(vessel: _Vessel) -> None:
    """Keep at most 12 points, oldest first, and keep the current AIS fix."""
    if len(vessel.track) <= TRACK_LIMIT:
        return
    current = None
    if vessel.last_seen is not None and vessel.lat is not None:
        for item in vessel.track:
            if (
                item["source"] == "aisstream"
                and item["t"] == vessel.last_seen
                and item["lat"] == vessel.lat
                and item["lon"] == vessel.lon
            ):
                current = item
    while len(vessel.track) > TRACK_LIMIT:
        if vessel.track[0] is current and len(vessel.track) > 1:
            del vessel.track[1]
        else:
            del vessel.track[0]


def _detail(lead: str | None, gfw: str) -> str:
    parts = []
    if lead:
        parts.append(lead)
    parts.append("arctic 60–90 N")
    parts.append(f"gfw: {gfw}")
    text = "; ".join(parts)
    for env_name in ("AISSTREAM_API_KEY", "GFW_API_TOKEN"):
        secret = os.environ.get(env_name, "").strip()
        if secret and secret in text:
            text = text.replace(secret, "[redacted]")
    return text


def _empty_ships(now: datetime) -> dict:
    return {
        "fixture": False,
        "generated_at": iso_z(now),
        "stale": False,
        "source": "aisstream",
        "bounding_box": dict(BBOX),
        "count": 0,
        "ships": [],
    }


async def run_forever(fleet: Fleet) -> None:
    """Reconnect forever. Exits immediately when ``AISSTREAM_API_KEY`` is unset."""
    if not api_key_configured():
        logger.info("AISSTREAM_API_KEY unset; ship socket not started")
        return
    import websockets
    from websockets.exceptions import ConnectionClosedOK

    attempt = 0
    split = False
    while True:
        key = os.environ.get("AISSTREAM_API_KEY", "").strip()
        if not key:
            fleet.mark_disconnected()
            return
        clean_close = False
        try:
            async with websockets.connect(
                WS_URL,
                open_timeout=20,
                ping_interval=20,
                ping_timeout=20,
                max_size=2**20,
            ) as ws:
                # The key rides in this frame only. Do not log the payload.
                await ws.send(json.dumps(subscription_message(key, split=split)))
                fleet.mark_connected()
                attempt = 0
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        action = fleet.consume(raw)
                    except Exception:
                        fleet._bump_decode()
                        action = None
                    if action == "resplit" and not split:
                        split = True
                        logger.info("aisstream rejected the wide box; resubscribing as six slices")
                        break
        except asyncio.CancelledError:
            fleet.mark_disconnected()
            raise
        except ConnectionClosedOK:
            clean_close = True
            fleet.mark_disconnected()
        except Exception:
            fleet.mark_disconnected()
            logger.warning("ais socket dropped")
        if clean_close:
            attempt = 0
            delay = jittered(backoff_seconds(0))
        else:
            delay = jittered(backoff_seconds(attempt))
            attempt += 1
            fleet.reconnect_attempt = attempt
        await asyncio.sleep(delay)


async def run_gfw(fleet: Fleet) -> None:
    """Optional delayed history. A failure here does not mark the AIS feed down.

    Public GFW positions are about 72 hours late. Points are appended to
    ``track`` with source ``gfw`` and are never copied into ``lat`` / ``lon``.
    """
    token = gfw_token()
    if not token:
        fleet.gfw_state = "off"
        return
    fleet.gfw_state = "on"
    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            while True:
                try:
                    await _gfw_once(client, headers, fleet)
                    fleet.gfw_state = "on"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    fleet.gfw_state = "error"
                    logger.warning("gfw history skipped")
                await asyncio.sleep(6 * 60 * 60)
    except asyncio.CancelledError:
        raise
    except Exception:
        fleet.gfw_state = "error"
        logger.warning("gfw history skipped")


async def _gfw_once(client, headers: dict, fleet: Fleet) -> None:
    end = utcnow() - timedelta(hours=72)
    start = end - timedelta(days=2)
    for mmsi in fleet.mmsis()[:8]:
        search = await client.get(
            "https://gateway.api.globalfishingwatch.org/v3/vessels/search",
            params={
                "query": mmsi,
                "datasets[0]": "public-global-vessel-identity:latest",
                "limit": 1,
            },
            headers=headers,
        )
        if search.status_code == 401:
            raise RuntimeError("gfw unauthorized")
        search.raise_for_status()
        body = search.json()
        entries = []
        if isinstance(body, dict):
            entries = body.get("entries") or body.get("vessels") or []
        if not entries:
            continue
        vessel_id = entries[0].get("id") or entries[0].get("vesselId")
        if not vessel_id:
            continue
        track = await client.get(
            f"https://gateway.api.globalfishingwatch.org/v3/vessels/{vessel_id}/tracks",
            params={
                "datasets[0]": "public-global-fishing-tracks:latest",
                "start-date": start.date().isoformat(),
                "end-date": end.date().isoformat(),
                "binary": "false",
            },
            headers=headers,
        )
        track.raise_for_status()
        fleet.add_gfw_points(mmsi, _gfw_points(track.json()))


def _gfw_points(payload) -> list[dict]:
    found: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            lat = node.get("lat", node.get("latitude"))
            lon = node.get("lon", node.get("longitude"))
            when = node.get("timestamp") or node.get("time") or node.get("t")
            if lat is not None and lon is not None and when is not None:
                found.append({"lat": lat, "lon": lon, "t": when})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found
