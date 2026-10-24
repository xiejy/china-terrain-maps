"""GEDTM30 global bare-earth DTM (OpenLandMap, ~30 m) on source.coop.

BigTIFF int32 in decimetres (x 0.1 = metres), nodata -2147483648, EPSG:4326
at 0.00025 deg. Copernicus GLO-30 DSM is the fallback (a surface model, so it
includes buildings and canopy: only used if GEDTM30 is unreachable).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window, from_bounds

from chinamaps.net import SOURCE_COOP, vsicurl
from chinamaps.sources.overture import Bbox

GEDTM30_URL = f"{SOURCE_COOP}/walkthru-earth/dem-terrain/GEDTM30/gedtm30.tif"
GLO30_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
SCALE = 0.1


def glo30_url(lon: float, lat: float) -> str:
    la, lo = math.floor(lat), math.floor(lon)
    ns, ew = ("N" if la >= 0 else "S"), ("E" if lo >= 0 else "W")
    name = f"Copernicus_DSM_COG_10_{ns}{abs(la):02d}_00_{ew}{abs(lo):03d}_00_DEM"
    return f"{GLO30_BASE}/{name}/{name}.tif"


def _read_window(url: str, bbox: Bbox, scale: float) -> tuple[np.ndarray, Affine]:
    with rasterio.open(vsicurl(url)) as ds:
        f = from_bounds(*bbox, ds.transform)
        c0, r0 = math.floor(f.col_off), math.floor(f.row_off)
        w = Window(c0, r0, math.ceil(f.col_off + f.width) - c0, math.ceil(f.row_off + f.height) - r0)
        a = ds.read(1, window=w, boundless=False).astype(np.float32)
        nodata = ds.nodata
        tr = ds.window_transform(w)
    if nodata is not None:
        a[a == np.float32(nodata)] = np.nan
    return a * np.float32(scale), tr


def extract(bbox: Bbox, dest: Path, pad: float = 0.002) -> Path:
    """Cache the DTM window (float32 metres, NaN nodata, EPSG:4326) as GeoTIFF."""
    dest = Path(dest)
    if dest.exists():
        return dest
    b = (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
    try:
        a, tr = _read_window(GEDTM30_URL, b, SCALE)
        source = "GEDTM30"
    except rasterio.errors.RasterioIOError:
        if math.floor(b[0]) != math.floor(b[2]) or math.floor(b[1]) != math.floor(b[3]):
            raise
        a, tr = _read_window(glo30_url(b[0], b[1]), b, 1.0)
        source = "GLO-30"
    dest.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(driver="GTiff", width=a.shape[1], height=a.shape[0], count=1, dtype="float32",
                   crs="EPSG:4326", transform=tr, nodata=np.nan, compress="deflate")
    with rasterio.open(dest, "w", **profile) as ds:
        ds.write(a, 1)
        ds.update_tags(source=source)
    return dest


def to_grid(src: Path, crs: str, transform: Affine, shape: tuple[int, int],
            resampling: Resampling = Resampling.bilinear, fill: float = 0.0) -> np.ndarray:
    """Reproject the cached DTM onto a grid; nodata becomes fill."""
    out = np.full(shape, np.nan, np.float32)
    with rasterio.open(src) as ds:
        reproject(rasterio.band(ds, 1), out, dst_transform=transform, dst_crs=crs,
                  dst_nodata=np.nan, resampling=resampling)
    out[~np.isfinite(out)] = fill
    return out
