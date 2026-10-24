"""GEBCO 2026 global bathymetry (15 arcsec COG on source.coop, int16 metres).

A window is read once at the overview level matching the needed resolution and
cached as a GeoTIFF under data/gebco/.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from chinamaps.net import DATA_DIR, SOURCE_COOP, vsicurl

URL = f"{SOURCE_COOP}/ausantarctic/gebco/GEBCO_2026.tif"
NATIVE_PPD = 240
OVERVIEWS = (2, 4, 8, 16, 32, 64, 128)
CACHE = DATA_DIR / "gebco"


def choose_level(ppd_needed: float) -> tuple[int, int]:
    """(ppd, OVERVIEW_LEVEL or -1) of the coarsest level at least as fine as needed."""
    best = (NATIVE_PPD, -1)
    for i, f in enumerate(OVERVIEWS):
        if NATIVE_PPD // f >= ppd_needed * 0.95:
            best = (NATIVE_PPD // f, i)
    return best


def window(lon0: float, lat0: float, lon1: float, lat1: float, ppd_needed: float) -> Path:
    """Cached GeoTIFF of GEBCO over the bbox (snapped outward to whole degrees)."""
    import rasterio
    from rasterio.windows import from_bounds

    ppd, lvl = choose_level(ppd_needed)
    lon0, lat0 = math.floor(lon0), max(math.floor(lat0), -90)
    lon1, lat1 = math.ceil(lon1), min(math.ceil(lat1), 90)
    dest = CACHE / f"gebco2026_{ppd}ppd_{lon0}_{lat0}_{lon1}_{lat1}.tif"
    if dest.exists():
        return dest
    kw = {"OVERVIEW_LEVEL": lvl} if lvl >= 0 else {}
    print(f"gebco: reading {lon1 - lon0}x{lat1 - lat0} deg at {ppd} px/deg", flush=True)
    with rasterio.open(vsicurl(URL), **kw) as ds:
        win = from_bounds(lon0, lat0, lon1, lat1, ds.transform).round_offsets().round_lengths()
        a = ds.read(1, window=win)
        tr = ds.window_transform(win)
        nodata = ds.nodata
    a = np.where(a == nodata, 0, a).astype(np.int16)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part.tif")
    profile = dict(driver="GTiff", width=a.shape[1], height=a.shape[0], count=1, dtype="int16",
                   crs="EPSG:4326", transform=tr, compress="deflate", predictor=2, tiled=True,
                   blockxsize=512, blockysize=512)
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(a, 1)
    tmp.replace(dest)
    return dest
