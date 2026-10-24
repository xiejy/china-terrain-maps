"""3D city models: Overture + GlobalBuildingAtlas LoD1 buildings on a GEDTM30
terrain draped with Sentinel-2 imagery, exported as glTF (see
docs/ARCHITECTURE.md, "City contract").

    build_city("hongkong-victoria-harbour")  ->  out/cities/<name>/
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import mapbox_earcut
import numpy as np
import pandas as pd
import shapely
import yaml
from PIL import Image, ImageFilter
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import Affine, from_origin
from rasterio.warp import transform_bounds
from scipy.ndimage import map_coordinates

from chinamaps.net import DATA_DIR, OUT_DIR, REPO_ROOT
from chinamaps.sources import gba, gedtm, overture, sentinel2

CITIES_YAML = REPO_ROOT / "config" / "cities.yaml"
GLTFPACK = ["npx", "-y", "gltfpack@1.3.0"]

HEIGHT_RANGE = (2.5, 650.0)
FLOORS_RANGE = (1, 150)
MIN_AREA = 10.0  # m2
BURY = 2.0  # m, buildings start below the lowest ground point

RESIDENTIAL = {"residential", "apartments", "house", "detached", "dormitory", "terrace", "semidetached_house",
               "bungalow", "cabin", "farm", "static_caravan", "houseboat"}
COMMERCIAL = {"commercial", "retail", "office", "hotel", "supermarket", "kiosk"}
INDUSTRIAL = {"industrial", "warehouse", "factory", "manufacture", "storage_tank", "hangar"}
LOW_CLASSES = {"roof", "carport", "shelter", "kiosk", "toilets", "garage", "garages", "hut", "shed", "canopy",
               "bridge_structure", "service", "transportation"}

# Ground texture styling (cities.yaml `water: {color, alpha}` overrides the water tint).
WATER_RGBA = (38, 86, 124, 0.45)
SAND = (150, 140, 118)  # exposed river bed inside water polygons
ROAD_FILL = (236, 233, 226)
ROAD_CASING = (60, 60, 62)
ROAD_WIDTH_M = {"motorway": 14, "trunk": 12, "primary": 10, "secondary": 8, "tertiary": 7,
                "residential": 4.5, "unclassified": 4.5, "living_street": 4}
RAIL_CLASSES = {"standard_gauge", "light_rail", "monorail", "narrow_gauge", "funicular", "tram"}
WATER_SUBTYPES = {"ocean", "river", "lake", "reservoir", "water", "pond", "canal", "physical", "human_made"}

# Materials (glTF PBR, linear factors).
BUILDING_COLOR = (0.80, 0.80, 0.82, 1.0)
GLASS_TINT = (0.69, 0.775, 0.854)  # multiplier toward a cool grey-blue for towers
GLASS_HEIGHTS = (100.0, 200.0)     # m: towers blend from BUILDING_COLOR to glass over this range

# Footprints, roads and water are mostly OpenStreetMap via Overture (ODbL); the rest of the footprints are
# Shi et al. 2023 (doi:10.5281/zenodo.8174931, via Overture) and GlobalBuildingAtlas.
ATTRIBUTION = ("© OpenStreetMap contributors (ODbL), via Overture Maps Foundation; Shi et al. 2023, A First "
               "High-quality Vector Data of Buildings in East Asian Countries, doi:10.5281/zenodo.8174931 "
               "(CC BY 4.0, via Overture); GlobalBuildingAtlas LoD1 (CC BY-NC 4.0, non-commercial); "
               "GEDTM30 (OpenLandMap, CC BY 4.0); contains modified Copernicus Sentinel data")

_T0 = time.time()


def _log(msg: str) -> None:
    print(f"[{time.time() - _T0:7.1f}s] {msg}", flush=True)


# --- Config and grid ----------------------------------------------------------

def load_config(name: str, path: Path = CITIES_YAML) -> dict:
    """City config merged over defaults (nested dicts merged one level deep)."""
    cfg = yaml.safe_load(Path(path).read_text())
    if name not in cfg["cities"]:
        raise KeyError(f"unknown city {name!r}; known: {sorted(cfg['cities'])}")
    out = dict(cfg.get("defaults", {}))
    for k, v in cfg["cities"][name].items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    out["name"] = name
    return out


@dataclass(frozen=True)
class Grid:
    """Metric working rectangle; origin = SW corner (xmin, ymin)."""

    epsg: int
    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @classmethod
    def from_bbox(cls, bbox, epsg: int, snap: float = 10.0) -> Grid:
        x0, y0, x1, y1 = transform_bounds(4326, epsg, *bbox, densify_pts=41)
        f, c = np.floor, np.ceil
        return cls(epsg, f(x0 / snap) * snap, f(y0 / snap) * snap, c(x1 / snap) * snap, c(y1 / snap) * snap)

    @property
    def crs(self) -> str:
        return f"EPSG:{self.epsg}"

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.xmin, self.ymin, self.xmax, self.ymax)

    @property
    def width(self) -> float:
        return self.xmax - self.xmin

    @property
    def height(self) -> float:
        return self.ymax - self.ymin

    def raster(self, res: float) -> tuple[Affine, tuple[int, int]]:
        """Pixel-is-area raster covering the bounds exactly."""
        return from_origin(self.xmin, self.ymax, res, res), (round(self.height / res), round(self.width / res))

    def nodes(self, res: float) -> tuple[Affine, tuple[int, int]]:
        """Raster whose pixel centres sit on grid nodes (mesh vertices)."""
        h, w = round(self.height / res), round(self.width / res)
        return from_origin(self.xmin - res / 2, self.ymax + res / 2, res, res), (h + 1, w + 1)

    def lonlat_bbox(self, pad: float = 0.0005) -> tuple[float, float, float, float]:
        b = transform_bounds(self.epsg, 4326, *self.bounds, densify_pts=41)
        return (b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad)

    def to_local(self, e, n, z) -> np.ndarray:
        """glTF Y-up frame: x = E - xmin, y = z, z = -(N - ymin)."""
        return np.column_stack([np.asarray(e) - self.xmin, np.asarray(z), -(np.asarray(n) - self.ymin)])


def sample_bilinear(dem: np.ndarray, grid: Grid, res: float, e, n) -> np.ndarray:
    """Bilinear DEM value at (E, N) on the node grid (edge-clamped)."""
    rows = (grid.ymax - np.asarray(n)) / res
    cols = (np.asarray(e) - grid.xmin) / res
    return map_coordinates(dem, [rows, cols], order=1, mode="nearest")


# --- Footprints and heights ---------------------------------------------------

def clean_footprints(gdf: gpd.GeoDataFrame, min_area: float = MIN_AREA, simplify: float = 0.2) -> gpd.GeoDataFrame:
    """make_valid, keep polygonal parts, simplify slightly, drop tiny ones (projected CRS)."""
    geom = shapely.make_valid(gdf.geometry.values)
    parts, idx = shapely.get_parts(geom, return_index=True)
    keep = shapely.get_type_id(parts) == 3  # Polygon
    parts, idx = parts[keep], idx[keep]
    parts = shapely.simplify(parts, simplify, preserve_topology=True)
    merged = shapely.multipolygons(parts, indices=idx, out=np.full(len(gdf), None, dtype=object))
    single = shapely.get_num_geometries(merged) == 1
    merged[single] = shapely.get_geometry(merged[single], 0)
    has = np.array([g is not None for g in merged])
    out = gdf[has].copy()
    out["geometry"] = merged[has]
    out = out.set_geometry("geometry")
    return out[(out.area >= min_area) & out.is_valid & ~out.is_empty]


def _valid_height(h) -> np.ndarray:
    h = pd.to_numeric(pd.Series(h), errors="coerce").to_numpy(float)
    return np.where((h >= HEIGHT_RANGE[0]) & (h <= HEIGHT_RANGE[1]), h, np.nan)


def _valid_floors(f) -> np.ndarray:
    f = pd.to_numeric(pd.Series(f), errors="coerce").to_numpy(float)
    return np.where((f >= FLOORS_RANGE[0]) & (f <= FLOORS_RANGE[1]), f, np.nan)


def gba_match(geoms: np.ndarray, gba_geoms: np.ndarray, gba_heights: np.ndarray,
              min_overlap: float) -> tuple[np.ndarray, np.ndarray]:
    """Height of the best-overlapping GBA footprint per geometry (NaN if overlap < min_overlap)."""
    out_h = np.full(len(geoms), np.nan)
    out_f = np.zeros(len(geoms))
    if len(geoms) == 0 or len(gba_geoms) == 0:
        return out_h, out_f
    tree = shapely.STRtree(gba_geoms)
    a, b = tree.query(geoms, predicate="intersects")
    if len(a) == 0:
        return out_h, out_f
    frac = shapely.area(shapely.intersection(geoms[a], gba_geoms[b])) / np.maximum(shapely.area(geoms[a]), 1e-9)
    order = np.lexsort((-frac, a))  # by building, best overlap first
    a, b, frac = a[order], b[order], frac[order]
    first = np.r_[True, a[1:] != a[:-1]]
    a, b, frac = a[first], b[first], frac[first]
    ok = frac >= min_overlap
    out_h[a[ok]] = gba_heights[b[ok]]
    out_f[a] = frac
    return out_h, out_f


def default_kind(cls, subtype, landuse) -> str:
    for v in (subtype, cls, landuse):
        if not isinstance(v, str):
            continue
        if v in RESIDENTIAL:
            return "residential"
        if v in COMMERCIAL:
            return "commercial"
        if v in INDUSTRIAL:
            return "industrial"
    return "default"


def assign_heights(bld: gpd.GeoDataFrame, gba_gdf: gpd.GeoDataFrame | None, cfg: dict,
                   landuse: gpd.GeoDataFrame | None = None) -> gpd.GeoDataFrame:
    """Height cascade (projected CRS): Overture height -> num_floors x floor_height
    -> best-overlap GBA height -> class/area default. Adds height, height_source.

    Exception: num_floors == 1 (often an import default) yields to a GBA match
    of at least three floors, unless the class is a canopy-like LOW_CLASSES one.
    """
    out = bld.copy()
    n = len(out)
    h_ov = _valid_height(out["height"] if "height" in out else np.full(n, np.nan))
    h_fl = _valid_floors(out["num_floors"] if "num_floors" in out else np.full(n, np.nan)) * cfg["floor_height"]
    height = np.where(np.isfinite(h_ov), h_ov, h_fl)
    source = np.where(np.isfinite(h_ov), "overture_height", np.where(np.isfinite(h_fl), "overture_floors", ""))

    need = ~np.isfinite(height)
    # a lone num_floors = 1 is often an import default: a much taller GBA match wins
    cls_all = out["class"].to_numpy(object) if "class" in out else np.full(n, None)
    suspect = ~np.isfinite(h_ov) & (h_fl == cfg["floor_height"]) & ~np.isin(cls_all, list(LOW_CLASSES))
    cand = need | suspect
    if gba_gdf is not None and len(gba_gdf) and cand.any():
        h_gba = np.full(n, np.nan)
        h_gba[cand], _ = gba_match(out.geometry.values[cand], gba_gdf.geometry.values,
                                   gba_gdf["height"].to_numpy(float), cfg["gba_min_overlap"])
        use = (need & np.isfinite(h_gba)) | (suspect & (h_gba >= 3 * cfg["floor_height"]))
        height[use] = np.clip(h_gba[use], 3.0, HEIGHT_RANGE[1])
        source[use] = "gba"

    need = ~np.isfinite(height)
    if need.any():
        d = cfg["default_heights"]
        lu = _landuse_class(out.geometry.values[need], landuse)
        cls = out["class"].to_numpy(object)[need] if "class" in out else [None] * need.sum()
        sub = out["subtype"].to_numpy(object)[need] if "subtype" in out else [None] * need.sum()
        area = shapely.area(out.geometry.values[need])
        hd = [d["small"] if a < d["small_area_m2"] else d[default_kind(c, s, u)]
              for c, s, u, a in zip(cls, sub, lu, area)]
        height[need] = hd
        source[need] = "default"
    out["height"] = np.clip(height, *HEIGHT_RANGE)
    out["height_source"] = source
    return out


def overlap_fraction(geoms: np.ndarray, others: np.ndarray) -> np.ndarray:
    """Fraction of each geometry's area covered by the union of others (pairwise sum, capped at 1)."""
    out = np.zeros(len(geoms))
    if len(geoms) == 0 or len(others) == 0:
        return out
    a, b = shapely.STRtree(others).query(geoms, predicate="intersects")
    np.add.at(out, a, shapely.area(shapely.intersection(geoms[a], others[b])))
    return np.minimum(out / np.maximum(shapely.area(geoms), 1e-9), 1.0)


def is_ml_only(bld: gpd.GeoDataFrame) -> np.ndarray:
    """Machine-learned footprints whose height did not come from Overture attributes."""
    src = bld["source_dataset"].fillna("").to_numpy(object)
    ml = np.array(["OpenStreetMap" not in s for s in src])
    return ml & ~bld["height_source"].isin(["overture_height", "overture_floors"]).to_numpy()


def conflate_gba(bld: gpd.GeoDataFrame, gba_gdf: gpd.GeoDataFrame, max_overlap: float = 0.3,
                 ml_buffer: float = 8.0) -> gpd.GeoDataFrame:
    """Replace ML-only Overture footprints with GBA footprints (projected CRS).

    Keeps every attributed Overture building, adds GBA footprints covered by
    them less than max_overlap (height_source 'gba'), then keeps only ML
    footprints that neither set, grown by ml_buffer metres, covers by
    max_overlap or more (offset duplicates of the same building are dropped).
    Run after assign_heights.
    """
    ml = is_ml_only(bld)
    keep = bld[~ml]
    g = gba_gdf[gba_gdf.area >= MIN_AREA]
    g = g[overlap_fraction(g.geometry.values, keep.geometry.values) < max_overlap]
    add = gpd.GeoDataFrame({
        "id": "gba:" + g["id"].astype(str), "height": np.clip(g["height"].to_numpy(float), 3.0, HEIGHT_RANGE[1]),
        "height_source": "gba", "source_dataset": "GlobalBuildingAtlas:" + g["source"].astype(str),
    }, geometry=g.geometry.values, crs=bld.crs)
    rest = bld[ml]
    taken = shapely.buffer(np.concatenate([keep.geometry.values, add.geometry.values]), ml_buffer, quad_segs=2)
    rest = rest[overlap_fraction(rest.geometry.values, taken) < max_overlap]
    return pd.concat([keep, add, rest], ignore_index=True)


def _landuse_class(geoms: np.ndarray, landuse: gpd.GeoDataFrame | None) -> list:
    """Land-use class at each footprint centroid (None where uncovered)."""
    out = [None] * len(geoms)
    if landuse is None or len(landuse) == 0 or len(geoms) == 0:
        return out
    lu = landuse[landuse.geom_type.isin(["Polygon", "MultiPolygon"])]
    tree = shapely.STRtree(lu.geometry.values)
    a, b = tree.query(shapely.centroid(geoms), predicate="within")
    cls = lu["class"].to_numpy(object)
    for i, j in zip(a, b):
        if out[i] is None or cls[j] in RESIDENTIAL | COMMERCIAL | INDUSTRIAL:
            out[i] = cls[j]
    return out


def attach_parts(bld: gpd.GeoDataFrame, parts: gpd.GeoDataFrame | None, cfg: dict,
                 coverage: float = 0.75) -> gpd.GeoDataFrame:
    """Add building_part prisms for buildings with parts.

    Parts inherit the parent's base and, when they lack heights, the parent's
    height. A parent whose parts cover >= coverage of its outline is replaced
    by them; otherwise both are kept. Adds min_height and part_of columns.
    """
    bld = bld.copy()
    bld["min_height"] = 0.0
    bld["part_of"] = None
    if parts is None or len(parts) == 0:
        return bld
    parent = bld.set_index("id")
    p = parts[parts["building_id"].isin(parent.index)].copy()
    if p.empty:
        return bld
    h = _valid_height(p["height"])
    hf = _valid_floors(p["num_floors"]) * cfg["floor_height"]
    ph = parent.loc[p["building_id"], "height"].to_numpy(float)
    psrc = parent.loc[p["building_id"], "height_source"].to_numpy(object)
    p["height"] = np.where(np.isfinite(h), h, np.where(np.isfinite(hf), hf, ph))
    p["height_source"] = np.where(np.isfinite(h), "overture_height", np.where(np.isfinite(hf), "overture_floors", psrc))
    mh = pd.to_numeric(p["min_height"], errors="coerce").fillna(0.0).to_numpy(float)
    p["min_height"] = np.clip(mh, 0, None)
    p = p[p["height"] > p["min_height"] + 1.0]
    p["part_of"] = p["building_id"]
    for col in ("class", "subtype", "name"):
        if col in parent:
            p[col] = parent.loc[p["building_id"], col].to_numpy(object)
    psd = parent.loc[p["building_id"], "source_dataset"].to_numpy(object)
    p["source_dataset"] = p["source_dataset"].where(p["source_dataset"].notna(), psd) if "source_dataset" in p else psd

    union = p.groupby("building_id").geometry.apply(lambda s: shapely.union_all(s.values))
    cov = shapely.area(shapely.intersection(union.values, parent.loc[union.index].geometry.values))
    cov = cov / parent.loc[union.index].geometry.area.to_numpy()
    replaced = set(union.index[cov >= coverage])
    keep = bld[~bld["id"].isin(replaced)]
    cols = [c for c in keep.columns if c in p.columns]
    return pd.concat([keep, p[cols]], ignore_index=True)


def separate_coplanar(bld: gpd.GeoDataFrame, tol: float = 0.3, step: float = 0.6,
                      passes: int = 3) -> gpd.GeoDataFrame:
    """Lower the smaller of any two overlapping prisms whose roofs coincide.

    Coplanar overlapping roofs z-fight in WebGL and self-shadow to black in
    Cycles (towers mapped on podiums, parts kept with their parent, duplicates).
    """
    out = bld.copy()
    g = out.geometry.values
    area = shapely.area(g)
    i, j = shapely.STRtree(g).query(g, predicate="intersects")
    keep = i < j
    i, j = i[keep], j[keep]
    ov = shapely.area(shapely.intersection(g[i], g[j])) > 1.0
    i, j = i[ov], j[ov]
    smaller = np.where(area[i] <= area[j], i, j)
    h = out["height"].to_numpy(float).copy()
    base = out["base_z"].to_numpy(float)
    for _ in range(passes):
        top = base + h
        cop = np.abs(top[i] - top[j]) < tol
        if not cop.any():
            break
        h[np.unique(smaller[cop])] -= step
    out["height"] = h
    return out


def base_heights(geoms: np.ndarray, dem: np.ndarray, grid: Grid, res: float) -> np.ndarray:
    """Minimum ground height over footprint vertices plus centroid."""
    xy, idx = shapely.get_coordinates(geoms, return_index=True)
    c = shapely.get_coordinates(shapely.centroid(geoms))
    xy = np.vstack([xy, c])
    idx = np.concatenate([idx, np.arange(len(geoms))])
    z = sample_bilinear(dem, grid, res, xy[:, 0], xy[:, 1])
    out = np.full(len(geoms), np.inf)
    np.minimum.at(out, idx, z)
    return out


# --- Meshes -------------------------------------------------------------------

@dataclass
class MeshData:
    name: str
    positions: np.ndarray  # (N, 3) float32, local frame
    indices: np.ndarray  # (M, 3) uint32
    normals: np.ndarray | None = None
    uv: np.ndarray | None = None
    colors: np.ndarray | None = None  # (N, 4) uint8
    color: tuple = (1.0, 1.0, 1.0, 1.0)
    roughness: float = 0.9
    image: bytes | None = None
    mime: str = "image/jpeg"


def terrain_mesh(dem: np.ndarray, grid: Grid, image: bytes | None = None) -> MeshData:
    """Grid mesh over a node DEM spanning the grid bounds (row 0 = north edge,
    last row/column on the south/east edges); UV (0,0) = NW corner (glTF)."""
    h, w = dem.shape
    es = np.linspace(grid.xmin, grid.xmax, w)
    ns = np.linspace(grid.ymax, grid.ymin, h)
    e, n = np.meshgrid(es, ns)
    pos = grid.to_local(e.ravel(), n.ravel(), dem.ravel()).astype(np.float32)
    uv = np.column_stack([(e.ravel() - grid.xmin) / grid.width, (grid.ymax - n.ravel()) / grid.height])
    uv = uv.astype(np.float32)
    gr, gc = np.gradient(dem.astype(np.float64), ns, es)
    dz_de, dz_dn = gc, gr
    nrm = np.stack([-dz_de, np.ones_like(dem, float), dz_dn], axis=-1).reshape(-1, 3)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
    idx = np.arange(h * w, dtype=np.uint32).reshape(h, w)
    nw, ne, sw, se = idx[:-1, :-1], idx[:-1, 1:], idx[1:, :-1], idx[1:, 1:]
    tris = np.concatenate([np.stack([sw, se, ne], -1).reshape(-1, 3), np.stack([sw, ne, nw], -1).reshape(-1, 3)])
    return MeshData("terrain", pos, tris, nrm.astype(np.float32), uv, image=image, roughness=0.95)


def extrude(polys: np.ndarray, z0: np.ndarray, z1: np.ndarray, grid: Grid,
            colors: np.ndarray | None = None) -> MeshData:
    """LoD1 prisms (walls + flat roofs, no floors) with flat normals.

    polys: Polygons in the grid CRS; z0/z1 bottom/top elevations per polygon;
    colors: optional (n, 4) uint8 per polygon.
    """
    polys = shapely.orient_polygons(polys, exterior_cw=False)
    rings, ring_poly = shapely.get_rings(polys, return_index=True)
    xy, ring_of = shapely.get_coordinates(rings, return_index=True)
    # walls: consecutive vertices of the same (closed) ring
    s = np.flatnonzero(ring_of[:-1] == ring_of[1:])
    p0, p1 = xy[s], xy[s + 1]
    pid = ring_poly[ring_of[s]]
    lo, hi = z0[pid], z1[pid]
    quad = np.stack([
        grid.to_local(p0[:, 0], p0[:, 1], lo), grid.to_local(p1[:, 0], p1[:, 1], lo),
        grid.to_local(p1[:, 0], p1[:, 1], hi), grid.to_local(p0[:, 0], p0[:, 1], hi),
    ], axis=1)  # (k, 4, 3)
    d = p1 - p0
    wn = np.column_stack([d[:, 1], np.zeros(len(d)), d[:, 0]])  # outward (dN, 0, dE) in local frame
    wn /= np.maximum(np.linalg.norm(wn, axis=1, keepdims=True), 1e-12)
    k = len(s)
    wall_pos = quad.reshape(-1, 3)
    wall_nrm = np.repeat(wn, 4, axis=0)
    base = (np.arange(k, dtype=np.uint32) * 4)[:, None]
    wall_tri = np.concatenate([base + [0, 1, 2], base + [0, 2, 3]])

    # roofs: earcut per polygon on rings without the closing vertex
    roof_xy, roof_tri, roof_pid = [], [], []
    off = 0
    ring_start = np.r_[0, np.flatnonzero(np.diff(ring_of)) + 1]
    ring_len = np.diff(np.r_[ring_start, len(ring_of)])
    poly_rings = np.split(np.arange(len(rings)), np.flatnonzero(np.diff(ring_poly)) + 1)
    for rids in poly_rings:
        if len(rids) == 0:
            continue
        pts = [xy[ring_start[r] : ring_start[r] + ring_len[r] - 1] for r in rids]
        v = np.vstack(pts)
        ends = np.cumsum([len(q) for q in pts]).astype(np.uint32)
        t = mapbox_earcut.triangulate_float64(v, ends).reshape(-1, 3).astype(np.int64)
        if len(t) == 0:
            continue
        a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
        cw = ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])) < 0
        t[cw] = t[cw][:, [0, 2, 1]]
        roof_xy.append(v)
        roof_tri.append(t + off)
        roof_pid.append(np.full(len(v), ring_poly[rids[0]]))
        off += len(v)
    rxy = np.vstack(roof_xy)
    rpid = np.concatenate(roof_pid)
    roof_pos = grid.to_local(rxy[:, 0], rxy[:, 1], z1[rpid])
    roof_nrm = np.tile([0.0, 1.0, 0.0], (len(rxy), 1))
    roof_tri = np.vstack(roof_tri) + len(wall_pos)

    pos = np.vstack([wall_pos, roof_pos]).astype(np.float32)
    nrm = np.vstack([wall_nrm, roof_nrm]).astype(np.float32)
    tri = np.vstack([wall_tri, roof_tri]).astype(np.uint32)
    col = None
    if colors is not None:
        col = np.vstack([np.repeat(colors[pid], 4, axis=0), colors[rpid]]).astype(np.uint8)
    return MeshData("buildings", pos, tri, nrm, colors=col, color=BUILDING_COLOR, roughness=0.85)


def height_tint(height: np.ndarray) -> np.ndarray:
    """Per-building colour multiplier: low-rise slightly darker/warmer, mid-rise white, towers
    above ~150 m toward a cool glass grey-blue so the skyline reads."""
    t = np.clip(np.log(np.maximum(height, 3) / 6) / np.log(50), 0, 1)[:, None]
    low, high = np.array([0.88, 0.86, 0.83]), np.array([1.0, 1.0, 1.0])
    rgb = low + (high - low) * t
    g = np.clip((height - GLASS_HEIGHTS[0]) / (GLASS_HEIGHTS[1] - GLASS_HEIGHTS[0]), 0, 1)[:, None]
    rgb = rgb + g * (np.asarray(GLASS_TINT) - rgb)
    return np.column_stack([rgb * 255, np.full(len(t), 255)]).round().astype(np.uint8)


# --- glTF writer --------------------------------------------------------------

def write_glb(path: Path, meshes: list[MeshData], extras: dict | None = None) -> None:
    """Minimal glTF 2.0 binary writer: one node + mesh + material per MeshData."""
    import pygltflib as g

    blob = bytearray()
    views, accessors = [], []

    def add_view(data: bytes, target: int | None = None) -> int:
        while len(blob) % 4:
            blob.append(0)
        views.append(g.BufferView(buffer=0, byteOffset=len(blob), byteLength=len(data), target=target))
        blob.extend(data)
        return len(views) - 1

    def add_accessor(arr: np.ndarray, ctype: int, atype: str, target: int | None, normalized: bool = False,
                     minmax: bool = False) -> int:
        v = add_view(np.ascontiguousarray(arr).tobytes(), target)
        acc = g.Accessor(bufferView=v, componentType=ctype, count=len(arr), type=atype, normalized=normalized or None)
        if minmax:
            acc.min = arr.min(axis=0).astype(float).tolist()
            acc.max = arr.max(axis=0).astype(float).tolist()
        accessors.append(acc)
        return len(accessors) - 1

    gl = g.GLTF2(asset=g.Asset(generator="chinamaps", copyright=ATTRIBUTION))
    for m in meshes:
        attrs = g.Attributes(POSITION=add_accessor(m.positions.astype(np.float32), g.FLOAT, g.VEC3, g.ARRAY_BUFFER,
                                                   minmax=True))
        if m.normals is not None:
            attrs.NORMAL = add_accessor(m.normals.astype(np.float32), g.FLOAT, g.VEC3, g.ARRAY_BUFFER)
        if m.uv is not None:
            attrs.TEXCOORD_0 = add_accessor(m.uv.astype(np.float32), g.FLOAT, g.VEC2, g.ARRAY_BUFFER)
        if m.colors is not None:
            attrs.COLOR_0 = add_accessor(m.colors.astype(np.uint8), g.UNSIGNED_BYTE, g.VEC4, g.ARRAY_BUFFER,
                                         normalized=True)
        ind = add_accessor(m.indices.astype(np.uint32).reshape(-1), g.UNSIGNED_INT, g.SCALAR, g.ELEMENT_ARRAY_BUFFER)
        pbr = g.PbrMetallicRoughness(baseColorFactor=list(m.color), metallicFactor=0.0, roughnessFactor=m.roughness)
        if m.image is not None:
            gl.images.append(g.Image(bufferView=add_view(m.image), mimeType=m.mime))
            if not gl.samplers:
                gl.samplers.append(g.Sampler(magFilter=g.LINEAR, minFilter=g.LINEAR_MIPMAP_LINEAR,
                                             wrapS=g.CLAMP_TO_EDGE, wrapT=g.CLAMP_TO_EDGE))
            gl.textures.append(g.Texture(source=len(gl.images) - 1, sampler=0))
            pbr.baseColorTexture = g.TextureInfo(index=len(gl.textures) - 1)
        gl.materials.append(g.Material(name=m.name, pbrMetallicRoughness=pbr))
        gl.meshes.append(g.Mesh(name=m.name, primitives=[g.Primitive(attributes=attrs, indices=ind,
                                                                      material=len(gl.materials) - 1)]))
        gl.nodes.append(g.Node(name=m.name, mesh=len(gl.meshes) - 1))
    gl.scenes = [g.Scene(name="city", nodes=list(range(len(gl.nodes))), extras=extras or {})]
    gl.scene = 0
    gl.bufferViews, gl.accessors = views, accessors
    while len(blob) % 4:
        blob.append(0)
    gl.buffers = [g.Buffer(byteLength=len(blob))]
    gl.set_binary_blob(bytes(blob))
    path.parent.mkdir(parents=True, exist_ok=True)
    gl.save_binary(str(path))


def inspect_glb(path: Path) -> dict:
    """Node names, embedded images and per-node y range (from accessor min/max)."""
    import pygltflib as g

    gl = g.GLTF2().load_binary(str(path))
    out = {"nodes": [n.name for n in gl.nodes], "images": [], "y_range": {}, "extensions": gl.extensionsUsed or []}
    blob = gl.binary_blob()
    for im in gl.images:
        bv = gl.bufferViews[im.bufferView]
        data = blob[bv.byteOffset or 0 : (bv.byteOffset or 0) + bv.byteLength] if blob is not None else b""
        out["images"].append({"mime": im.mimeType, "bytes": bv.byteLength,
                              "size": Image.open(io.BytesIO(data)).size if data else None})
    for n in gl.nodes:
        if n.mesh is None:
            continue
        acc = gl.accessors[gl.meshes[n.mesh].primitives[0].attributes.POSITION]
        if acc.min and acc.max:
            s = (n.scale or [1, 1, 1])[1]
            t = (n.translation or [0, 0, 0])[1]
            out["y_range"][n.name] = [acc.min[1] * s + t, acc.max[1] * s + t]
    return out


def gltfpack(src: Path, dst: Path, extra: list[str] | None = None) -> None:
    """meshopt-compress with gltfpack (EXT_meshopt_compression, textures untouched)."""
    cmd = [*GLTFPACK, "-i", str(src), "-o", str(dst), "-cc", "-ce", "ext", "-kn", "-km", *(extra or [])]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"gltfpack failed: {r.stdout[-500:]} {r.stderr[-1000:]}")


# --- Ground texture -----------------------------------------------------------

def _supersampled_mask(geoms, transform: Affine, shape: tuple[int, int], ss: int = 2) -> np.ndarray:
    """Anti-aliased coverage in [0, 1] by rasterizing at ss x resolution."""
    geoms = [g for g in geoms if g is not None and not g.is_empty]
    if not geoms:
        return np.zeros(shape, np.float32)
    t = transform * Affine.scale(1 / ss)
    m = rasterize(geoms, out_shape=(shape[0] * ss, shape[1] * ss), transform=t, dtype="uint8")
    return m.reshape(shape[0], ss, shape[1], ss).mean(axis=(1, 3), dtype=np.float32)


def _blend(img: np.ndarray, alpha: np.ndarray, rgb) -> None:
    a = alpha[..., None]
    img *= 1 - a
    img += a * (np.asarray(rgb, np.float32) / 255)


def water_polygons(water: gpd.GeoDataFrame | None) -> gpd.GeoSeries:
    if water is None or water.empty:
        return gpd.GeoSeries([])
    w = water[water.geom_type.isin(["Polygon", "MultiPolygon"]) & water["subtype"].isin(WATER_SUBTYPES)]
    return w.geometry


def road_layers(seg: gpd.GeoDataFrame | None) -> tuple[list, list, list]:
    """(casing, fill, rail) buffered geometries in the projected CRS."""
    if seg is None or seg.empty:
        return [], [], []
    s = seg[seg.geom_type.isin(["LineString", "MultiLineString"])]
    if "is_tunnel" in s:
        s = s[s["is_tunnel"] < 0.5]
    roads = s[(s["subtype"] == "road") & s["class"].isin(ROAD_WIDTH_M)]
    width = roads["class"].map(ROAD_WIDTH_M).to_numpy(float)
    fill = shapely.buffer(roads.geometry.values, width / 2, cap_style="round", join_style="round")
    casing = shapely.buffer(roads.geometry.values, width / 2 + 1.5, cap_style="round", join_style="round")
    rail = s[(s["subtype"] == "rail") & s["class"].isin(RAIL_CLASSES)]
    rail_g = shapely.buffer(rail.geometry.values, 1.5, cap_style="flat")
    return list(casing), list(fill), list(rail_g)


def water_mask(water: gpd.GeoSeries, transform: Affine, shape, sigma: float = 2.0) -> np.ndarray:
    """Smoothed water coverage in [0, 1] (Gaussian sigma px, then a soft 0.5 threshold)."""
    from scipy.ndimage import gaussian_filter

    m = gaussian_filter(_supersampled_mask(list(water.values), transform, shape), sigma)
    return np.clip((m - 0.5) * 4 + 0.5, 0, 1).astype(np.float32)


def sand_mask(img: np.ndarray, wmask: np.ndarray, sigma: float = 2.0) -> np.ndarray:
    """Exposed river bed: pixels inside the water mask whose imagery is warm (red above blue) and
    bright (muddy water is warm too, but darker)."""
    from scipy.ndimage import gaussian_filter

    warm = np.clip(((img[..., 0] - img[..., 2]) - 0.04) / 0.06, 0, 1)
    bright = np.clip((img.mean(-1) - 0.30) / 0.08, 0, 1)
    return np.clip(gaussian_filter(warm * bright * wmask, sigma) * 1.6 - 0.3, 0, 1).astype(np.float32)


def soften_water(img: np.ndarray, wmask: np.ndarray, sigma: float = 6.0) -> np.ndarray:
    """Blur the imagery under water: 10-20 m Sentinel-2 mosaic pixels otherwise show as stair-stepped
    plumes and sandbars once upsampled to the 2 m ground texture."""
    from scipy.ndimage import gaussian_filter

    soft = gaussian_filter(img, (sigma, sigma, 0))
    return img + wmask[..., None] * (soft - img)


def ground_texture(rgb: np.ndarray, transform: Affine, water: gpd.GeoSeries, segments: gpd.GeoDataFrame | None,
                   s2_kw: dict | None = None, water_style: dict | None = None,
                   wmask: np.ndarray | None = None) -> np.ndarray:
    """Stretched S2 true colour, sharpened, with water tint (exposed river bed as wet sand) and
    road lines. water_style: {color, alpha, sand}. -> uint8 (H, W, 3)."""
    shape = rgb.shape[1:]
    wmask = water_mask(water, transform, shape) if wmask is None else wmask
    base = sentinel2.true_color(rgb, mask=wmask < 0.5, **(s2_kw or {}))
    base = Image.fromarray(base).filter(ImageFilter.UnsharpMask(radius=2, percent=70, threshold=2))
    img = soften_water(np.asarray(base, np.float32) / 255, wmask)
    ws = {"color": WATER_RGBA[:3], "alpha": WATER_RGBA[3], "sand": True, **(water_style or {})}
    sand = sand_mask(img, wmask) if ws["sand"] else np.zeros_like(wmask)
    _blend(img, wmask * (1 - sand) * float(ws["alpha"]), ws["color"])
    _blend(img, sand * 0.8, SAND)
    casing, fill, rail = road_layers(segments)
    _blend(img, _supersampled_mask(casing, transform, shape) * 0.25, ROAD_CASING)
    _blend(img, _supersampled_mask(fill, transform, shape) * 0.65, ROAD_FILL)
    _blend(img, _supersampled_mask(rail, transform, shape) * 0.55, (70, 66, 72))
    return (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8)


def jpeg_bytes(img: np.ndarray | Image.Image, quality: int = 92, max_side: int | None = None) -> bytes:
    im = img if isinstance(img, Image.Image) else Image.fromarray(img)
    if max_side and max(im.size) > max_side:
        s = max_side / max(im.size)
        im = im.resize((round(im.size[0] * s), round(im.size[1] * s)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, subsampling=0 if quality >= 90 else 2, optimize=True)
    return buf.getvalue()


# --- Preview ------------------------------------------------------------------

def preview(path: Path, ground: np.ndarray, grid: Grid, bld: gpd.GeoDataFrame, title: str,
            landmarks: list[dict], max_px: int = 1600) -> None:
    """Top-down ground texture with footprints coloured by height."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from matplotlib.colors import LogNorm

    aspect = grid.height / grid.width
    fig_w = 10.0
    dpi = max_px / (fig_w * 1.12)
    fig, ax = plt.subplots(figsize=(fig_w * 1.12, fig_w * aspect + 0.6), dpi=dpi)
    small = Image.fromarray(ground)
    small.thumbnail((2400, 2400))
    ax.imshow(np.asarray(small), extent=(grid.xmin, grid.xmax, grid.ymin, grid.ymax), interpolation="lanczos")
    b = bld.sort_values("height")
    polys = [np.asarray(p.exterior.coords) for g in b.geometry.values for p in getattr(g, "geoms", [g])]
    heights = np.concatenate([[h] * len(getattr(g, "geoms", [g])) for g, h in zip(b.geometry.values, b["height"])])
    pc = PolyCollection(polys, array=heights, cmap="magma", norm=LogNorm(3, max(200, heights.max())),
                        alpha=0.6, linewidths=0.15, edgecolors="k")
    ax.add_collection(pc)
    for lm in landmarks:
        ax.plot(lm["e"], lm["n"], "c+", ms=12, mew=1.5)
        ax.annotate(lm["name"], (lm["e"], lm["n"]), xytext=(6, 6), textcoords="offset points", color="c", fontsize=7)
    ax.set_xlim(grid.xmin, grid.xmax)
    ax.set_ylim(grid.ymin, grid.ymax)
    ax.set_aspect("equal")
    ax.set_title(title, fontsize=9)
    ax.tick_params(labelsize=6)
    fig.colorbar(pc, ax=ax, fraction=0.03, pad=0.01, label="height (m)")
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


# --- Pipeline -----------------------------------------------------------------

def extract_sources(cfg: dict, grid: Grid, cache: Path) -> dict:
    """Overture themes, GBA footprints and the DTM window, cached under cache/."""
    q = grid.lonlat_bbox()
    release = None
    out = {}
    for key, (theme, type_) in {"building": ("buildings", "building"), "part": ("buildings", "building_part"),
                                "water": ("base", "water"), "land_use": ("base", "land_use"),
                                "segment": ("transportation", "segment")}.items():
        dest = cache / f"overture_{type_}.parquet"
        if not dest.exists() and release is None:
            release = overture.latest_release()
        try:
            out[key] = overture.extract(theme, type_, q, dest, release)
        except RuntimeError as e:  # e.g. no parts in the bbox
            _log(f"  {key}: {e}")
            out[key] = None
        _log(f"  overture {type_}: {0 if out[key] is None else len(out[key])}")
    meta = cache / "overture_building.json"
    out["release"] = json.loads(meta.read_text())["release"] if meta.exists() else release
    out["gba"] = gba.extract(cfg.get("gba_tile") or gba.tile_name(*grid_center(q)), q, cache / "gba_lod1.parquet")
    _log(f"  gba: {len(out['gba'])}")
    out["dtm"] = gedtm.extract(q, cache / "gedtm30.tif")
    return out


def grid_center(b) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def sentinel_rgb(cfg: dict, grid: Grid, water: gpd.GeoSeries, transform: Affine, shape, cache: Path) -> tuple:
    """Pick the quarter (cached choice or config s2_quarter) and read RGB on the texture grid."""
    choice = cache / "s2_choice.json"
    if cfg.get("s2_quarter"):
        y, q = cfg["s2_quarter"].split("-")
        sel = {"year": int(y), "quarter": q, "scores": None}
    elif choice.exists():
        sel = json.loads(choice.read_text())
    else:
        tr20, shape20 = grid.raster(20.0)
        wm = _supersampled_mask(list(water.values), tr20, shape20) > 0.99
        (y, q), _, scores = sentinel2.choose_quarter(cfg["bbox"], cfg["epsg"], tr20, shape20, wm,
                                                     hint=cfg.get("s2_mgrs"))
        sel = {"year": y, "quarter": q, "scores": scores}
        choice.write_text(json.dumps(sel, indent=1))
    tiles = sentinel2.resolve_tiles(grid.lonlat_bbox(), cfg["epsg"], sel["year"], sel["quarter"], cfg.get("s2_mgrs"))
    rgb_path = cache / f"s2_{sel['year']}_{sel['quarter']}_{transform.a:g}m.npy"
    if rgb_path.exists():
        rgb = np.load(rgb_path)
    else:
        rgb = sentinel2.read_bands(sel["year"], sel["quarter"], tiles, grid.crs, transform, shape)
        np.save(rgb_path, rgb)
    return rgb, {**sel, "tiles": tiles}


def vegetated(geoms: np.ndarray, rgb: np.ndarray, transform: Affine, exg_min: float = 0.08) -> np.ndarray:
    """True where the imagery around a footprint's centroid (3x3 px) is vegetation (excess-green
    index 2g - r - b in chromatic coordinates above exg_min): ML footprints under forest canopy."""
    from rasterio.transform import rowcol

    c = shapely.get_coordinates(shapely.centroid(geoms))
    rows, cols = (np.asarray(v) for v in rowcol(transform, c[:, 0], c[:, 1]))
    h, w = rgb.shape[1:]
    acc = np.zeros((3, len(c)))
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            r = np.clip(rows + dr, 0, h - 1)
            k = np.clip(cols + dc, 0, w - 1)
            acc += np.nan_to_num(rgb[:, r, k])
    chrom = acc / np.maximum(acc.sum(0), 1e-9)
    return (2 * chrom[1] - chrom[0] - chrom[2]) > exg_min


def smooth_under_water(dem: np.ndarray, wmask: np.ndarray, sigma: float = 8.0) -> np.ndarray:
    """Blend the DEM toward a smoothed copy under water (removes 30 m stair-steps on river beds)."""
    from scipy.ndimage import gaussian_filter

    return (dem + wmask * (gaussian_filter(dem, sigma) - dem)).astype(np.float32)


def build_buildings(src: dict, grid: Grid, dem: np.ndarray, cfg: dict) -> gpd.GeoDataFrame:
    """Footprints -> heights cascade -> parts -> base_z, in the grid CRS. Footprints smaller than
    cfg min_footprint_m2 (machine-learned specks on forested slopes, kiosks) are dropped."""
    frame = shapely.box(*grid.bounds)
    bld = src["building"].to_crs(grid.epsg)
    if "is_underground" in bld:
        bld = bld[~bld["is_underground"].fillna(False).astype(bool)]
    bld = clean_footprints(bld)
    bld = bld[bld.within(frame)]
    gba_p = clean_footprints(src["gba"].to_crs(grid.epsg), min_area=1.0)
    lu = src["land_use"].to_crs(grid.epsg) if src.get("land_use") is not None else None
    bld = assign_heights(bld, gba_p, cfg, lu)
    bld["source_dataset"] = bld["source_dataset"].fillna("unknown")
    if cfg.get("gba_footprints"):
        bld = conflate_gba(bld, gba_p[gba_p.within(frame)])
    bld = bld[bld.area >= float(cfg.get("min_footprint_m2", MIN_AREA))]
    base = base_heights(bld.geometry.values, dem, grid, cfg["terrain_res"])
    bld["base_z"] = base
    parts = None
    if src.get("part") is not None:
        parts = clean_footprints(src["part"].to_crs(grid.epsg), min_area=2.0)
        if "is_underground" in parts:
            parts = parts[~parts["is_underground"].fillna(False).astype(bool)]
    out = attach_parts(bld, parts, cfg)
    is_part = out["part_of"].notna().to_numpy()
    parent_base = bld.set_index("id")["base_z"]
    out.loc[is_part, "base_z"] = parent_base.loc[out.loc[is_part, "part_of"]].to_numpy()
    return separate_coplanar(out.reset_index(drop=True))


def building_mesh(bld: gpd.GeoDataFrame, grid: Grid) -> MeshData:
    parts, idx = shapely.get_parts(bld.geometry.values, return_index=True)
    is_part = bld["part_of"].notna().to_numpy()
    base = bld["base_z"].to_numpy(float)
    mh = bld["min_height"].fillna(0).to_numpy(float)
    z0 = np.where(is_part & (mh > 0), base + mh, base - BURY)
    z1 = base + bld["height"].to_numpy(float)
    return extrude(parts, z0[idx], z1[idx], grid, height_tint(bld["height"].to_numpy(float))[idx])


def landmark_info(cfg: dict, grid: Grid, bld: gpd.GeoDataFrame, dem: np.ndarray) -> list[dict]:
    tr = Transformer.from_crs(4326, grid.epsg, always_xy=True)
    out = []
    for lm in cfg.get("landmarks", []):
        e, n = tr.transform(lm["lon"], lm["lat"])
        pt = shapely.Point(e, n)
        d = bld.distance(pt)
        near = bld[d < 30]
        top = float((near["base_z"] + near["height"]).max()) if len(near) else None
        h = float(near["height"].max()) if len(near) else None
        ground = float(sample_bilinear(dem, grid, cfg["terrain_res"], [e], [n])[0])
        x, y, z = grid.to_local([e], [n], [ground])[0]
        out.append({"name": lm["name"], "lon": lm["lon"], "lat": lm["lat"], "e": e, "n": n,
                    "local": [round(float(x), 2), round(float(y), 2), round(float(z), 2)],
                    "height": h, "top_z": top,
                    "height_source": (near.loc[near["height"].idxmax(), "height_source"] if len(near) else None)})
    return out


def build_city(name: str, web_terrain_res: float = 15.0, web_max_mb: float = 8.0) -> Path:
    """Build out/cities/<name>/ per the city contract. Returns the output dir."""
    t0 = time.time()
    cfg = load_config(name)
    grid = Grid.from_bbox(cfg["bbox"], cfg["epsg"])
    cache, out_dir = DATA_DIR / "cities" / name, OUT_DIR / "cities" / name
    cache.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    _log(f"{name}: grid {grid.width:.0f} x {grid.height:.0f} m in {grid.crs}")

    src = extract_sources(cfg, grid, cache)

    tres = float(cfg["terrain_res"])
    ntr, nshape = grid.nodes(tres)
    dem = gedtm.to_grid(src["dtm"], grid.crs, ntr, nshape)
    water = water_polygons(src["water"]).to_crs(grid.epsg) if src.get("water") is not None else gpd.GeoSeries([])
    dem = smooth_under_water(dem, water_mask(water, ntr, nshape, sigma=1.0), sigma=8.0)
    _log(f"terrain {nshape} z {dem.min():.1f}..{dem.max():.1f} m")

    bld = build_buildings(src, grid, dem, cfg)
    counts = bld["height_source"].value_counts().to_dict()
    _log(f"buildings {len(bld)} ({int(bld['part_of'].notna().sum())} parts) {counts}; "
         f"max {bld['height'].max():.0f} m")

    xres = float(cfg["texture_res"])
    xres = max(xres, max(grid.width, grid.height) / 8192)
    xtr, xshape = grid.raster(xres)
    seg = src["segment"].to_crs(grid.epsg) if src.get("segment") is not None else None
    rgb, s2sel = sentinel_rgb(cfg, grid, water, xtr, xshape, cache)
    dv = cfg.get("drop_vegetated")  # small machine-learned specks on wooded slopes (OSM footprints kept)
    if dv:
        veg = vegetated(bld.geometry.values, rgb, xtr, float(dv.get("exg", 0.2)))
        veg &= ~bld["source_dataset"].eq("OpenStreetMap").to_numpy() & bld["part_of"].isna().to_numpy()
        veg &= (bld.area < float(dv.get("max_area_m2", 150))).to_numpy()
        bld = bld[~veg].reset_index(drop=True)
        counts = bld["height_source"].value_counts().to_dict()
        _log(f"dropped {int(veg.sum())} non-OSM footprints under vegetation -> {len(bld)} buildings")
    wmask = water_mask(water, xtr, xshape)
    ground = ground_texture(rgb, xtr, water, seg, water_style=cfg.get("water"), wmask=wmask)
    del rgb
    Image.fromarray(ground).save(out_dir / "ground.png", optimize=False, compress_level=6)
    # read by blender/render_city.py for the water material (not part of the web assets)
    Image.fromarray((wmask * 255 + 0.5).astype(np.uint8)).save(out_dir / "water_mask.png", compress_level=6)
    del wmask
    _log(f"ground texture {ground.shape[1]} x {ground.shape[0]} (S2 {s2sel['year']} {s2sel['quarter']})")

    bmesh = building_mesh(bld, grid)
    tmesh = terrain_mesh(dem, grid, jpeg_bytes(ground, 92))
    z_min = float(min(dem.min(), bmesh.positions[:, 1].min()))
    z_max = float(max(dem.max(), bmesh.positions[:, 1].max()))
    extras = {"epsg": grid.epsg, "origin": [grid.xmin, grid.ymin], "frame": "x=E-origin_e, y=z, z=-(N-origin_n)"}
    write_glb(out_dir / "city.glb", [tmesh, bmesh], extras)
    info = inspect_glb(out_dir / "city.glb")
    _log(f"city.glb {(out_dir / 'city.glb').stat().st_size / 1e6:.1f} MB nodes {info['nodes']} "
         f"terrain y {info['y_range'].get('terrain')} ({len(tmesh.positions)} + {len(bmesh.positions)} verts)")
    del tmesh

    web_size = build_web_glb(out_dir, dem, grid, tres, ground, bmesh, extras, web_terrain_res, web_max_mb)
    _log(f"city_web.glb {web_size / 1e6:.2f} MB")

    lms = landmark_info(cfg, grid, bld, dem)
    write_buildings(out_dir / "buildings.geoparquet", bld)
    meta = {
        "name": name, "title": cfg.get("title"), "epsg": grid.epsg, "bbox": cfg["bbox"],
        "origin": [grid.xmin, grid.ymin], "bounds_proj": list(grid.bounds), "extent_m": [grid.width, grid.height],
        "terrain_res": tres, "texture_res": xres, "terrain_shape": list(dem.shape),
        "texture_shape": list(ground.shape[:2]),
        "n_buildings": len(bld), "n_parts": int(bld["part_of"].notna().sum()),
        "height_source_counts": {k: int(v) for k, v in counts.items()},
        "height_max": float(bld["height"].max()), "z_min": z_min, "z_max": z_max,
        "terrain_z": [float(dem.min()), float(dem.max())],
        "landmarks": lms, "camera": cfg.get("camera"), "sun": cfg.get("sun"),
        "sources": {"overture_release": src["release"], "gba_tile": cfg.get("gba_tile"),
                    "dtm": "GEDTM30", "sentinel2": {k: s2sel[k] for k in ("year", "quarter", "tiles")}},
        "attribution": ATTRIBUTION, "license_note": "non-commercial (GlobalBuildingAtlas CC BY-NC 4.0)",
        "build_seconds": round(time.time() - t0, 1),
    }
    (out_dir / "city.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False, default=_json_default))
    preview(out_dir / "preview.png", ground, grid, bld, f"{name}: {len(bld)} buildings, S2 {s2sel['year']} "
            f"{s2sel['quarter']}, max {bld['height'].max():.0f} m", lms)
    _log(f"done {name} in {time.time() - t0:.0f} s")
    return out_dir


def _json_default(o):
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(type(o))


def build_web_glb(out_dir: Path, dem: np.ndarray, grid: Grid, tres: float, ground: np.ndarray,
                  bmesh: MeshData, extras: dict, web_res: float, max_mb: float) -> int:
    """Coarser terrain + JPEG texture, meshopt-compressed; shrink until <= max_mb."""
    tmp, dst = out_dir / "_web_src.glb", out_dir / "city_web.glb"
    size = 0
    for res, side, quality in ((web_res, 4096, 85), (web_res, 3072, 82), (web_res * 1.5, 2048, 80),
                               (web_res * 2, 1536, 75)):
        h, w = round(grid.height / res) + 1, round(grid.width / res) + 1
        e, n = np.meshgrid(np.linspace(grid.xmin, grid.xmax, w), np.linspace(grid.ymax, grid.ymin, h))
        wdem = sample_bilinear(dem, grid, tres, e, n).astype(np.float32)
        tm = terrain_mesh(wdem, grid, jpeg_bytes(ground, quality, side))
        write_glb(tmp, [tm, bmesh], extras)
        gltfpack(tmp, dst)
        size = dst.stat().st_size
        if size <= max_mb * 1e6:
            break
    tmp.unlink(missing_ok=True)
    return size


def write_buildings(path: Path, bld: gpd.GeoDataFrame) -> None:
    cols = ["id", "name", "height", "height_source", "base_z", "min_height", "class", "source_dataset", "part_of"]
    out = bld[[c for c in cols if c in bld] + ["geometry"]].copy()
    for c in ("class", "name", "part_of", "source_dataset"):
        if c in out:
            out[c] = out[c].astype(object).where(out[c].notna(), None)
    out = out.to_crs(4326)
    tmp = path.with_suffix(".part")
    out.to_parquet(tmp)
    shutil.move(tmp, path)
