"""Load ``ships/testdata/ais_sample.jsonl`` for unit tests.

Live routes must not import this module. The returned envelope is labeled
``fixture: true`` and ``source: recorded-sample`` so it cannot be mistaken
for the AIS socket.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ships.worker import Fleet, iso_z

FIXTURE_NOTICE = "RECORDED SAMPLE — not a live AIS feed. Offline unit-test input only."
SOURCE = "recorded-sample"


def load_recorded_sample(path: str | Path, concentration_at=None, now: datetime | None = None) -> dict:
    """Replay a jsonl capture. Sets the recorded-sample flags. Not an HTTP handler."""
    fleet = Fleet()
    newest = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            fleet.decode_errors += 1
            continue
        fleet.ingest(payload)
        if fleet.last_message_at is not None and (newest is None or fleet.last_message_at > newest):
            newest = fleet.last_message_at
    # Freeze the clock on the capture so a later wall time does not forget the ships.
    moment = now or newest or datetime.now().astimezone()
    # Not a live route: replay even when AISSTREAM_API_KEY is unset, then label it.
    body = fleet.ships_payload(concentration_at=concentration_at, now=moment, require_key=False)
    body["fixture"] = True
    body["source"] = SOURCE
    body["fixture_notice"] = FIXTURE_NOTICE
    body["using_sample"] = True
    body["generated_at"] = iso_z(moment)
    return body
