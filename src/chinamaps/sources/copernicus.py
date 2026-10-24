"""Copernicus DEM GLO-30 / GLO-90 1x1 degree COG tiles (AWS open data, unsigned https).

Tiles are read downsampled (internal overview + average resampling) to the
pixels-per-degree the scene needs and cached as small GeoTIFFs under
data/copernicus/<product>/<ppd>/. Ocean cells have no tile.

Each tile is pixel-is-point: its pixel grid starts half a native pixel west and
north of the integer degree, and adjacent tiles abut without overlap.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from chinamaps.net import DATA_DIR, download, vsicurl

CACHE = DATA_DIR / "copernicus"


@dataclass(frozen=True)
class Product:
    bucket: str
    code: str  # "10" (1 arcsec) or "30" (3 arcsec) in tile names
    ppd: int  # native pixels per degree (latitude)
    max_ppd: int  # finest level this product name may use

    @property
    def levels(self) -> tuple[int, ...]:
        """Pixels per degree of the full-res raster and its internal overviews."""
        n = 4 if self.ppd == 3600 else 3
        return tuple(self.ppd >> i for i in range(n))


PRODUCTS = {
    "glo30": Product("copernicus-dem-30m", "10", 3600, 3600),
    "glo30-ovr2": Product("copernicus-dem-30m", "10", 3600, 450),
    "glo90": Product("copernicus-dem-90m", "30", 1200, 1200),
}


def product(name: str) -> Product:
    try:
        return PRODUCTS[name]
    except KeyError:
        raise ValueError(f"unknown DEM product {name!r}; expected one of {sorted(PRODUCTS)}") from None


def tile_id(lat: int, lon: int, prod: Product) -> str:
    """Tile name for the cell whose south-west corner is (lat, lon)."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_{prod.code}_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def tile_url(lat: int, lon: int, prod: Product) -> str:
    tid = tile_id(lat, lon, prod)
    return f"https://{prod.bucket}.s3.amazonaws.com/{tid}/{tid}.tif"


@lru_cache(maxsize=None)
def tile_list(bucket: str) -> frozenset[str]:
    """All tile ids in a bucket (cached tileList.txt)."""
    path = download(f"https://{bucket}.s3.amazonaws.com/tileList.txt", CACHE / f"tileList-{bucket}.txt")
    return frozenset(path.read_text().split())


def cells_in_bbox(lon0: float, lat0: float, lon1: float, lat1: float) -> list[tuple[int, int]]:
    """(lat, lon) south-west corners of all 1-degree cells touching the bbox."""
    las = range(math.floor(lat0), math.ceil(lat1))
    los = range(math.floor(lon0), math.ceil(lon1))
    return [(la, lo) for la in las for lo in los]


def existing_cells(cells: list[tuple[int, int]], prod: Product) -> list[tuple[int, int]]:
    ids = tile_list(prod.bucket)
    return [c for c in cells if tile_id(c[0], c[1], prod) in ids]


def choose_ppd(prod: Product, ppd_needed: float) -> int:
    """Coarsest pixels-per-degree at least as fine as needed: an available level
    (capped at the product's max_ppd) averaged down by an integer factor."""
    levels = [p for p in prod.levels if p <= prod.max_ppd]
    ok = [p for p in levels if p >= ppd_needed * 0.95]
    level_ppd = min(ok) if ok else max(levels)
    k = max(1, int(level_ppd // max(ppd_needed, 1e-9)))
    while level_ppd % k:
        k -= 1
    return level_ppd // k


def read_plan(prod: Product, ppd: int) -> tuple[int, int]:
    """(OVERVIEW_LEVEL or -1 for full res, averaging factor) that yields ppd."""
    level_ppd = min(p for p in prod.levels if p >= ppd and p % ppd == 0)
    return prod.levels.index(level_ppd) - 1, level_ppd // ppd


def cache_path(lat: int, lon: int, prod: Product, ppd: int) -> Path:
    return CACHE / prod.bucket / str(ppd) / f"{tile_id(lat, lon, prod)}.tif"


def _fetch(lat: int, lon: int, prod: Product, ppd: int) -> Path:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    from scipy.ndimage import zoom

    dest = cache_path(lat, lon, prod, ppd)
    if dest.exists():
        return dest
    ovr, k = read_plan(prod, ppd)
    kw = {"OVERVIEW_LEVEL": ovr} if ovr >= 0 else {}
    with rasterio.open(vsicurl(tile_url(lat, lon, prod)), **kw) as ds:
        h, w = ds.height // k, ds.width // k
        a = ds.read(1, out_shape=(h, w), resampling=Resampling.average if k > 1 else Resampling.nearest)
        left, top = ds.bounds.left, ds.bounds.top
    if w != h:  # tiles above 50N are narrower in longitude
        a = zoom(a, (1.0, h / w), order=1, grid_mode=True, mode="nearest")
    a = a.astype(np.float32)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part.tif")
    profile = dict(driver="GTiff", width=h, height=h, count=1, dtype="float32", crs="EPSG:4326",
                   transform=from_origin(left, top, 1.0 / ppd, 1.0 / ppd), compress="deflate", predictor=3,
                   tiled=h >= 512, **({"blockxsize": 512, "blockysize": 512} if h >= 512 else {}))
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(a, 1)
    tmp.replace(dest)
    return dest


def _cached_levels(prod: Product, lo: int, hi: int) -> list[int]:
    root = CACHE / prod.bucket
    if not root.exists():
        return []
    return sorted(int(d.name) for d in root.iterdir() if d.name.isdigit() and lo <= int(d.name) <= hi)


def fetch_tiles(cells: list[tuple[int, int]], prod: Product, ppd_needed: float, workers: int = 8,
                progress: bool = True) -> tuple[int, dict[tuple[int, int], Path]]:
    """Download (or reuse) every existing tile among cells. Returns (ppd, {cell: path}).

    A mostly complete cache at up to 2x finer resolution is reused and topped up.
    """
    ppd = choose_ppd(prod, ppd_needed)
    cells = existing_cells(cells, prod)
    for p in _cached_levels(prod, ppd + 1, min(2 * ppd, prod.max_ppd)):
        have = sum(cache_path(c[0], c[1], prod, p).exists() for c in cells)
        if have >= 0.9 * len(cells):
            ppd = p
            break
    todo = [c for c in cells if not cache_path(c[0], c[1], prod, ppd).exists()]
    if todo and progress:
        print(f"copernicus: fetching {len(todo)}/{len(cells)} {prod.bucket} tiles at {ppd} px/deg", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda c: _fetch(c[0], c[1], prod, ppd), todo))
    return ppd, {c: cache_path(c[0], c[1], prod, ppd) for c in cells}


def mosaic(paths: dict[tuple[int, int], Path], cells: list[tuple[int, int]], ppd: int, prod: Product):
    """Mosaic cached tiles covering the given cells. Missing cells are NaN.

    Returns (array float32, affine transform in EPSG:4326).
    """
    from affine import Affine

    import rasterio

    la0 = min(c[0] for c in cells)
    la1 = max(c[0] for c in cells) + 1
    lo0 = min(c[1] for c in cells)
    lo1 = max(c[1] for c in cells) + 1
    out = np.full(((la1 - la0) * ppd, (lo1 - lo0) * ppd), np.nan, dtype=np.float32)
    for lat, lon in cells:
        p = paths.get((lat, lon))
        if p is None:
            continue
        with rasterio.open(p) as ds:
            r = (la1 - 1 - lat) * ppd
            c = (lon - lo0) * ppd
            out[r : r + ppd, c : c + ppd] = ds.read(1)
    half = 0.5 / prod.ppd
    return out, Affine(1.0 / ppd, 0.0, lo0 - half, 0.0, -1.0 / ppd, la1 + half)
