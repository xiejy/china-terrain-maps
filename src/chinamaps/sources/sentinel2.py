"""Sentinel-2 quarterly cloudless mosaics (Copernicus, ESA) on source.coop.

Layout: {year}/{Qn}/{MGRS}_{i}_{j}/{B02,B03,B04,B08}.tif, int16 reflectance
x 10000, nodata -32768, ~100 km UTM tiles (COGs with overviews). Tiles are
resolved from the MGRS 100 km square of the bbox corners, confirmed by listing
and by the COG bounds; the quarter with the fewest gaps and least haze wins.
"""

from __future__ import annotations

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from rasterio.transform import Affine
from rasterio.warp import Resampling, reproject

from chinamaps.net import SOURCE_COOP, vsicurl
from chinamaps.sources.overture import Bbox, s3_list

PREFIX = "tge-labs/sentinel-2-quarterly-cloudless-mosaics"
S2_BASE = f"{SOURCE_COOP}/{PREFIX}"
NODATA = -32768
RGB = ("B04", "B03", "B02")
LAT_BANDS = "CDEFGHJKLMNPQRSTUVWX"
COL_SETS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUV"


def utm_zone(lon: float) -> int:
    return int((lon + 180) // 6) + 1


def mgrs_100k(lon: float, lat: float, zone: int | None = None) -> str:
    """MGRS grid zone + 100 km square id (e.g. '48RXT'), WGS84 'AA' lettering."""
    zone = zone or utm_zone(lon)
    epsg = (32600 if lat >= 0 else 32700) + zone
    e, n = Transformer.from_crs(4326, epsg, always_xy=True).transform(lon, lat)
    col = COL_SETS[(zone - 1) % 3][int(e // 100_000) - 1]
    row = ROW_LETTERS[(int(n // 100_000) + (5 if zone % 2 == 0 else 0)) % 20]
    band = LAT_BANDS[int((lat + 80) // 8)]
    return f"{zone}{band}{col}{row}"


def quarters() -> list[tuple[int, str]]:
    """Available (year, quarter) pairs, most recent first."""
    _, years = s3_list(SOURCE_COOP, f"{PREFIX}/", "/")
    out = []
    for y in years:
        year = y.rstrip("/").split("/")[-1]
        if not year.isdigit():
            continue
        _, qs = s3_list(SOURCE_COOP, y, "/")
        out += [(int(year), q.rstrip("/").split("/")[-1]) for q in qs]
    return sorted(out, reverse=True)


def band_url(year: int, quarter: str, tile: str, band: str) -> str:
    return f"{S2_BASE}/{year}/{quarter}/{tile}/{band}.tif"


def resolve_tiles(bbox: Bbox, epsg: int, year: int, quarter: str, hint: list[str] | None = None) -> list[str]:
    """Tile directories ({MGRS}_{i}_{j}) in the city's UTM zone that intersect bbox."""
    zone = int(str(epsg)[-2:])
    if hint:
        ids = set(hint)
    else:
        corners = [(bbox[0], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[1]), (bbox[2], bbox[3])]
        ids = {mgrs_100k(lon, lat, zone) for lon, lat in corners}
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True)
    xs, ys = to_utm.transform([bbox[0], bbox[0], bbox[2], bbox[2]], [bbox[1], bbox[3], bbox[1], bbox[3]])
    want = (min(xs), min(ys), max(xs), max(ys))
    tiles = []
    for mgrs in sorted(ids):
        _, dirs = s3_list(SOURCE_COOP, f"{PREFIX}/{year}/{quarter}/{mgrs}_", "/")
        for d in dirs:
            tile = d.rstrip("/").split("/")[-1]
            with rasterio.open(vsicurl(band_url(year, quarter, tile, "B04"))) as ds:
                if CRS.from_user_input(ds.crs).to_epsg() != epsg:
                    continue
                b = ds.bounds
            if b.left <= want[2] and b.right >= want[0] and b.bottom <= want[3] and b.top >= want[1]:
                tiles.append(tile)
    return tiles


def read_bands(year: int, quarter: str, tiles: list[str], crs: str, transform: Affine, shape: tuple[int, int],
               bands: tuple[str, ...] = RGB, resampling: Resampling = Resampling.cubic) -> np.ndarray:
    """(len(bands), H, W) float32 reflectance on the target grid; NaN = nodata."""
    out = np.full((len(bands), *shape), np.nan, np.float32)
    for tile in tiles:
        for i, band in enumerate(bands):
            buf = np.full(shape, np.nan, np.float32)
            with rasterio.open(vsicurl(band_url(year, quarter, tile, band))) as ds:
                reproject(rasterio.band(ds, 1), buf, src_nodata=NODATA, dst_transform=transform, dst_crs=crs,
                          dst_nodata=np.nan, resampling=resampling)
            fill = np.isnan(out[i])
            out[i][fill] = buf[fill] * 1e-4
    return out


def quality(rgb: np.ndarray, water: np.ndarray | None = None) -> dict:
    """Score an RGB stack (lower is better).

    haze: median land blue reflectance (haze and thin cloud brighten it).
    blotch: 5-95 percentile spread of blue over water, which should be
    uniform; residual cloud, haze patches and seam artifacts widen it.
    """
    valid = np.isfinite(rgb).all(axis=0)
    nodata = float(1 - valid.mean())
    if valid.mean() < 0.5:
        return {"nodata": nodata, "haze": 1.0, "blotch": 1.0, "score": 99.0}
    blue = rgb[2]
    water = np.zeros(valid.shape, bool) if water is None else water
    land, wat = valid & ~water, valid & water
    haze = float(np.median(blue[land])) if land.any() else 0.06
    blotch = float(np.subtract(*np.percentile(blue[wat], [95, 5]))) if wat.sum() > 100 else 0.02
    return {"nodata": nodata, "haze": haze, "blotch": blotch,
            "score": 10 * nodata + haze / 0.06 + blotch / 0.02}


def choose_quarter(bbox: Bbox, epsg: int, transform: Affine, shape: tuple[int, int],
                   water: np.ndarray | None = None, n: int = 8, tol: float = 0.35,
                   hint: list[str] | None = None) -> tuple[tuple[int, str], list[str], dict]:
    """Most recent of the last n quarters scoring within tol of the best.

    transform/shape should be a coarse grid (e.g. 20 m) over the city; water
    is a bool mask on that grid. Returns ((year, quarter), tiles, scores).
    """
    scores, tiles_by_q = {}, {}
    for year, q in quarters()[:n]:
        tiles = resolve_tiles(bbox, epsg, year, q, hint)
        if not tiles:
            continue
        rgb = read_bands(year, q, tiles, f"EPSG:{epsg}", transform, shape, resampling=Resampling.average)
        scores[f"{year}-{q}"] = quality(rgb, water)
        tiles_by_q[(year, q)] = tiles
    best = min(s["score"] for s in scores.values())
    for (year, q), tiles in tiles_by_q.items():  # most recent first
        if scores[f"{year}-{q}"]["score"] <= best + tol:
            return (year, q), tiles, scores
    raise RuntimeError("no Sentinel-2 quarter")


def true_color(rgb: np.ndarray, lo: float = 2, hi: float = 98, gamma: float = 0.9, saturation: float = 1.15,
               mask: np.ndarray | None = None) -> np.ndarray:
    """Per-band percentile stretch -> gamma -> saturation boost -> uint8 (H, W, 3).

    mask (optional, bool H x W) limits the pixels the percentiles are taken from.
    """
    out = np.empty(rgb.shape[1:] + (3,), np.float32)
    sel = np.isfinite(rgb).all(axis=0) if mask is None else mask & np.isfinite(rgb).all(axis=0)
    for i in range(3):
        a, b = np.percentile(rgb[i][sel], [lo, hi])
        out[..., i] = np.clip((rgb[i] - a) / max(b - a, 1e-6), 0, 1)
    out = out ** gamma
    grey = out.mean(axis=2, keepdims=True)
    out = np.clip(grey + (out - grey) * saturation, 0, 1)
    out[~np.isfinite(out)] = 0.5
    return (out * 255 + 0.5).astype(np.uint8)
