"""Last-good cache. Writes land via a temp file in the same directory, then rename."""

from __future__ import annotations

import json
import os
from pathlib import Path


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: dict) -> None:
    atomic_write(path, json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
