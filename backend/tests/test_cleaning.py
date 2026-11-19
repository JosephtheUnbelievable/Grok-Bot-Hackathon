"""Cleaning rules: sentinels, the 1987-88 gap, and the single km² conversion."""

import io

import numpy as np
import tifffile

from app.cleaning import (
    is_gap_day,
    is_gap_month,
    millions_to_km2,
    parse_daily_csv,
    parse_monthly_csv,
    read_concentration_tif,
    scale_concentration,
)
from datetime import date


def test_extent_converted_once_and_sentinel_dropped():
    assert millions_to_km2("7.05") == 7_050_000
    assert millions_to_km2("-9999") is None
    assert millions_to_km2("-9999.000") is None
    assert millions_to_km2("0") == 0


def test_monthly_gap_and_scale():
    text = (
        "year, mo, source_dataset, region, extent, area\n"
        "1979,  9, NSIDC-0051, N, 7.05, 4.58\n"
        "1987, 12, -9999, N, -9999, -9999\n"
        "1988,  1, -9999, N, -9999, -9999\n"
        "2024,  9, NSIDC-0051, N, 4.35, 2.91\n"
    )
    rows = {(row.year, row.month): row for row in parse_monthly_csv(text, "north")}
    assert rows[(1979, 9)].extent_km2 == 7_050_000
    assert rows[(1979, 9)].area_km2 == 4_580_000
    assert rows[(1987, 12)].gap_masked is True
    assert rows[(1987, 12)].extent_km2 is None
    assert rows[(1988, 1)].extent_km2 is None
    assert rows[(2024, 9)].extent_km2 == 4_350_000
    assert is_gap_month(1987, 12) and is_gap_month(1988, 1)
    assert not is_gap_month(1987, 9)


def test_daily_gap_is_not_interpolated_and_smr_holes_are_not_the_gap():
    text = (
        "Year, Month, Day, Extent, Missing, Source Data\n"
        "YYYY, MM, DD, 10^6 sq km, 10^6 sq km, Source\n"
        "1979, 1, 1, 14.00, 0.0, src\n"
        "1979, 1, 3, 14.10, 0.0, src\n"
        "1987, 12, 2, 13.20, 0.0, src\n"
        "1987, 12, 3, -9999, 0.0, src\n"
        "1988, 1, 13, 13.50, 0.0, src\n"
    )
    rows = parse_daily_csv(text)
    days = [row.day for row in rows]
    assert date(1979, 1, 2) not in days
    assert date(1987, 12, 3) in days
    assert next(row for row in rows if row.day == date(1987, 12, 3)).gap_masked is True
    assert next(row for row in rows if row.day == date(1987, 12, 3)).extent_km2 is None
    assert not is_gap_day(date(1987, 12, 2))
    assert is_gap_day(date(1988, 1, 12))
    assert not is_gap_day(date(1988, 1, 13))


def test_geotiff_class_values_never_survive():
    raw = np.array(
        [[0, 1000, 2510], [2530, 2540, 2550], [150, 9999, 50]],
        dtype=np.uint16,
    )
    scaled, holes = scale_concentration(raw)
    assert holes == 1
    assert scaled[0, 0] == 0
    assert scaled[0, 1] == 100
    assert scaled[0, 2] == 100
    assert scaled[1, 0] == 255
    assert scaled[1, 1] == 255
    assert scaled[1, 2] == 255
    assert scaled[2, 0] == 15
    assert scaled[2, 1] == 255
    assert scaled[2, 2] == 5
    assert 2510 not in scaled and 2530 not in scaled and -9999 not in scaled

    buf = io.BytesIO()
    tifffile.imwrite(buf, raw, photometric="minisblack")
    grid = read_concentration_tif(buf.getvalue())
    assert grid.pole_hole_cells == 1
    assert grid.width == 3 and grid.height == 3
    assert 2510 not in np.frombuffer(grid.values, dtype=np.uint8)
