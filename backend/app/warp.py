"""Equirectangular 1024×512 RGBA overlay from a polar-stereographic concentration grid.

Nearest neighbour. North is the top row, longitude −180 is the left column,
matching the GIBS WMS bbox −90,−180,90,180. Open water (< 15%) and masked cells
are transparent. Ice mixes #5dade2 at 15% to #ffffff at 100%.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from app.cleaning import CleanGrid

WIDTH = 1024
HEIGHT = 512

# EPSG codes NSIDC actually writes, plus the user-guide names.
# Hughes 1980: a = 6378273 m, b = 6356889.449 m. WGS84 otherwise.
_HUGHES = (6378273.0, 6356889.449)
_WGS84 = (6378137.0, 6356752.314245)
PROJECTIONS = {
    3411: {"north": True, "lat_ts": 70.0, "lon0": -45.0, "ellipsoid": _HUGHES},
    3412: {"north": False, "lat_ts": -70.0, "lon0": 0.0, "ellipsoid": _HUGHES},
    3413: {"north": True, "lat_ts": 70.0, "lon0": -45.0, "ellipsoid": _WGS84},
    3031: {"north": False, "lat_ts": -71.0, "lon0": 0.0, "ellipsoid": _WGS84},
}


def _forward(lat_deg, lon_deg, spec):
    a, b = spec["ellipsoid"]
    e2 = 1.0 - (b * b) / (a * a)
    e = np.sqrt(e2)
    north = spec["north"]
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg - spec["lon0"])
    lat_ts = np.radians(spec["lat_ts"])

    def t_of(phi):
        s = np.sin(phi)
        if north:
            return np.tan(np.pi / 4 - phi / 2) / ((1 - e * s) / (1 + e * s)) ** (e / 2)
        return np.tan(np.pi / 4 + phi / 2) / ((1 + e * s) / (1 - e * s)) ** (e / 2)

    if north:
        t = np.where(np.asarray(lat_deg) >= 89.999, 0.0, t_of(lat))
    else:
        t = np.where(np.asarray(lat_deg) <= -89.999, 0.0, t_of(lat))
    t_c = float(t_of(np.array(spec["lat_ts"])))
    m_c = np.cos(lat_ts) / np.sqrt(1 - e2 * np.sin(lat_ts) ** 2)
    rho = a * m_c * t / t_c
    x = rho * np.sin(lon)
    y = (-rho if north else rho) * np.cos(lon)
    return x, y


def rgba_from_grid(grid: CleanGrid) -> np.ndarray:
    """Return (512, 1024, 4) uint8. Raises if the raster has no georeferencing."""
    if grid.origin_x is None or grid.origin_y is None or not grid.pixel_size_x or not grid.pixel_size_y:
        raise ValueError("geotiff has no pixel scale")
    spec = PROJECTIONS.get(grid.epsg or 0)
    if spec is None:
        # Trust the hemisphere of a known code; otherwise north uses 3413-style WGS84.
        spec = PROJECTIONS[3413 if (grid.epsg or 3413) != 3412 else 3412]
        if grid.epsg in (None, 0):
            spec = PROJECTIONS[3411]
    lon = -180.0 + (np.arange(WIDTH) + 0.5) * (360.0 / WIDTH)
    lat = 90.0 - (np.arange(HEIGHT) + 0.5) * (180.0 / HEIGHT)
    lon_g, lat_g = np.meshgrid(lon, lat)
    x, y = _forward(lat_g, lon_g, spec)
    col = np.rint((x - grid.origin_x) / grid.pixel_size_x).astype(np.int32)
    row = np.rint((grid.origin_y - y) / grid.pixel_size_y).astype(np.int32)
    valid = (col >= 0) & (col < grid.width) & (row >= 0) & (row < grid.height)
    src = np.frombuffer(grid.values, dtype=np.uint8).reshape(grid.height, grid.width)
    sampled = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    sampled[valid] = src[row[valid], col[valid]]
    return colorize(sampled)


def colorize(concentration: np.ndarray) -> np.ndarray:
    """Map percent (0–100, 255 masked) to RGBA. 15% is #5dade2, 100% is #ffffff."""
    conc = np.asarray(concentration)
    rgba = np.zeros(conc.shape + (4,), dtype=np.uint8)
    ice = (conc >= 15) & (conc <= 100)
    if not ice.any():
        return rgba
    t = (conc[ice].astype(np.float32) - 15.0) / 85.0
    rgba[ice, 0] = np.clip(np.rint(93 + t * (255 - 93)), 0, 255).astype(np.uint8)
    rgba[ice, 1] = np.clip(np.rint(173 + t * (255 - 173)), 0, 255).astype(np.uint8)
    rgba[ice, 2] = np.clip(np.rint(226 + t * (255 - 226)), 0, 255).astype(np.uint8)
    rgba[ice, 3] = 255
    return rgba


def png_bytes(rgba: np.ndarray) -> bytes:
    image = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def transparent_png() -> bytes:
    return png_bytes(np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8))


def render_png(grid: CleanGrid) -> bytes:
    return png_bytes(rgba_from_grid(grid))
