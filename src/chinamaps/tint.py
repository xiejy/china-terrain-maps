"""Hypsometric tints: cross-blended land colours (after Patterson) blended by a
smoothed climate mask (arid / warm-humid / cold-humid), darker forest on humid
slopes, snow above a latitude-dependent snowline, and an ocean depth ramp.

Colours are float32 RGB in [0, 1].
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import yaml

from chinamaps.net import REPO_ROOT

CLIMATES = ("arid", "warm_humid", "cold_humid")
FIELD_CELL = 5000.0  # m, cell size of the climate weight grid
SIGMA_M = 150_000.0


@lru_cache(maxsize=1)
def _palettes() -> dict:
    return yaml.safe_load((REPO_ROOT / "config" / "palettes.yaml").read_text())


def palettes(override: dict | None = None) -> dict:
    """config/palettes.yaml, with an optional per-scene override deep-merged over it (lists such as
    colour stops are replaced whole)."""
    import copy

    def merge(a: dict, b: dict) -> dict:
        out = dict(a)
        for k, v in b.items():
            out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
        return out

    return merge(_palettes(), override) if override else _palettes()


def stops(rows) -> tuple[np.ndarray, np.ndarray]:
    """Sort [elev, r, g, b] rows ascending: (elevations, colours in [0, 1])."""
    a = np.array(sorted(rows, key=lambda r: r[0]), dtype=np.float64)
    return a[:, 0], (a[:, 1:4] / 255.0)


def land_stops(climate: str, pal: dict | None = None) -> tuple[np.ndarray, np.ndarray]:
    pal = pal or palettes()
    return stops(pal["land"][climate] + pal["land"]["shared_high"])


def _gamma(v: np.ndarray, gamma: float) -> np.ndarray:
    return np.sign(v) * np.abs(v) ** gamma


def ramp(z: np.ndarray, elev: np.ndarray, cols: np.ndarray, gamma: float = 1.0) -> np.ndarray:
    """Piecewise-linear colour ramp in gamma-warped elevation."""
    gz = _gamma(z.astype(np.float64), gamma) if gamma != 1.0 else z
    ge = _gamma(elev, gamma) if gamma != 1.0 else elev
    out = np.empty(z.shape + (3,), dtype=np.float32)
    for k in range(3):
        out[..., k] = np.interp(gz, ge, cols[:, k])
    return out


def land_color(z: np.ndarray, weights: dict[str, np.ndarray], gamma: float = 0.7, pal: dict | None = None) -> np.ndarray:
    """Climate-blended hypsometric colour. weights: climate -> array (sums to 1)."""
    out = np.zeros(z.shape + (3,), dtype=np.float32)
    for c in CLIMATES:
        w = weights[c][..., None]
        out += w * ramp(z, *land_stops(c, pal), gamma=gamma)
    return out


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - np.float32(e0)) / np.float32(e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def snowline(lat: np.ndarray | float, warm_humid: np.ndarray | float = 0.0, pal: dict | None = None):
    """Approximate snowline (m) by latitude, lower in the monsoon south-east."""
    p = (pal or palettes())["snow"]
    tab = np.asarray(p["line_by_lat"], dtype=np.float32)
    line = np.interp(np.asarray(lat, dtype=np.float32), tab[:, 0], tab[:, 1]).astype(np.float32)
    return line - np.float32(p.get("humid_drop_m", 0)) * np.asarray(warm_humid, np.float32)


def snow_cover(z: np.ndarray, slope: np.ndarray, lat: np.ndarray, warm_humid: np.ndarray | float = 0.0,
               pal: dict | None = None, northness: np.ndarray | float = 0.0) -> np.ndarray:
    """Snow/ice weight in [0, 1]: above the snowline (lower on north-facing slopes by snow.aspect_m),
    not on cliffs."""
    p = (pal or palettes())["snow"]
    line = snowline(lat, warm_humid, pal) - np.float32(p.get("aspect_m", 0)) * np.clip(northness, 0, 1)
    w = _smoothstep(0, p["fade_m"], z - line)
    return w * (1 - _smoothstep(p["steep_deg"] - 10, p["steep_deg"] + 15, slope))


def slope_tint(col: np.ndarray, slope: np.ndarray, cfg: dict) -> np.ndarray:
    """Two-tone by slope: steep -> cfg.steep, gentle -> cfg.flat, blended over cfg.deg [lo, hi]
    (e.g. forested karst towers over pale paddy and village valley floors)."""
    t = _smoothstep(*cfg["deg"], slope)[..., None]
    target = (np.asarray(cfg["flat"], np.float32) + t * (np.asarray(cfg["steep"], np.float32)
                                                          - np.asarray(cfg["flat"], np.float32))) / 255
    return col + np.float32(cfg.get("amount", 1.0)) * (target - col)


def surface_hints(col: np.ndarray, z: np.ndarray, slope: np.ndarray, weights: dict, lat: np.ndarray,
                  pal: dict | None = None, res: float = 30.0, northness: np.ndarray | float = 0.0) -> np.ndarray:
    """Darken humid slopes toward forest green (or a slope two-tone when the palette has
    `slope_tint`), then whiten snow. slope in true degrees.

    The forest mask uses slope smoothed over ~150 m so ridge crests and narrow
    valley floors do not show as pale lines.
    """
    from scipy.ndimage import gaussian_filter

    pal = pal or palettes()
    f = pal["forest"]
    humid = 1 - weights["arid"]
    rugged = gaussian_filter(slope, float(np.clip(150.0 / res, 1.0, 4.0)), mode="nearest")
    w = (np.float32(f["amount"]) * humid * _smoothstep(*f["slope_deg"], rugged)
         * (1 - _smoothstep(*f["top_m"], z)))
    fc = np.asarray(f["color"], dtype=np.float32) / 255
    col = col + w[..., None] * (fc - col)
    if pal.get("slope_tint"):
        col = slope_tint(col, rugged if pal["slope_tint"].get("smooth", False) else slope, pal["slope_tint"])
    sw = snow_cover(z, slope, lat, weights["warm_humid"], pal, northness)[..., None]
    sc = np.asarray(pal["snow"]["color"], dtype=np.float32) / 255
    return col + sw * (sc - col)


def ocean_color(z: np.ndarray, pal: dict | None = None) -> np.ndarray:
    pal = pal or palettes()
    return ramp(np.minimum(z, 0), *stops(pal["ocean"]), gamma=0.6)


def climate_field(frame, pal: dict | None = None) -> tuple[dict[str, np.ndarray], object]:
    """Smoothed climate weights (plus a "lat" field) on a coarse grid around the frame:
    ({name: grid}, transform)."""
    import geopandas as gpd
    from rasterio.features import rasterize
    from rasterio.transform import from_origin
    from scipy.ndimage import gaussian_filter
    from shapely.geometry import Polygon, box

    pal = pal or palettes()
    margin = 3 * SIGMA_M
    xmin, ymin, xmax, ymax = frame.bounds
    x0, y1 = xmin - margin, ymax + margin
    w = int(np.ceil((xmax - xmin + 2 * margin) / FIELD_CELL))
    h = int(np.ceil((ymax - ymin + 2 * margin) / FIELD_CELL))
    tr = from_origin(x0, y1, FIELD_CELL, FIELD_CELL)
    clip = box(*frame.lonlat_polygon().buffer(8.0).bounds)
    sigma = SIGMA_M / FIELD_CELL
    raw = {}
    for name in ("arid", "warm_humid"):
        polys = [Polygon(p).segmentize(0.1).intersection(clip) for p in pal["climate_regions"][name]]
        polys = [p for p in polys if not p.is_empty]
        if polys:
            geoms = gpd.GeoSeries(polys, crs=4326).to_crs(frame.crs).values
            m = rasterize(((g, 1) for g in geoms), out_shape=(h, w), transform=tr, fill=0, dtype="uint8")
        else:
            m = np.zeros((h, w), np.uint8)
        raw[name] = gaussian_filter(m.astype(np.float32), sigma, mode="nearest")
    raw["cold_humid"] = np.clip(1 - raw["arid"] - raw["warm_humid"], 0, 1)
    total = sum(raw.values())
    out = {k: (v / total).astype(np.float32) for k, v in raw.items()}
    xs = x0 + (np.arange(w) + 0.5) * FIELD_CELL
    ys = y1 - (np.arange(h) + 0.5) * FIELD_CELL
    xx, yy = np.meshgrid(xs, ys)
    out["lat"] = np.asarray(frame.to_lonlat.transform(xx, yy)[1], dtype=np.float32)
    return out, tr


def sample_field(field: np.ndarray, field_tr, tile_tr, shape: tuple[int, int]) -> np.ndarray:
    """Bilinear sample of a coarse north-up grid at the pixel centres of a finer tile."""
    from scipy.ndimage import map_coordinates

    fx = (tile_tr.c + (np.arange(shape[1]) + 0.5) * tile_tr.a - field_tr.c) / field_tr.a - 0.5
    fy = (tile_tr.f + (np.arange(shape[0]) + 0.5) * tile_tr.e - field_tr.f) / field_tr.e - 0.5
    yy, xx = np.meshgrid(fy.astype(np.float32), fx.astype(np.float32), indexing="ij")
    return map_coordinates(field, [yy, xx], order=1, mode="nearest").astype(np.float32)
