"""Scene frame (a regular grid in the scene CRS) and elevation assembly.

Land heights come from Copernicus DEM tiles (mosaicked in geographic
coordinates per chunk, then warped), the coastline from Natural Earth land,
and sea depths from GEBCO with a short feather off the coast.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cached_property

import numpy as np

import chinamaps.net  # noqa: F401  (sets GDAL env before rasterio is used)
from chinamaps.sources import copernicus, gebco

M_PER_DEG = 111_320.0
HALO = 8  # px of overlap around each chunk
FEATHER_PX = 3.0  # sea-side ramp from the coast to full GEBCO depth


@dataclass(frozen=True)
class Frame:
    crs: str
    bounds: tuple[float, float, float, float]  # xmin, ymin, xmax, ymax (metres)
    dem_res: float
    texture_res: float

    @property
    def factor(self) -> int:
        return int(round(self.dem_res / self.texture_res))

    def shape(self, res: float) -> tuple[int, int]:
        xmin, ymin, xmax, ymax = self.bounds
        return int(round((ymax - ymin) / res)), int(round((xmax - xmin) / res))

    @property
    def dem_shape(self) -> tuple[int, int]:
        return self.shape(self.dem_res)

    @property
    def texture_shape(self) -> tuple[int, int]:
        return self.shape(self.texture_res)

    def transform(self, res: float):
        from rasterio.transform import from_origin

        return from_origin(self.bounds[0], self.bounds[3], res, res)

    @cached_property
    def to_lonlat(self):
        from pyproj import Transformer

        return Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)

    def window_lonlat_bbox(self, x0: float, y0: float, x1: float, y1: float, n: int = 64) -> tuple[float, ...]:
        """Lon/lat bbox of a projected rectangle (densified edges, inverse-projected)."""
        lon, lat = self.to_lonlat.transform(*_ring(x0, y0, x1, y1, n))
        return float(np.min(lon)), float(np.min(lat)), float(np.max(lon)), float(np.max(lat))

    def lonlat_polygon(self, n: int = 256):
        from shapely.geometry import Polygon

        lon, lat = self.to_lonlat.transform(*_ring(*self.bounds, n))
        return Polygon(np.column_stack([lon, lat]))

    @property
    def center_lonlat(self) -> tuple[float, float]:
        xmin, ymin, xmax, ymax = self.bounds
        lon, lat = self.to_lonlat.transform((xmin + xmax) / 2, (ymin + ymax) / 2)
        return float(lon), float(lat)


def _ring(x0: float, y0: float, x1: float, y1: float, n: int) -> tuple[np.ndarray, np.ndarray]:
    t = np.linspace(0.0, 1.0, n, endpoint=False)
    xs = np.concatenate([x0 + (x1 - x0) * t, np.full(n, x1), x1 - (x1 - x0) * t, np.full(n, x0)])
    ys = np.concatenate([np.full(n, y0), y0 + (y1 - y0) * t, np.full(n, y1), y1 - (y1 - y0) * t])
    return xs, ys


def make_frame(cfg: dict, draft: bool = False) -> Frame:
    """Projected bbox of the densified lon/lat bounds, snapped outward to dem_res."""
    from pyproj import Transformer

    scale = 2 if draft else 1
    dem_res = float(cfg["dem_res"]) * scale
    tex_res = float(cfg.get("texture_res", cfg["dem_res"])) * scale
    if abs(dem_res / tex_res - round(dem_res / tex_res)) > 1e-9:
        raise ValueError(f"dem_res {dem_res} must be a multiple of texture_res {tex_res}")
    fwd = Transformer.from_crs("EPSG:4326", cfg["crs"], always_xy=True)
    x, y = fwd.transform(*_ring(*cfg["bounds"], 256))
    r = dem_res
    b = (math.floor(min(x) / r) * r, math.floor(min(y) / r) * r, math.ceil(max(x) / r) * r, math.ceil(max(y) / r) * r)
    return Frame(cfg["crs"], tuple(float(v) for v in b), dem_res, tex_res)


def frame_cells(frame: Frame) -> list[tuple[int, int]]:
    """1-degree cells (lat, lon) intersecting the frame footprint."""
    from shapely.geometry import box

    poly = frame.lonlat_polygon().buffer(0.02)
    return [c for c in copernicus.cells_in_bbox(*poly.bounds) if poly.intersects(box(c[1], c[0], c[1] + 1, c[0] + 1))]


def chunks(shape: tuple[int, int], size: int):
    """Yield (row0, col0, height, width) blocks covering shape."""
    for r in range(0, shape[0], size):
        for c in range(0, shape[1], size):
            yield r, c, min(size, shape[0] - r), min(size, shape[1] - c)


def land_geometries(frame: Frame):
    """Natural Earth land (+ minor islands) projected to the frame CRS."""
    import geopandas as gpd
    import pandas as pd

    from chinamaps.sources import naturalearth as ne

    bbox = frame.lonlat_polygon().buffer(0.5).bounds
    parts = [ne.read(n).clip(bbox) for n in ("ne_10m_land", "ne_10m_minor_islands")]
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326)
    gdf = gdf[~gdf.geometry.is_empty]
    return list(gdf.to_crs(frame.crs).geometry.values)


def _warp(src: np.ndarray, src_transform, dst_shape, dst_transform, dst_crs, nodata=np.nan, resampling=None):
    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    dst = np.full(dst_shape, np.nan, dtype=np.float32)
    reproject(src, dst, src_transform=src_transform, src_crs="EPSG:4326", src_nodata=nodata,
              dst_transform=dst_transform, dst_crs=dst_crs, dst_nodata=np.nan,
              resampling=resampling or Resampling.bilinear)
    return dst


def build_elevation(frame: Frame, cfg: dict, chunk: int = 4096, workers: int = 8) -> tuple[np.ndarray, np.ndarray]:
    """Elevation (float32) and land mask (bool) on the texture grid. Row 0 = north."""
    import rasterio
    from rasterio.features import rasterize
    from rasterio.transform import from_origin
    from rasterio.windows import from_bounds
    from scipy.ndimage import distance_transform_edt

    prod = copernicus.product(cfg["dem"])
    res = frame.texture_res
    cells = frame_cells(frame)
    ppd, paths = copernicus.fetch_tiles(cells, prod, M_PER_DEG / res, workers=workers)
    bathy = bool(cfg.get("bathymetry", False))
    gpath = None
    if bathy:
        gpath = gebco.window(*frame.lonlat_polygon().buffer(0.5).bounds, M_PER_DEG / frame.dem_res)
    land_geoms = land_geometries(frame)

    H, W = frame.texture_shape
    z_out = np.empty((H, W), dtype=np.float32)
    land_out = np.empty((H, W), dtype=bool)
    xmin, ymax = frame.bounds[0], frame.bounds[3]
    for r0, c0, h, w in chunks((H, W), chunk):
        # chunk grid with a halo so the coastal feather does not see chunk edges
        hr, hc = r0 - HALO, c0 - HALO
        shp = (h + 2 * HALO, w + 2 * HALO)
        x0, y1 = xmin + hc * res, ymax - hr * res
        x1, y0 = x0 + shp[1] * res, y1 - shp[0] * res
        tr = from_origin(x0, y1, res, res)
        lo0, la0, lo1, la1 = frame.window_lonlat_bbox(x0, y0, x1, y1)
        m = 3.0 / ppd
        ccells = copernicus.cells_in_bbox(lo0 - m, la0 - m, lo1 + m, la1 + m)
        mos, mtr = copernicus.mosaic(paths, ccells, ppd, prod)
        z = _warp(mos, mtr, shp, tr, frame.crs)
        del mos
        has_tile = np.isfinite(z)
        land = rasterize(((g, 1) for g in land_geoms), out_shape=shp, transform=tr, fill=0, dtype="uint8").astype(bool)
        land &= has_tile
        z = np.where(has_tile, z, np.float32(0))
        if bathy:
            with rasterio.open(gpath) as ds:
                win = from_bounds(lo0 - 0.1, la0 - 0.1, lo1 + 0.1, la1 + 0.1, ds.transform)
                win = win.round_offsets().round_lengths()
                g = ds.read(1, window=win, boundless=True, fill_value=0).astype(np.float32)
                gtr = ds.window_transform(win)
            sea = _warp(g, gtr, shp, tr, frame.crs, nodata=None)
            sea = np.minimum(np.nan_to_num(sea, nan=0.0), np.float32(-1.0))
            d = distance_transform_edt(~land).astype(np.float32)
            ramp = np.clip(d / np.float32(FEATHER_PX), 0, 1)
            z = np.where(land, z, sea * ramp)
        else:
            z = np.where(land, z, np.float32(0))
        z_out[r0 : r0 + h, c0 : c0 + w] = z[HALO : HALO + h, HALO : HALO + w]
        land_out[r0 : r0 + h, c0 : c0 + w] = land[HALO : HALO + h, HALO : HALO + w]
    return z_out, land_out


def block_mean(a: np.ndarray, f: int) -> np.ndarray:
    """Average f x f blocks (shape must be divisible by f)."""
    if f == 1:
        return a.astype(np.float32, copy=True)
    h, w = a.shape[0] // f, a.shape[1] // f
    return a[: h * f, : w * f].reshape(h, f, w, f).mean(axis=(1, 3), dtype=np.float32)


def drainage(z: np.ndarray, no_exit: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Flow routing on a grid: (parent, upstream cell count), both flat (H*W,).

    Priority-flood routing done as a minimum spanning tree (scipy, in C): edge
    weights rank cells by the higher of their two elevations, so tree paths to
    the frame edge never climb more than needed (depressions are filled), and
    ties prefer the steepest drop. Flats drain through their lowest outlet;
    cells with no lower or equal neighbour on the frame edge drain out (parent -1),
    except where no_exit is set (e.g. where a river flows into the frame).
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import breadth_first_order, minimum_spanning_tree

    H, W = z.shape
    n = H * W
    rank = np.unique(z.ravel(), return_inverse=True)[1].reshape(H, W).astype(np.float64)
    idx = np.arange(n, dtype=np.int64).reshape(H, W)
    src, dst, wts, slopes = [], [], [], []
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        ys, yd = slice(0, H - dy), slice(dy, H)
        xs, xd = (slice(0, W - dx), slice(dx, W)) if dx >= 0 else (slice(-dx, W), slice(0, W + dx))
        slopes.append((np.abs(z[ys, xs] - z[yd, xd]) / np.hypot(dy, dx)).ravel())
        wts.append((np.maximum(rank[ys, xs], rank[yd, xd]) + 1).ravel())
        src.append(idx[ys, xs].ravel())
        dst.append(idx[yd, xd].ravel())
    smax = max(float(sl.max()) for sl in slopes) + 1e-9
    for w, sl in zip(wts, slopes):
        w += 0.01 + 0.98 * (1 - sl / smax)  # within a rank, steeper edges first
    del slopes
    edge = np.zeros((H, W), bool)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    if no_exit is not None and (edge & ~no_exit).any():
        edge &= ~no_exit
    src.append(idx[edge])
    dst.append(np.full(int(edge.sum()), n))
    wts.append(rank[edge] + 1.995)  # leave the frame only if no inner edge of the same rank (flats drain inward)
    g = coo_matrix((np.concatenate(wts), (np.concatenate(src), np.concatenate(dst))), shape=(n + 1, n + 1))
    del src, dst, wts
    t = minimum_spanning_tree(g.tocsr())
    t = t + t.T
    _, pred = breadth_first_order(t, n, directed=False, return_predecessors=True)
    parent = np.append(pred[:n], n).astype(np.int64)  # root points to itself
    parent[n] = n
    # depth by pointer jumping, then accumulate level by level from the deepest
    depth = np.ones(n + 1, np.int64)
    depth[n] = 0
    anc = parent.copy()
    while (anc != n).any():
        depth += depth[anc] * (anc != n)
        anc = anc[anc]
    order = np.argsort(depth[:n], kind="stable")
    bounds = np.searchsorted(depth[:n][order], np.arange(depth.max() + 2))
    acc = np.ones(n + 1, np.float64)
    for d in range(int(depth.max()), 0, -1):
        nodes = order[bounds[d] : bounds[d + 1]]
        np.add.at(acc, parent[nodes], acc[nodes])
    parent = parent[:n]
    parent[parent == n] = -1
    return parent, acc[:n]


INFLOW_MARGIN = 150_000.0  # m of terrain routed around a regional frame to size inflowing rivers
ROUTING_MAX_PX = 1600  # longest side of the routing grid


def coarse_inflows(frame: Frame, a_min_km2: float) -> list[tuple[float, float, float]]:
    """Rivers entering the frame: (x, y, upstream km2) from coarse routing of the
    frame plus a margin (Copernicus overview tiles)."""
    m = INFLOW_MARGIN
    xmin, ymin, xmax, ymax = frame.bounds
    cres = max(frame.texture_res * 4, math.ceil(max(xmax - xmin, ymax - ymin, 1) / 1400 + 2 * m / 1400))
    cres = float(math.ceil(cres / 10) * 10)
    b = (math.floor((xmin - m) / cres) * cres, math.floor((ymin - m) / cres) * cres,
         math.ceil((xmax + m) / cres) * cres, math.ceil((ymax + m) / cres) * cres)
    ext = Frame(frame.crs, b, cres, cres)
    z, _ = build_elevation(ext, {"dem": "glo30-ovr2", "bathymetry": False})
    parent, acc = drainage(z)
    h, w = z.shape
    rows, cols = np.divmod(np.arange(h * w), w)
    x = b[0] + (cols + 0.5) * cres
    y = b[3] - (rows + 0.5) * cres
    inside = (x > xmin) & (x < xmax) & (y > ymin) & (y < ymax)
    area = acc * (cres / 1000) ** 2
    p = np.maximum(parent, 0)
    cross = ~inside & (parent >= 0) & inside[p] & (area >= a_min_km2)
    i = np.flatnonzero(cross)
    return [(float((x[k] + x[p[k]]) / 2), float((y[k] + y[p[k]]) / 2), float(area[k])) for k in i]


def river_network(frame: Frame, z: np.ndarray, a_min_km2: float, inflows=(), exclude=None,
                  search_m: float = 1500.0) -> list[tuple[np.ndarray, np.ndarray]]:
    """DEM-derived river links: [(xy, area_km2)], xy in global texture pixels
    (x right, y up from the south edge), one entry per link between junctions.

    inflows (x, y, km2) add upstream area at the lowest routed cell near each
    point; exclude (bool, texture grid) removes cells (e.g. glaciers) from the network.
    """
    H, W = z.shape
    s = max(1, math.ceil(max(H, W) / ROUTING_MAX_PX))
    zc = block_mean(z[: H // s * s, : W // s * s], s)
    h, w = zc.shape
    cell = frame.texture_res * s
    rad = max(2, int(round(search_m / cell)))
    rc = [(int((frame.bounds[3] - y) / cell), int((x - frame.bounds[0]) / cell)) for x, y, _ in inflows]
    no_exit = np.zeros((h, w), bool)
    for r, c in rc:  # where a river flows in, the frame edge is not an outlet
        no_exit[max(0, r - rad) : r + rad + 1, max(0, c - rad) : c + rad + 1] = True
    parent, acc = drainage(zc, no_exit)
    area = acc * (cell / 1000) ** 2
    for (r, c), (_, _, a) in zip(rc, inflows):
        r0, r1 = max(2, r - rad), min(h - 2, r + rad + 1)
        c0, c1 = max(2, c - rad), min(w - 2, c + rad + 1)
        if r0 >= r1 or c0 >= c1:
            continue
        win = zc[r0:r1, c0:c1]
        rr, cc = np.unravel_index(int(np.argmin(win)), win.shape)
        i = (r0 + rr) * w + c0 + cc
        while i >= 0:
            area[i] += a
            i = parent[i]
    river = area >= a_min_km2
    rows, cols = np.divmod(np.arange(h * w), w)
    on_edge = (rows == 0) | (rows == h - 1) | (cols == 0) | (cols == w - 1)
    river &= ~(on_edge & on_edge[np.maximum(parent, 0)] & (parent >= 0))  # no runs along the frame edge
    if exclude is not None:
        river &= ~(block_mean(exclude[: h * s, : w * s].astype(np.float32), s) > 0.5).ravel()
    return _links(parent, river, area, s, H, w)


def _links(parent: np.ndarray, river: np.ndarray, area: np.ndarray, s: int, H: int, w: int):
    """Split the river tree into links between heads, junctions and outlets."""
    n = parent.size
    idx = np.flatnonzero(river)
    par = parent[idx]
    ok = par >= 0
    ok[ok] = river[par[ok]]
    nchild = np.bincount(par[ok], minlength=n)
    starts = idx[(nchild[idx] == 0) | (nchild[idx] >= 2)]
    out = []
    for st in starts:
        path = [st]
        i = parent[st]
        while i >= 0 and river[i]:
            path.append(i)
            if nchild[i] >= 2:
                break
            i = parent[i]
        if len(path) < 2:
            continue
        p = np.asarray(path)
        r, c = np.divmod(p, w)
        xy = np.column_stack([(c + 0.5) * s, H - (r + 0.5) * s]).astype(np.float64)
        out.append((xy, area[p]))
    return out
