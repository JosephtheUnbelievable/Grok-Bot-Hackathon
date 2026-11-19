"""Turn NSIDC CSV and GeoTIFF bytes into masked, unit-converted values.

Extent in the Sea Ice Index CSVs is millions of km². Multiply by 1e6 once.
Sentinel −9999 and GeoTIFF class flags never leave this module as concentrations.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

# Instrument gap verified in G02135 v4. Do not interpolate these.
GAP_MONTHS = {(1987, 12), (1988, 1)}
GAP_DAY_START = date(1987, 12, 3)
GAP_DAY_END = date(1988, 1, 12)

SENTINEL = -9999.0
POLE_HOLE = 2510
MASK_CLASSES = (2530, 2540, 2550)


def is_gap_month(year: int, month: int) -> bool:
    return (year, month) in GAP_MONTHS


def is_gap_day(day: date) -> bool:
    return GAP_DAY_START <= day <= GAP_DAY_END


def is_sentinel(value: float) -> bool:
    return value <= -9000 or abs(value - SENTINEL) < 0.1


def millions_to_km2(raw: str | float) -> int | None:
    """Convert one CSV extent or area cell. Returns km², or None if empty/sentinel."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "" or text.lower() in {"na", "nan", "none"}:
        return None
    value = float(text)
    if is_sentinel(value):
        return None
    # One conversion. Round half-up so 7.05 million km² becomes 7050000, not a binary artifact.
    return int(np.floor(value * 1_000_000 + 0.5))


def scale_concentration(raw: np.ndarray) -> tuple[np.ndarray, int]:
    """Map a uint16 concentration raster to uint8 percent. Returns (grid, pole_hole_cells)."""
    src = np.asarray(raw)
    out = np.full(src.shape, 255, dtype=np.uint8)
    valid = (src >= 0) & (src <= 1000)
    if valid.any():
        scaled = np.floor(src[valid].astype(np.float64) / 10.0 + 0.5).astype(np.int32)
        scaled = np.clip(scaled, 0, 100)
        out[valid] = scaled.astype(np.uint8)
    hole = src == POLE_HOLE
    out[hole] = 100
    # 2530 coast, 2540 land, 2550 missing, and anything else stay 255.
    return out, int(hole.sum())


@dataclass(frozen=True)
class ExtentRecord:
    year: int
    month: int
    extent_km2: int | None
    area_km2: int | None
    gap_masked: bool


def parse_monthly_csv(text: str, pole: str) -> list[ExtentRecord]:
    """Parse one calendar-month extent file. Gap months are forced to null."""
    sample = text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(sample))
    try:
        header = next(reader)
    except StopIteration:
        return []
    names = [cell.strip().lower() for cell in header]
    def idx(*options: str) -> int | None:
        for name in options:
            if name in names:
                return names.index(name)
        return None

    year_i = idx("year")
    month_i = idx("mo", "month")
    extent_i = idx("extent")
    area_i = idx("area")
    region_i = idx("region")
    if year_i is None or extent_i is None:
        return []
    want = "N" if pole == "north" else "S"
    rows: list[ExtentRecord] = []
    for cells in reader:
        if not cells or all(not c.strip() for c in cells):
            continue
        try:
            year = int(cells[year_i].strip())
            month = int(cells[month_i].strip()) if month_i is not None else 0
        except (ValueError, IndexError):
            continue
        if region_i is not None and region_i < len(cells):
            region = cells[region_i].strip().upper()
            if region and region not in {want, pole.upper()}:
                continue
        try:
            extent = millions_to_km2(cells[extent_i]) if extent_i < len(cells) else None
        except ValueError:
            extent = None
        area = None
        if area_i is not None and area_i < len(cells):
            try:
                area = millions_to_km2(cells[area_i])
            except ValueError:
                area = None
        gap = is_gap_month(year, month)
        if gap:
            extent = None
            area = None
        rows.append(ExtentRecord(year, month, extent, area, gap))
    return rows


@dataclass(frozen=True)
class DailyRecord:
    day: date
    extent_km2: int | None
    gap_masked: bool


def parse_daily_csv(text: str) -> list[DailyRecord]:
    """Parse a daily extent file. Two header rows. Sentinel rows and blank rows are dropped.

    Missing dates (SMMR every-other-day sampling, and the 1987-12-03..1988-01-12 gap)
    are left absent. Nothing is interpolated.
    """
    lines = text.lstrip("\ufeff").splitlines()
    start = 0
    # Skip preamble rows until a data row whose first cell is a year.
    for i, line in enumerate(lines):
        cell = line.split(",", 1)[0].strip()
        if cell.isdigit() and 1978 <= int(cell) <= 2100:
            start = i
            break
    reader = csv.reader(lines[start:])
    rows: list[DailyRecord] = []
    for cells in reader:
        if len(cells) < 4:
            continue
        try:
            year = int(cells[0].strip())
            month = int(cells[1].strip())
            day = int(cells[2].strip())
            extent = millions_to_km2(cells[3])
        except ValueError:
            continue
        if not (1978 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
            continue
        try:
            stamp = date(year, month, day)
        except ValueError:
            continue
        gap = is_gap_day(stamp)
        if extent is None and not gap:
            # A −9999 daily row is a missing day, not a zero. Drop it.
            continue
        if gap:
            # The v4 files omit these dates. If one appears with a sentinel, keep it flagged.
            rows.append(DailyRecord(stamp, None, True))
            continue
        rows.append(DailyRecord(stamp, extent, False))
    return rows


def newest_daily(rows: list[DailyRecord]) -> date | None:
    days = [row.day for row in rows if row.extent_km2 is not None and not row.gap_masked]
    return max(days) if days else None


@dataclass
class CleanGrid:
    width: int
    height: int
    values: bytes
    projection: str
    epsg: int | None
    nominal_resolution_m: float
    pole_hole_cells: int
    origin_x: float | None
    origin_y: float | None
    pixel_size_x: float | None
    pixel_size_y: float | None

    def array(self) -> np.ndarray:
        return np.frombuffer(self.values, dtype=np.uint8).reshape(self.height, self.width).copy()


def _epsg_from_geokeys(values) -> int | None:
    if values is None:
        return None
    keys = [int(v) for v in values]
    if len(keys) < 4:
        return None
    count = keys[3]
    for i in range(count):
        base = 4 + i * 4
        if base + 3 >= len(keys):
            break
        key_id, location, _count, value = keys[base : base + 4]
        if key_id == 3072 and location == 0 and value > 0:
            return value
    return None


def read_concentration_tif(data: bytes) -> CleanGrid:
    import tifffile

    with tifffile.TiffFile(io.BytesIO(data)) as tif:
        page = tif.pages[0]
        raw = page.asarray()
        scale = page.tags.get("ModelPixelScaleTag")
        tie = page.tags.get("ModelTiepointTag")
        geokeys = page.tags.get("GeoKeyDirectoryTag")
        citation = page.tags.get("GeoAsciiParamsTag")
    grid, holes = scale_concentration(raw)
    if grid.ndim != 2:
        grid = np.squeeze(grid)
        if grid.ndim != 2:
            raise ValueError("concentration raster is not 2-D")
    height, width = grid.shape
    epsg = _epsg_from_geokeys(geokeys.value if geokeys is not None else None)
    projection = f"EPSG:{epsg}" if epsg else "unknown"
    if not epsg and citation is not None:
        text = str(citation.value).split("|", 1)[0].strip()
        if text:
            projection = text
    origin_x = origin_y = pixel_x = pixel_y = None
    resolution = 25000.0
    if scale is not None and len(scale.value) >= 2:
        pixel_x = float(scale.value[0])
        pixel_y = abs(float(scale.value[1]))
        if pixel_x:
            resolution = abs(pixel_x)
    if tie is not None and len(tie.value) >= 6:
        origin_x = float(tie.value[3])
        origin_y = float(tie.value[4])
    return CleanGrid(
        width=int(width),
        height=int(height),
        values=np.ascontiguousarray(grid).tobytes(),
        projection=projection,
        epsg=epsg,
        nominal_resolution_m=float(resolution),
        pole_hole_cells=holes,
        origin_x=origin_x,
        origin_y=origin_y,
        pixel_size_x=pixel_x,
        pixel_size_y=pixel_y,
    )


def month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1)
