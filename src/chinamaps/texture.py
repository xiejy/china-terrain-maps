"""Scene texture: hypsometric tint (with forest and snow hints) x soft MDOW
hillshade, soft-light texture shading, gentle slope darkening, then anti-aliased
vectors (boundaries, South China Sea dashes, provinces, rivers, lakes, glaciers)
burned in with matplotlib Agg.

The texture is processed in tiles with a halo. Vectors are drawn in global
texture-pixel coordinates, so every tile rasterises them identically and no
seams appear. Shading is soft because Blender casts the real shadows.

Rivers: Natural Earth for the China overview; for regional scenes they are
traced from the DEM itself (so they sit exactly in the valleys), with inflow
from upstream terrain outside the frame.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from chinamaps import shade, tint

TILE = 2048
HALO = 24  # px; covers the Horn window and the forest-mask blur
RENDER_WIDTH = 3840  # final render width the line weights are tuned for
MIN_PX = 0.9  # thinnest line drawn into the texture
HS_ALTITUDE = 45.0

RIVER = (46, 136, 206)  # saturated so rivers read on shadowed valley floors in oblique renders
RIVER_MUTED = (140, 166, 186)
LAKE = (150, 190, 214)
CHINA_LINE = (148, 46, 36)
HALO_COLOR = (255, 251, 244)
GLOW_COLOR = (255, 240, 205)  # soft outer glow along China's land border (national map)
OTHER_LINE = (116, 116, 116)
PROVINCE = (255, 250, 240)
GLACIER = (242, 247, 252)


@dataclass
class Layer:
    """Vector layer in global texture-pixel coordinates (x right, y up from the south edge)."""

    kind: str  # "line" or "poly"
    parts: list[np.ndarray]
    color: tuple[int, int, int]
    width: float | np.ndarray = 1.0  # px, scalar or one per part
    alpha: float = 1.0
    boxes: np.ndarray = field(init=False)

    def __post_init__(self):
        if self.parts:
            vs = [p[0] if isinstance(p, tuple) else p for p in self.parts]
            self.boxes = np.array([[v[:, 0].min(), v[:, 1].min(), v[:, 0].max(), v[:, 1].max()] for v in vs])
        else:
            self.boxes = np.zeros((0, 4))
        self.width = np.broadcast_to(np.asarray(self.width, dtype=float), (len(self.parts),)).copy()

    def select(self, x0: float, y0: float, x1: float, y1: float, pad: float) -> np.ndarray:
        b = self.boxes
        if not len(b):
            return np.zeros(0, dtype=int)
        m = (b[:, 2] >= x0 - pad) & (b[:, 0] <= x1 + pad) & (b[:, 3] >= y0 - pad) & (b[:, 1] <= y1 + pad)
        return np.nonzero(m)[0]


# ---------------------------------------------------------------- shading


def compose_tile(z: np.ndarray, land: np.ndarray, weights: dict, neighbour: np.ndarray | None,
                 ts: np.ndarray, res: float, zf: float, pal: dict) -> np.ndarray:
    """RGB float32 for one tile: tint, forest/snow hints, neighbour greying, ocean, then shading."""
    p, q = shade.horn(z, res)
    slope = shade.slope_deg(p, q)
    north = -q / np.maximum(np.hypot(p, q), np.float32(1e-6))  # 1 = faces north
    col = tint.land_color(z, weights, pal=pal)
    col = tint.surface_hints(col, z, slope, weights, weights["lat"], pal, res, north)
    if neighbour is not None:
        mix = pal["neighbour_mix"]
        c = np.asarray(mix["color"], dtype=np.float32) / 255
        col = col + (np.float32(mix["amount"]) * neighbour)[..., None] * (c - col)
        lighten = np.float32(mix.get("lighten", 0.0)) * neighbour
        col = col + lighten[..., None] * (1 - col)
    if not land.all():
        col = np.where(land[..., None], col, tint.ocean_color(z, pal))
    zf32 = np.float32(zf)
    return shade_color(col, land, p * zf32, q * zf32, ts)


def shade_color(color: np.ndarray, land: np.ndarray, p: np.ndarray, q: np.ndarray, ts: np.ndarray) -> np.ndarray:
    """Soft MDOW hillshade, soft-light texture shading and slope darkening (p, q exaggerated)."""
    hs = shade.mdow(p, q, HS_ALTITUDE) / np.float32(math.sin(math.radians(HS_ALTITUDE)))
    curved = np.clip(hs, 0, 1.35)
    k_hs = np.where(land, np.float32(0.30), np.float32(0.22))
    color = color * (1 + k_hs * (curved - 1))[..., None]

    b = (np.float32(0.5) + np.float32(0.5) * ts)[..., None]
    soft = (1 - 2 * b) * color * color + 2 * b * color
    k_ts = np.where(land, np.float32(0.30), np.float32(0.15))[..., None]
    color = color + k_ts * (soft - color)

    s = np.clip(shade.slope_deg(p, q) / np.float32(55.0), 0, 1)
    dark = np.float32(1) - np.float32(0.14) * s * s * (3 - 2 * s) * land
    return np.clip(color * dark[..., None], 0, 1)


# ---------------------------------------------------------------- vectors


def _lines(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type in ("MultiLineString", "GeometryCollection"):
        return [g for part in geom.geoms for g in _lines(part)]
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return _lines(geom.boundary)
    return []


def _polys(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [g for part in geom.geoms for g in _polys(part)]
    return []


class Pixels:
    """Projected metres -> global texture pixels (x right, y up)."""

    def __init__(self, frame):
        self.xmin, self.ymin = frame.bounds[0], frame.bounds[1]
        self.res = frame.texture_res

    def __call__(self, coords) -> np.ndarray:
        a = np.asarray(coords, dtype=np.float64)[:, :2]
        return np.column_stack([(a[:, 0] - self.xmin) / self.res, (a[:, 1] - self.ymin) / self.res])


def _poly_path(poly, px: Pixels):
    from matplotlib.path import Path
    from shapely.geometry.polygon import orient

    poly = orient(poly, 1.0)
    verts, codes = [], []
    for ring in [poly.exterior, *poly.interiors]:
        v = px(ring.coords)
        verts.append(v)
        c = np.full(len(v), Path.LINETO, dtype=np.uint8)
        c[0], c[-1] = Path.MOVETO, Path.CLOSEPOLY
        codes.append(c)
    return np.concatenate(verts), np.concatenate(codes)


def dashes(line, dash: float, gap: float) -> list:
    """Split a line into dash segments (lengths in the line's units)."""
    from shapely.ops import substring

    out = []
    for part in _lines(line):
        n = part.length
        s = 0.0
        while s < n:
            seg = substring(part, s, min(s + dash, n))
            if seg.geom_type == "LineString" and seg.length > 0:
                out.append(seg)
            s += dash + gap
    return out


def _project(gdf, frame, clip_poly):
    gdf = gdf[gdf.intersects(clip_poly)]
    if gdf.empty:
        return gdf
    gdf = gdf.copy()
    gdf["geometry"] = gdf.geometry.intersection(clip_poly)
    gdf = gdf[~gdf.geometry.is_empty]
    return gdf.to_crs(frame.crs)


def river_width(scalerank: np.ndarray) -> np.ndarray:
    """Line width in render pixels by Natural Earth scalerank (0 = biggest)."""
    return np.clip(1.75 - 0.15 * np.asarray(scalerank, dtype=float), 0.45, 1.6)


def province_lines(frame, clip_poly):
    """Shared edges between Chinese provinces (Taiwan, Hong Kong, Macao each one unit)."""
    import geopandas as gpd
    from shapely.ops import unary_union

    from chinamaps.sources import naturalearth as ne

    p = ne.provinces("cn")
    p["unit"] = np.where(p["adm0_a3"] == "CHN", p["name"], p["adm0_a3"])
    p = p.dissolve(by="unit").reset_index()
    p = _project(p[["unit", "geometry"]], frame, clip_poly)
    geoms = list(p.geometry.values)
    tol = 2.0 * frame.texture_res
    edges = []
    for i, g in enumerate(geoms[:-1]):  # each shared edge once, from its lower-index side
        others = unary_union(geoms[i + 1 :]).buffer(tol)
        edges.extend(_lines(g.boundary.intersection(others)))
    return gpd.GeoSeries(edges, crs=frame.crs).simplify(0.5 * frame.texture_res).values


def boundaries(worldview: str, bbox):
    """International land boundaries in bbox, or None when fewer than two countries touch it."""
    from shapely.geometry import box

    from chinamaps.sources import naturalearth as ne

    c = ne.countries(worldview)
    if c.intersects(box(*bbox)).sum() < 2:  # boundary_lines() cannot build an empty frame
        return None
    return ne.boundary_lines(worldview, bbox)


def ne_river_layers(frame, clip_poly, px, u: float, china_geom=None, color=RIVER) -> list[Layer]:
    """Natural Earth rivers (scalerank widths); outside China muted when china_geom is given."""
    import shapely

    from chinamaps.sources import naturalearth as ne

    rv = ne.read("ne_10m_rivers_lake_centerlines_scale_rank")
    rv = rv[rv["scalerank"] <= 8]
    rv = _project(rv[["scalerank", "featurecla", "geometry"]], frame, clip_poly)
    geoms = rv.geometry.simplify(0.4 * frame.texture_res).values
    w = river_width(rv["scalerank"].values) * np.where(rv["featurecla"].str.contains("Intermittent"), 0.7, 1.0)
    if china_geom is None:
        inside, outside = geoms, None
    else:
        shapely.prepare(china_geom)
        inside = shapely.intersection(geoms, china_geom)
        outside = shapely.difference(geoms, china_geom)

    def layer(gs, color, scale, alpha):
        parts, widths = [], []
        for g, wi in zip(gs, w):
            for line in _lines(g):
                parts.append(px(line.coords))
                widths.append(max(MIN_PX, wi * scale * u))
        return Layer("line", parts, color, np.array(widths), alpha=alpha)

    out = [layer(inside, color, 1.0, 1.0)]
    if outside is not None:
        out.insert(0, layer(outside, RIVER_MUTED, 0.75, 1.0))
    return out


def dem_river_layer(frame, z: np.ndarray, u: float, color=RIVER, max_m: float = 200.0) -> Layer:
    """Rivers traced on the DEM, widths from drainage area (render-scaled or true width, the latter
    capped at max_m metres)."""
    from chinamaps import dem

    xmin, ymin, xmax, ymax = frame.bounds
    frame_km2 = (xmax - xmin) * (ymax - ymin) / 1e6
    a_min = max(2.0, 0.003 * frame_km2)
    inflows = dem.coarse_inflows(frame, a_min)
    lat = frame.center_lonlat[1]
    snow = z > tint.snowline(lat) - 600  # no rivers on glacier tongues
    links = dem.river_network(frame, z, a_min, inflows=inflows, exclude=snow)
    parts, widths = [], []
    for xy, area in links:
        w_render = np.clip(0.5 + 0.5 * np.log10(area / a_min), 0.5, 3.0) * u
        w_true = np.minimum(2.0 * np.sqrt(area), max_m) / frame.texture_res
        w = np.round(np.maximum(np.maximum(w_render, w_true), MIN_PX) * 4) / 4  # quarter-px classes
        cut = np.flatnonzero(np.diff(w)) + 1
        for a, b in zip(np.r_[0, cut], np.r_[cut, len(w)]):
            seg = xy[max(a - 1, 0) : b + (1 if b < len(w) else 0)]  # overlap one vertex at class changes
            if len(seg) >= 2:
                parts.append(smooth_line(seg, 2))
                widths.append(float(w[a]))
    return Layer("line", parts, color, np.array(widths))  # opaque: overlapping joints stay even


def smooth_line(xy: np.ndarray, n: int = 2, window: int = 5) -> np.ndarray:
    """Moving-average then Chaikin corner cutting, endpoints fixed (smooths D8 staircases)."""
    if len(xy) > window:
        k = np.ones(window) / window
        pad = np.pad(xy, ((window // 2, window // 2), (0, 0)), mode="edge")
        sm = np.column_stack([np.convolve(pad[:, i], k, mode="valid") for i in range(2)])
        sm[0], sm[-1] = xy[0], xy[-1]
        xy = sm
    for _ in range(n):
        if len(xy) < 3:
            return xy
        a, b = xy[:-1], xy[1:]
        mid = np.empty((2 * len(a), 2))
        mid[0::2] = 0.75 * a + 0.25 * b
        mid[1::2] = 0.25 * a + 0.75 * b
        xy = np.vstack([xy[:1], mid[1:-1], xy[-1:]])
    return xy


def vector_layers(frame, cfg: dict, z: np.ndarray | None = None, china_geom=None) -> list[Layer]:
    """Projected, styled vector layers in drawing order."""
    from chinamaps.sources import naturalearth as ne

    px = Pixels(frame)
    # texture px per final-render px; line_scale > 1 thickens lines for oblique views, where the
    # foreshortened texture covers only part of the frame width
    u = frame.texture_shape[1] / RENDER_WIDTH * float(cfg.get("line_scale", 1.0))
    # rivers and borders may be weighted separately (river_scale / border_scale multiply line_scale)
    u_river = u * float(cfg.get("river_scale", 1.0))
    u_border = u * float(cfg.get("border_scale", 1.0))
    river = tuple(cfg.get("river_color", RIVER))

    def wpx(w: float, unit: float = u) -> float:
        return max(MIN_PX, w * unit)

    clip_poly = frame.lonlat_polygon().buffer(0.3)
    bbox = clip_poly.bounds
    is_china = bool(cfg.get("highlight_china", False))
    overview = frame.texture_res >= 200
    simp = 0.4 * frame.texture_res

    def lines_of(gdf) -> list[np.ndarray]:
        return [px(line.coords) for g in gdf.geometry.simplify(simp).values for line in _lines(g)]

    layers: list[Layer] = []
    if overview:
        gl = _project(ne.read("ne_10m_glaciated_areas")[["geometry"]], frame, clip_poly)
        layers.append(Layer("poly", [_poly_path(p, px) for g in gl.geometry.simplify(simp) for p in _polys(g)],
                            GLACIER, alpha=0.35))
        layers += ne_river_layers(frame, clip_poly, px, u_river, china_geom if is_china else None, river)
    elif z is not None:
        layers.append(dem_river_layer(frame, z, u_river, river, float(cfg.get("river_max_m", 200.0))))

    lk = _project(ne.read("ne_10m_lakes")[["geometry"]], frame, clip_poly)
    lake_polys = [p for g in lk.geometry.simplify(simp) for p in _polys(g)]
    layers.append(Layer("poly", [_poly_path(p, px) for p in lake_polys], LAKE))
    layers.append(Layer("line", [px(line.coords) for p in lake_polys for line in _lines(p)], river,
                        wpx(0.45), alpha=0.8))

    if is_china:
        dash, gap = 5.0 * u * frame.texture_res, 3.0 * u * frame.texture_res
        segs = [d for line in province_lines(frame, clip_poly) for d in dashes(line, dash, gap)]
        layers.append(Layer("line", [px(s.coords) for s in segs], PROVINCE, wpx(0.7), alpha=0.85))

    # borders: false drops 1:10M boundaries from close-ups where they visibly miss the ridge crests
    b = boundaries(cfg.get("worldview", "cn"), bbox) if cfg.get("borders", True) else None
    b = _project(b, frame, clip_poly) if b is not None else None
    cn_parts: list[np.ndarray] = []
    if b is not None and not b.empty:
        other, cn = b[~b["china"]], b[b["china"]]
        layers.append(Layer("line", lines_of(other), HALO_COLOR, wpx(2.0), alpha=0.35))
        layers.append(Layer("line", lines_of(other), OTHER_LINE, wpx(0.85)))
        cn_parts = lines_of(cn)
    glow = cfg.get("border_glow")  # {width: render px, alpha}: stacked lines approximate a soft glow
    if glow and cn_parts:
        for f in (1.0, 0.7, 0.45):
            layers.append(Layer("line", cn_parts, GLOW_COLOR, wpx(float(glow["width"]) * f, u_border),
                                alpha=float(glow["alpha"]) / 2.2))
    if is_china:
        cn_parts += lines_of(_project(ne.scs_dashes()[["geometry"]], frame, clip_poly))
    layers.append(Layer("line", cn_parts, HALO_COLOR, wpx(3.4, u_border), alpha=0.6))
    layers.append(Layer("line", cn_parts, CHINA_LINE, wpx(1.6, u_border)))
    return layers


def render_vectors(layers: list[Layer], x0: float, y0: float, w: int, h: int) -> np.ndarray:
    """RGBA float32 (h, w, 4) of the layers over global pixel window [x0, x0+w] x [y0, y0+h]."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.collections import LineCollection, PathCollection
    from matplotlib.figure import Figure
    from matplotlib.path import Path

    dpi = 72.0  # 1 pt == 1 px
    fig = Figure(figsize=(w / dpi, h / dpi), dpi=dpi)
    fig.patch.set_alpha(0)
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    ax.set_xlim(x0, x0 + w)
    ax.set_ylim(y0, y0 + h)
    drawn = False
    for z, layer in enumerate(layers):
        idx = layer.select(x0, y0, x0 + w, y0 + h, pad=float(np.max(layer.width, initial=1.0)) * 2)
        if not len(idx):
            continue
        rgba = (*[c / 255 for c in layer.color], layer.alpha)
        if layer.kind == "line":
            col = LineCollection([layer.parts[i] for i in idx], linewidths=layer.width[idx], colors=[rgba],
                                 capstyle="round", joinstyle="round", antialiased=True, zorder=z)
        else:
            col = PathCollection([Path(*layer.parts[i]) for i in idx], facecolors=[rgba], edgecolors="none",
                                 antialiased=True, zorder=z)
        col.set_snap(False)
        ax.add_collection(col)
        drawn = True
    if not drawn:
        return np.zeros((h, w, 4), dtype=np.float32)
    canvas.draw()
    buf = np.asarray(canvas.buffer_rgba())
    if buf.shape[:2] != (h, w):
        raise RuntimeError(f"Agg canvas {buf.shape[:2]} != {(h, w)}")
    return buf.astype(np.float32) / 255


def build_texture(frame, cfg: dict, z: np.ndarray, land: np.ndarray, ts: np.ndarray, ts_tr,
                  zf: float | None = None, tile: int = TILE, progress: bool = True) -> np.ndarray:
    """Full RGB uint8 texture on the frame's texture grid."""
    import geopandas as gpd
    from rasterio.features import rasterize
    from rasterio.transform import from_origin
    from scipy.ndimage import gaussian_filter

    from chinamaps.dem import chunks
    from chinamaps.sources import naturalearth as ne

    pal = tint.palettes(cfg.get("palette"))
    res = frame.texture_res
    zf = zf or shade.default_zfactor(res)
    H, W = frame.texture_shape
    fields, ftr = tint.climate_field(frame, pal)
    china_geom = None
    if cfg.get("highlight_china"):
        china_geom = gpd.GeoSeries([ne.china(cfg.get("worldview", "cn"))], crs=4326).to_crs(frame.crs).iloc[0]
    layers = vector_layers(frame, cfg, z, china_geom)
    out = np.empty((H, W, 3), dtype=np.uint8)
    xmin, ymax = frame.bounds[0], frame.bounds[3]
    blocks = list(chunks((H, W), tile))
    for n, (r0, c0, h, w) in enumerate(blocks):
        ra, rb = max(0, r0 - HALO), min(H, r0 + h + HALO)
        ca, cb = max(0, c0 - HALO), min(W, c0 + w + HALO)
        zt, lt = z[ra:rb, ca:cb], land[ra:rb, ca:cb]
        tr = from_origin(xmin + ca * res, ymax - ra * res, res, res)
        shp = zt.shape
        weights = {k: tint.sample_field(v, ftr, tr, shp) for k, v in fields.items()}
        neighbour = None
        if china_geom is not None:
            cm = rasterize([(china_geom, 1)], out_shape=shp, transform=tr, fill=0, dtype="uint8")
            neighbour = (1 - gaussian_filter(cm.astype(np.float32), 1.0)) * lt
        tst = tint.sample_field(ts, ts_tr, tr, shp)
        rgb = compose_tile(zt, lt, weights, neighbour, tst, res, zf, pal)
        iy, ix = r0 - ra, c0 - ca
        rgb = rgb[iy : iy + h, ix : ix + w]
        # vectors: global pixel window, y measured up from the south edge
        vec = render_vectors(layers, c0, H - (r0 + h), w, h)
        a = vec[..., 3:4]
        rgb = rgb * (1 - a) + vec[..., :3] * a
        out[r0 : r0 + h, c0 : c0 + w] = np.round(rgb * 255).astype(np.uint8)
        if progress and (n % 8 == 0 or n == len(blocks) - 1):
            print(f"texture: tile {n + 1}/{len(blocks)}", flush=True)
    return out
