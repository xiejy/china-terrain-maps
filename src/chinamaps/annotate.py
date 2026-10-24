"""Map labels for the oblique renders.

prepare_labels(scene_dir)  config/labels.yaml features inside a scene, projected to the scene CRS
                           with DEM elevations -> scene_dir/labels_proj.json (read by Blender).
annotate(render, labels_json, scene_json, out)
                           composites the renderer's screen positions in CJK type: serif Chinese
                           names over small English names, white with a soft dark glow, letter-
                           spaced area names, blue water names (Chinese in left-slanted Song, as in
                           GB/T 20257; English in italic), a title block with a north arrow, and
                           credits with the vertical exaggeration.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import yaml

from chinamaps.net import REPO_ROOT

LABELS_YAML = REPO_ROOT / "config" / "labels.yaml"
REF_WIDTH = 1600  # font sizes below are pixels at this image width

AREA_KINDS = {"range", "plateau", "basin", "plain", "desert", "region"}
WATER_KINDS = {"river", "lake", "sea"}
POINT_KINDS = {"peak", "city", "island", "landmark"}
DROP_IF_OCCLUDED = {"peak", "city", "island", "landmark", "river", "lake"}
KIND_ORDER = ["sea", "plateau", "basin", "range", "plain", "desert", "region", "city", "landmark", "peak",
              "island", "lake", "river"]

ATTRIBUTION = "Copernicus DEM, GEBCO 2026, Natural Earth · non-commercial"
CITY_ATTRIBUTION = ("© OpenStreetMap contributors (ODbL) via Overture Maps, Shi et al. 2023 East Asia buildings (CC BY 4.0), "
                    "GlobalBuildingAtlas (CC BY-NC), GEDTM30, contains modified Copernicus Sentinel data {s2_year}"
                    " · non-commercial")

# (zh px, en px) at REF_WIDTH by style and rank
SIZES = {
    "area": {1: (25, 10.5), 2: (20, 9.5), 3: (17, 8.5)},
    "sea": {1: (25, 10.5), 2: (19, 9.5), 3: (16, 8.5)},
    "point": {1: (18, 9.5), 2: (16, 8.5), 3: (14.5, 8.0)},
    "water": {1: (18, 9.5), 2: (16, 8.5), 3: (14.5, 8.0)},
}
TRACKING = {"area": (0.36, 0.1), "sea": (0.6, 0.16), "point": (0.04, 0.0), "water": (0.12, 0.0)}
MIN_EN_PX = 8.0           # absolute floor for English text
MIN_EN_FRAC = 0.0031      # ... and relative to image width (12 px at 3840)
WHITE, WARM_WHITE = (255, 255, 255), (255, 249, 236)
RIVER_BLUE, SEA_BLUE, SEA_NAVY = (178, 222, 255), (206, 230, 255), (30, 70, 120)
DARK_GLOW, NAVY_GLOW = (12, 14, 18), (6, 24, 52)
SLANT = 0.2  # horizontal shear of water names per unit height


# --------------------------------------------------------------------------- preparation

def load_config(path: Path = LABELS_YAML) -> dict:
    return yaml.safe_load(Path(path).read_text())


def scene_settings(cfg: dict, name: str, small: bool = False) -> dict:
    """Per-scene label settings; small=True overlays the scene's `small` block (README-size copy)."""
    st = {"max_rank": 3, "scale": 1.0, "peak_elevations": True}
    st.update((cfg.get("scenes") or {}).get(name, {}) or {})
    if small:
        st.update(st.get("small") or {})
    return st


def shrink(bounds, frac: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (float(v) for v in bounds)
    dx, dy = (x1 - x0) * frac, (y1 - y0) * frac
    return x0 + dx, y0 + dy, x1 - dx, y1 - dy


def center(bounds) -> tuple[float, float]:
    x0, y0, x1, y1 = (float(v) for v in bounds)
    return (x0 + x1) / 2, (y0 + y1) / 2


def inside(x: float, y: float, bounds) -> bool:
    x0, y0, x1, y1 = bounds
    return x0 <= x <= x1 and y0 <= y <= y1


@lru_cache(maxsize=1)
def _rivers():
    from chinamaps.sources import naturalearth as ne

    return ne.read("ne_10m_rivers_lake_centerlines_scale_rank")


def river_geometry(names: list[str], crs: str, bounds_proj, bounds_lonlat=None):
    """Named Natural Earth river line(s) in the scene CRS, clipped to bounds_proj (or None)."""
    from shapely.geometry import box

    rv = _rivers()
    sel = rv[rv["name"].isin(names)]
    if bounds_lonlat is not None:
        sel = sel[sel.intersects(box(*shrink(bounds_lonlat, -0.1)))]
    if sel.empty:
        return None
    geom = sel.to_crs(crs).geometry.union_all().intersection(box(*bounds_proj))
    return None if geom.is_empty else geom


def snap_to_river(geom, xy: tuple[float, float]) -> tuple[float, float]:
    from shapely.geometry import Point
    from shapely.ops import nearest_points

    q = nearest_points(geom, Point(*xy))[0]
    return float(q.x), float(q.y)


def along_river(geom, xy: tuple[float, float], n: int = 8, spacing: float | None = None) -> list[list[float]]:
    """Alternative anchors spread along a river, nearest to xy first (excluding xy itself)."""
    from shapely.geometry import Point

    lines = [g for g in getattr(geom, "geoms", [geom]) if g.geom_type == "LineString"]
    total = sum(g.length for g in lines)
    if not lines or total == 0:
        return []
    step = spacing or total / (n + 1)
    pts = [g.interpolate(d) for g in lines for d in np.arange(step / 2, g.length, step)]
    here = Point(*xy)
    pts = sorted((p for p in pts if p.distance(here) > step / 2), key=here.distance)[:n]
    return [[round(p.x, 2), round(p.y, 2)] for p in pts]


def sample_dem(dem: np.ndarray, bounds_proj, x: float, y: float, peak: bool = False) -> float:
    """Bilinear DEM value at (x, y); peaks take the max within 2 pixels."""
    x0, y0, x1, y1 = (float(v) for v in bounds_proj)
    h, w = dem.shape
    col = (x - x0) / (x1 - x0) * w - 0.5
    row = (y1 - y) / (y1 - y0) * h - 0.5
    if peak:
        r, c = round(row), round(col)
        win = np.asarray(dem[max(0, r - 2):min(h, r + 3), max(0, c - 2):min(w, c + 3)])
        if win.size:
            return float(win.max())
    c0, r0 = int(np.clip(np.floor(col), 0, w - 2)), int(np.clip(np.floor(row), 0, h - 2))
    fc, fr = float(np.clip(col - c0, 0, 1)), float(np.clip(row - r0, 0, 1))
    q = np.asarray(dem[r0:r0 + 2, c0:c0 + 2], dtype=np.float64)
    return float((q[0, 0] * (1 - fc) + q[0, 1] * fc) * (1 - fr) + (q[1, 0] * (1 - fc) + q[1, 1] * fc) * fr)


def select_labels(meta: dict, features: list[dict], max_rank: int = 3, dem: np.ndarray | None = None,
                  snap_rivers: bool = True) -> list[dict]:
    """Features whose anchor falls inside the scene, with projected x/y and DEM z."""
    from pyproj import Transformer

    tr = Transformer.from_crs(4326, meta["crs"], always_xy=True)
    inner = shrink(meta["bounds_proj"], 0.02)
    river_box = shrink(meta["bounds_proj"], 0.06)
    out = []
    for i, f in enumerate(features):
        if int(f.get("rank", 3)) > max_rank:
            continue
        cands = [(f["lon"], f["lat"])] + [tuple(p) for p in f.get("points", [])]
        pt, alts = None, []
        if snap_rivers and f.get("ne_river"):
            geom = river_geometry(f["ne_river"], meta["crs"], river_box, meta.get("bounds"))
            if geom is not None:
                ax, ay = tr.transform(*cands[0])
                if not inside(ax, ay, inner):  # anchor is elsewhere on the river: use the mid-scene stretch
                    ax, ay = center(meta["bounds_proj"])
                pt = snap_to_river(geom, (ax, ay))
                alts = along_river(geom, pt)
        inside_pts = [(float(x), float(y)) for x, y in (tr.transform(lon, lat) for lon, lat in cands)
                      if inside(x, y, inner)]
        if pt is None and inside_pts:
            pt, alts = inside_pts[0], [list(q) for q in inside_pts[1:]]
        if pt is None:
            continue
        lon, lat = tr.transform(*pt, direction="INVERSE")
        rec = {"id": i, "zh": f["zh"], "en": f.get("en", ""), "kind": f["kind"], "rank": int(f.get("rank", 3)),
               "lon": round(lon, 5), "lat": round(lat, 5), "x": round(pt[0], 2), "y": round(pt[1], 2)}
        for k in ("elevation", "approx", "marker", "capital"):
            if k in f:
                rec[k] = f[k]
        if alts:
            rec["alts"] = alts
        if dem is not None:
            rec["z"] = round(sample_dem(dem, meta["bounds_proj"], *pt, peak=f["kind"] == "peak"), 1)
        out.append(rec)
    return out


def prepare_labels(scene_dir: Path, labels_yaml: Path = LABELS_YAML) -> Path:
    """Write scene_dir/labels_proj.json for blender/render_scene.py."""
    scene_dir = Path(scene_dir)
    meta = json.loads((scene_dir / "scene.json").read_text())
    name = meta.get("name") or scene_dir.name
    cfg = load_config(labels_yaml)
    st = scene_settings(cfg, name)
    dem_path = scene_dir / "dem.npy"
    dem = np.load(dem_path, mmap_mode="r") if dem_path.exists() else None
    labels = select_labels(meta, cfg["features"], st["max_rank"], dem)
    out = scene_dir / "labels_proj.json"
    out.write_text(json.dumps({"scene": name, "crs": meta["crs"], "settings": st, "labels": labels},
                              ensure_ascii=False, indent=1))
    print(f"labels {name}: {len(labels)} features inside the scene -> {out}", flush=True)
    return out


# --------------------------------------------------------------------------- layout

@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    def intersects(self, o: Box, pad: float = 0.0) -> bool:
        return not (self.x1 + pad <= o.x0 or o.x1 + pad <= self.x0 or self.y1 + pad <= o.y0 or o.y1 + pad <= self.y0)

    def within(self, w: float, h: float, margin: float = 0.0) -> bool:
        return self.x0 >= margin and self.y0 >= margin and self.x1 <= w - margin and self.y1 <= h - margin

    def shift(self, dx: float, dy: float) -> Box:
        return Box(self.x0 + dx, self.y0 + dy, self.x1 + dx, self.y1 + dy)


@dataclass
class Request:
    """A label to place: candidate boxes in preference order (+ an optional fixed marker box)."""
    key: int
    priority: tuple
    candidates: list[Box]
    marker: Box | None = None
    payload: dict = field(default_factory=dict)


def place_labels(requests: list[Request], width: float, height: float, blocked: list[Box] | None = None,
                 pad: float = 2.0, margin: float = 4.0, repair: bool = True) -> dict[int, int]:
    """Greedy placement by priority: each label takes its first candidate that stays inside the
    image and clears every box placed so far. Returns {key: candidate index}; unplaced keys are
    absent. A label's marker is reserved only when its text is placed.

    repair: a label that finds no free candidate may still be placed if the (at most two) labels
    in the way can move to another of their own candidates, e.g. a city name hops to the other side
    of its dot so that the basin it sits in keeps its name.
    """
    fixed = list(blocked or [])
    markers: dict[int, Box] = {}
    boxes: dict[int, Box] = {}
    chosen: dict[int, int] = {}
    by_key = {r.key: r for r in requests}

    def clear(b: Box, ignore: set[int] = frozenset(), extra: tuple[Box, ...] = (), own: int | None = None,
              gap: float = pad) -> bool:
        """b is inside the image and clear of fixed boxes, markers (except own's) and placed text
        (except the keys in ignore)."""
        if not b.within(width, height, margin):
            return False
        if any(b.intersects(o, gap) for o in (*fixed, *extra)):
            return False
        if any(b.intersects(o, gap) for k, o in markers.items() if k != own):
            return False
        return not any(b.intersects(o, gap) for k, o in boxes.items() if k not in ignore)

    def try_repair(req: Request) -> bool:
        for j, cand in enumerate(req.candidates):
            if not clear(cand, set(boxes)):  # image edge, fixed boxes and markers rule a candidate out
                continue
            blockers = [k for k, o in boxes.items() if cand.intersects(o, pad)]
            if len(blockers) > 2:
                continue
            moves: dict[int, int] = {}
            for k in blockers:
                moved = tuple(by_key[k2].candidates[i2] for k2, i2 in moves.items())
                alt = next((i for i, b in enumerate(by_key[k].candidates) if i != chosen[k]
                            and clear(b, set(blockers), (cand, *moved), own=k)), None)
                if alt is None:
                    break
                moves[k] = alt
            else:
                for k, i in moves.items():
                    chosen[k], boxes[k] = i, by_key[k].candidates[i]
                chosen[req.key], boxes[req.key] = j, cand
                return True
        return False

    for req in sorted(requests, key=lambda r: r.priority):
        if req.marker is not None and not clear(req.marker, gap=0.15 * pad):  # a dot needs less clearance
            continue
        j = next((j for j, cand in enumerate(req.candidates) if clear(cand)), None)
        if j is not None:
            chosen[req.key], boxes[req.key] = j, req.candidates[j]
        elif not (repair and try_repair(req)):
            continue
        if req.marker is not None:
            markers[req.key] = req.marker
    return chosen


def style_of(kind: str) -> str:
    if kind == "sea":
        return "sea"
    if kind in ("river", "lake"):
        return "water"
    if kind in AREA_KINDS:
        return "area"
    return "point"


def has_marker(lab: dict) -> bool:
    if "marker" in lab:
        return bool(lab["marker"])
    return lab["kind"] in POINT_KINDS


def visible(lab: dict) -> bool:
    """Drop out-of-frame labels, and fully hidden point/water labels (area names may float)."""
    if not lab.get("in_frame", True):
        return False
    if lab["kind"] not in DROP_IF_OCCLUDED:
        return True
    return lab.get("visible_frac", 0.0 if lab.get("occluded") else 1.0) > 0


def priority(lab: dict) -> tuple:
    """Marked points of rank <= 2 first: they have only four fixed boxes, while area names have many
    and can flow around the city dots and peaks. Then by rank; within a rank point features first
    (small, exact), then area and sea names, then water."""
    k = KIND_ORDER.index(lab["kind"]) if lab["kind"] in KIND_ORDER else len(KIND_ORDER)
    style = style_of(lab["kind"])
    st = {"point": 0, "area": 1, "sea": 1, "water": 2}[style]
    rank = int(lab.get("rank", 3))
    inflexible = style == "point" and rank <= 2 and has_marker(lab)
    return (0 if inflexible else 1, rank, st, k, lab.get("depth", 0.0))


def candidate_boxes(lab: dict, w: float, h: float, ax: float, ay: float, r: float, gap: float,
                    zh_h: float) -> list[Box]:
    """Text block boxes around an anchor, in preference order."""
    if has_marker(lab):
        right = Box(ax + r + gap, ay - zh_h / 2, ax + r + gap + w, ay - zh_h / 2 + h)
        left = right.shift(-(w + 2 * (r + gap)), 0)
        above = Box(ax - w / 2, ay - r - gap - h, ax + w / 2, ay - r - gap)
        below = Box(ax - w / 2, ay + r + gap, ax + w / 2, ay + r + gap + h)
        return [above, right, left, below] if lab["kind"] == "peak" else [right, left, above, below]
    c = Box(ax - w / 2, ay - h / 2, ax + w / 2, ay + h / 2)
    steps = [(0, 0), (0, -0.6), (0, 0.6), (0.3, 0), (-0.3, 0), (0.3, -0.6), (-0.3, -0.6), (0.3, 0.6),
             (-0.3, 0.6), (0, -1.2), (0, 1.2)]
    return [c.shift(dx * w, dy * h) for dx, dy in steps]


# --------------------------------------------------------------------------- drawing

@lru_cache(maxsize=64)
def font(key: str, size: int):
    from PIL import ImageFont

    from chinamaps.fonts import font_path

    return ImageFont.truetype(str(font_path(key)), max(6, size))


def tracked(fnt, tracking: float) -> bool:
    """Letter-spacing worth drawing per character (otherwise keep the font's own kerning)."""
    return tracking * fnt.size >= 1.0


def text_width(text: str, fnt, tracking: float) -> float:
    if not text:
        return 0.0
    if not tracked(fnt, tracking):
        return float(fnt.getlength(text))
    return sum(fnt.getlength(ch) for ch in text) + tracking * fnt.size * (len(text) - 1)


def line_height(fnt) -> float:
    asc, desc = fnt.getmetrics()
    return float(asc + desc)


def draw_tracked(draw, xy: tuple[float, float], text: str, fnt, tracking: float, fill=255) -> None:
    x, y = xy
    if not tracked(fnt, tracking):
        draw.text((x, y), text, font=fnt, fill=fill)
        return
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += fnt.getlength(ch) + tracking * fnt.size


@dataclass
class Block:
    """Measured two-line label: Chinese over English."""
    zh: str
    en: str
    zh_font: object
    en_font: object
    zh_track: float
    en_track: float
    italic: bool = False

    @property
    def zh_w(self) -> float:
        return text_width(self.zh, self.zh_font, self.zh_track)

    @property
    def en_w(self) -> float:
        return text_width(self.en, self.en_font, self.en_track)

    @property
    def gap(self) -> float:
        return 0.08 * self.zh_font.size

    @property
    def size(self) -> tuple[float, float]:
        h = line_height(self.zh_font) + (self.gap + line_height(self.en_font) if self.en else 0)
        return max(self.zh_w, self.en_w), h


def glow_layer(mask, radius: float, spread: int = 3):
    from PIL import ImageFilter

    g = mask.filter(ImageFilter.MaxFilter(spread)) if spread > 1 else mask
    return g.filter(ImageFilter.GaussianBlur(max(0.5, radius)))


def composite_mask(base, mask, xy: tuple[int, int], color, glow_color, glow_radius: float,
                   glow_alpha: float = 0.8) -> None:
    """Paint a text/marker mask with a soft glow onto an RGBA base image in place."""
    from PIL import Image

    pad = math.ceil(glow_radius * 3) + 2
    big = Image.new("L", (mask.width + 2 * pad, mask.height + 2 * pad), 0)
    big.paste(mask, (pad, pad))
    g = glow_layer(big, glow_radius)
    g = g.point(lambda v: int(min(255, v * glow_alpha * 1.6)))
    x, y = xy[0] - pad, xy[1] - pad
    glow = Image.new("RGBA", big.size, (*glow_color, 0))
    glow.putalpha(g)
    text = Image.new("RGBA", big.size, (*color, 0))
    text.putalpha(big)
    _composite(base, glow, x, y)
    _composite(base, text, x, y)


def _composite(base, layer, x: int, y: int) -> None:
    """alpha_composite that tolerates layers hanging off the top/left edge."""
    cx, cy = max(0, -x), max(0, -y)
    if cx >= layer.width or cy >= layer.height:
        return
    if cx or cy:
        layer = layer.crop((cx, cy, layer.width, layer.height))
    base.alpha_composite(layer, (max(0, x), max(0, y)))


def render_block_mask(block: Block, align: str = "center"):
    """L mask of a two-line label. Water names: the Chinese line is sheared to lean LEFT (左斜宋体,
    the GB/T 20257 convention for hydrographic names); the English line uses a true italic font."""
    from PIL import Image, ImageDraw

    w, h = block.size
    lh = line_height(block.zh_font)
    slant = SLANT * lh if block.italic else 0
    m = Image.new("L", (math.ceil(w + slant) + 2, math.ceil(h) + 2), 0)
    d = ImageDraw.Draw(m)

    def x_for(lw: float) -> float:  # half the slant room on each side keeps the sheared glyphs inside
        return slant / 2 + {"left": 0.0, "right": w - lw}.get(align, (w - lw) / 2)

    zh_mask = Image.new("L", m.size, 0)
    draw_tracked(ImageDraw.Draw(zh_mask), (x_for(block.zh_w), 0), block.zh, block.zh_font, block.zh_track)
    if block.italic:  # output (x, y) samples input (x - SLANT * (y - lh / 2), y): tops move left
        zh_mask = zh_mask.transform(zh_mask.size, Image.AFFINE, (1, -SLANT, SLANT * lh * 0.5, 0, 1, 0),
                                    resample=Image.BICUBIC)
    m.paste(255, (0, 0), zh_mask)
    if block.en:
        y = lh + block.gap
        draw_tracked(d, (x_for(block.en_w), y), block.en, block.en_font, block.en_track)
    return m


def marker_mask(kind: str, r: float, capital: bool = False):
    from PIL import Image, ImageDraw

    s = math.ceil(2 * r) + 3
    m = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(m)
    c = s / 2
    if kind == "peak":
        d.polygon([(c, c - r), (c + r * 0.95, c + r * 0.7), (c - r * 0.95, c + r * 0.7)], fill=255)
    elif capital:
        d.ellipse([c - r, c - r, c + r, c + r], outline=255, width=max(1, int(r * 0.3)))
        d.ellipse([c - r * 0.45, c - r * 0.45, c + r * 0.45, c + r * 0.45], fill=255)
    else:
        rr = r * (0.75 if kind in ("city", "landmark") else 0.6)
        d.ellipse([c - rr, c - rr, c + rr, c + rr], fill=255)
    return m


def elevation_text(v) -> str:
    """8848.86 -> '8848.86 m', 7556.0 -> '7556 m'."""
    return f"{float(v):.2f}".rstrip("0").rstrip(".") + " m"


def make_block(lab: dict, k: float, width: int = REF_WIDTH, elevations: bool = False) -> tuple[Block, str]:
    st = style_of(lab["kind"])
    zs, es = SIZES[st][min(3, max(1, int(lab.get("rank", 3))))]
    zt, et = TRACKING[st]
    water = st in ("water", "sea")
    en = lab.get("en", "") or ""
    if st in ("area", "sea"):
        en = en.upper()
    zh = lab.get("zh") or en
    if zh == en:
        en = ""
    if elevations and lab["kind"] == "peak" and lab.get("elevation"):
        en = f"{en} · {elevation_text(lab['elevation'])}" if en else elevation_text(lab["elevation"])
    zh_font = font("serif-bold" if st in ("area", "sea") or lab.get("rank", 3) == 1 else "serif", zs * k)
    en_font = font("latin-italic" if water else "serif", max(MIN_EN_PX, MIN_EN_FRAC * width, es * k))
    return Block(zh, en, zh_font, en_font, zt, et, italic=water), st


def colors(st: str, light_bg: bool = False) -> tuple[tuple, tuple, float]:
    """(text colour, glow colour, glow alpha) per style. Sea names over light water are dark navy
    with a white glow (pale text there reads as a smudge)."""
    if st == "sea" and light_bg:
        return SEA_NAVY, WHITE, 0.55
    return {"area": (WARM_WHITE, DARK_GLOW, 0.75), "point": (WHITE, DARK_GLOW, 0.8),
            "water": (RIVER_BLUE, NAVY_GLOW, 0.85), "sea": (SEA_BLUE, NAVY_GLOW, 0.8)}[st]


def is_light(base, box, threshold: float = 140) -> bool:
    """True when nearly all of the region is light (dark text then reads; else use white + glow)."""
    region = np.asarray(base.convert("L").crop(tuple(int(v) for v in box)), np.float32)
    return region.size == 0 or float(np.percentile(region, 5)) > threshold


def is_dark(base, box) -> bool:
    """True when nearly all of the region is dark (white text with a glow reads)."""
    region = np.asarray(base.convert("L").crop(tuple(int(v) for v in box)), np.float32)
    return region.size == 0 or float(np.percentile(region, 95)) < 150


def draw_title(base, title: dict, k: float) -> Box:
    """Title block (zh over en) in the top-left; dark on light backgrounds, else white with glow."""
    from PIL import Image, ImageDraw

    w = base.width
    m = 0.032 * w
    zh_font = font("serif-black", 0.034 * w)
    en_font = font("serif", 0.0118 * w)
    zh, en = title.get("zh", ""), title.get("en", "")
    zw, ew = text_width(zh, zh_font, 0.08), text_width(en, en_font, 0.0)
    zh_h = line_height(zh_font)
    rule_y = zh_h + 0.25 * en_font.size
    bw, bh = max(zw, ew), rule_y + 0.6 * en_font.size + line_height(en_font)
    light_bg = is_light(base, (m, m, m + bw, m + bh))
    mask = Image.new("L", (int(bw) + 2, int(bh) + 2), 0)
    d = ImageDraw.Draw(mask)
    draw_tracked(d, (0, 0), zh, zh_font, 0.08)
    d.line([(1, rule_y), (max(zw, ew) * 0.35, rule_y)], fill=200, width=max(1, int(0.0012 * w)))
    draw_tracked(d, (0, rule_y + 0.6 * en_font.size), en, en_font, 0.0)
    if light_bg:
        composite_mask(base, mask, (int(m), int(m)), (30, 38, 46), (255, 255, 255), 0.004 * w, 0.35)
    else:
        composite_mask(base, mask, (int(m), int(m)), WHITE, DARK_GLOW, 0.004 * w, 0.8)
    return Box(m, m, m + bw, m + bh)


def draw_attribution(base, text: str) -> Box:
    from PIL import Image, ImageDraw

    w, h = base.size
    fnt = font("sans", max(9, 0.0068 * w))
    tw, th = text_width(text, fnt, 0.02), line_height(fnt)
    x, y = w - 0.018 * w - tw, h - 0.014 * w - th
    light_bg = is_light(base, (x, y, x + tw, y + th))
    mixed = not light_bg and not is_dark(base, (x, y, x + tw, y + th))
    if mixed:  # e.g. half over the slab edge, half over the pale sky: a soft dark plate keeps it legible
        pad = 0.45 * th
        plate = Image.new("L", base.size, 0)
        ImageDraw.Draw(plate).rounded_rectangle((x - pad, y - 0.3 * pad, x + tw + pad, y + th + 0.3 * pad),
                                                radius=pad, fill=110)
        shade = Image.new("RGBA", base.size, (*DARK_GLOW, 0))
        shade.putalpha(plate)
        base.alpha_composite(shade)
    mask = Image.new("L", (int(tw) + 2, int(th) + 2), 0)
    draw_tracked(ImageDraw.Draw(mask), (0, 0), text, fnt, 0.02)
    color, glow = ((70, 74, 78), (255, 255, 255)) if light_bg else ((235, 235, 235), DARK_GLOW)
    composite_mask(base, mask, (int(x), int(y)), color, glow, 0.002 * w, 0.4)
    return Box(x, y, x + tw, y + th)


def north_angle(camera: dict) -> float | None:
    """Screen angle of north (degrees clockwise from image-up) for a camera at compass `azimuth`
    (bearing from the target to the camera) looking down at `elevation`; None if unknown."""
    if not camera or "azimuth" not in camera:
        return None
    a, e = math.radians(float(camera["azimuth"])), math.radians(float(camera.get("elevation", 90)))
    f = -np.array([math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), math.sin(e)])  # view direction
    r = np.cross(f, [0.0, 0.0, 1.0])
    r = r / np.linalg.norm(r) if np.linalg.norm(r) > 1e-9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(r, f)
    north = np.array([0.0, 1.0, 0.0])
    return math.degrees(math.atan2(float(north @ r), float(north @ u)))


def draw_compass(base, angle: float, xy: tuple[float, float], size: float, light_bg: bool) -> Box:
    """North needle (filled half toward north) with a 北/N mark, centred at xy, rotated by angle."""
    from PIL import Image, ImageDraw

    s = math.ceil(size * 4.2)
    m = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(m)
    c, r, wd = s / 2, size, size * 0.32
    d.polygon([(c, c - r), (c + wd, c), (c - wd, c)], fill=255)                # north half, solid
    d.polygon([(c, c + r), (c + wd, c), (c - wd, c)], outline=255, width=max(1, int(size * 0.08)))
    d.ellipse([c - size * 0.09, c - size * 0.09, c + size * 0.09, c + size * 0.09], fill=0)
    m = m.rotate(-angle, resample=Image.BICUBIC, center=(c, c))
    t = math.radians(angle)
    tx, ty = c + math.sin(t) * r * 1.55, c - math.cos(t) * r * 1.55  # 北 beyond the needle tip
    fnt = font("serif-bold", size * 0.62)
    tw = fnt.getlength("北")
    ImageDraw.Draw(m).text((tx - tw / 2, ty - line_height(fnt) / 2), "北", font=fnt, fill=255)
    x, y = round(xy[0] - c), round(xy[1] - c)
    if light_bg:
        composite_mask(base, m, (x, y), (30, 38, 46), WHITE, size * 0.12, 0.35)
    else:
        composite_mask(base, m, (x, y), WHITE, DARK_GLOW, size * 0.12, 0.8)
    return Box(x, y, x + s, y + s)


def label_request(i: int, lab: dict, k: float, width: int, elevations: bool = False) -> Request:
    """Candidate boxes for one label: around its anchor, then around alternate anchors (unmarked
    labels slide along rivers/ranges), then Chinese-only compact variants for area/water names."""
    block, st_name = make_block(lab, k, width, elevations)
    r = 0.32 * block.zh_font.size if has_marker(lab) else 0.0
    gap = 0.25 * block.zh_font.size
    ax, ay = float(lab["px"]), float(lab["py"])
    anchors = [(ax, ay)]
    if not r:
        anchors += [(a["px"], a["py"]) for a in lab.get("alts", [])
                    if a.get("in_frame", True) and a.get("visible_frac", 1.0) > 0]
    variants = [block] + ([Block(block.zh, "", block.zh_font, block.en_font, block.zh_track, 0.0, block.italic)]
                          if not r and block.en else [])
    boxes, blocks = [], []
    for v in variants:
        bw, bh = v.size
        for j, (x, y) in enumerate(anchors):
            cands = candidate_boxes(lab, bw, bh, x, y, r, gap, line_height(v.zh_font))
            cands = cands if j == 0 else cands[:3]
            boxes += cands
            blocks += [v] * len(cands)
    marker = Box(ax - r, ay - r, ax + r, ay + r) if r else None
    return Request(i, priority(lab), boxes, marker, {"label": lab, "r": r, "style": st_name, "blocks": blocks})


def credits(info: dict, meta: dict) -> str:
    """Attribution line: data credits, plus the vertical exaggeration for terrain scenes."""
    if "epsg" in meta or info.get("kind") == "city":
        year = ((meta.get("sources") or {}).get("sentinel2") or {}).get("year", "")
        return CITY_ATTRIBUTION.format(s2_year=year).replace("data  ·", "data ·")
    ex = info.get("exaggeration", meta.get("exaggeration"))
    return ATTRIBUTION + (f" · 垂直夸张 ×{float(ex):g} Vertical exaggeration ×{float(ex):g}" if ex else "")


def annotate(render_png: Path, labels_json: Path, scene_json: Path | None, out_png: Path,
             attribution: str | None = None, labels_yaml: Path = LABELS_YAML, width: int | None = None,
             small: bool = False) -> Path:
    """Composite labels, title, north arrow and credits over a render. Returns out_png.

    width: resample the render to this width first (label positions scale with it); small: use the
    scene's `small` label settings (fewer, larger labels for a README-size copy)."""
    from PIL import Image

    base = Image.open(render_png).convert("RGBA")
    src_w = base.width
    if width and width != base.width:
        base = base.resize((width, round(base.height * width / base.width)), Image.LANCZOS)
    f = base.width / src_w
    info = json.loads(Path(labels_json).read_text())
    meta = json.loads(Path(scene_json).read_text()) if scene_json and Path(scene_json).exists() else {}
    name = meta.get("name") or info.get("scene", "")
    st = scene_settings(load_config(labels_yaml), name, small) if Path(labels_yaml).exists() else {"scale": 1.0}
    w, h = base.size
    k = w / REF_WIDTH * float(st.get("scale", 1.0))
    if attribution is None:
        attribution = credits(info, meta)

    title = info.get("title") or meta.get("title") or {}  # the renderer's copy reflects the live config
    tbox = draw_title(base, title, k)
    blocked = [tbox, draw_attribution(base, attribution)]
    angle = north_angle(info.get("camera") or meta.get("camera") or {})
    if angle is not None:
        size = 0.013 * w
        cxy = (tbox.x0 + size * 1.3, tbox.y1 + size * 2.2)
        blocked.append(draw_compass(base, angle, cxy, size, is_light(base, (cxy[0] - size, cxy[1] - size,
                                                                         cxy[0] + size, cxy[1] + size))))
    requests = []
    max_rank = int(st.get("max_rank", 3))
    for i, lab in enumerate(info.get("labels", [])):
        if visible(lab) and int(lab.get("rank", 3)) <= max_rank:
            lab = {**lab, "px": lab["px"] * f, "py": lab["py"] * f,
                   "alts": [{**a, "px": a["px"] * f, "py": a["py"] * f} for a in lab.get("alts", [])]}
            requests.append(label_request(i, lab, k, w, bool(st.get("peak_elevations", True))))
    chosen = place_labels(requests, w, h, blocked, pad=0.45 * 16 * k, margin=0.01 * w)

    for req in sorted(requests, key=lambda q: q.priority, reverse=True):  # most important drawn last
        if req.key not in chosen:
            continue
        lab, r, st_name = req.payload["label"], req.payload["r"], req.payload["style"]
        box, block = req.candidates[chosen[req.key]], req.payload["blocks"][chosen[req.key]]
        color, glow, ga = colors(st_name, st_name == "sea" and is_light(base, (box.x0, box.y0, box.x1, box.y1), 150))
        if r:
            mm = marker_mask(lab["kind"], r, bool(lab.get("capital")))
            composite_mask(base, mm, (round(lab["px"] - mm.width / 2), round(lab["py"] - mm.height / 2)),
                           WHITE, DARK_GLOW, 0.22 * r + 1, 0.9)
        align = "left" if box.x0 > lab["px"] else "right" if box.x1 < lab["px"] else "center"
        mask = render_block_mask(block, align)
        composite_mask(base, mask, (round(box.x0), round(box.y0)), color, glow,
                       0.11 * block.zh_font.size, ga)
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(out_png, compress_level=4)
    placed = sorted(req.payload["label"]["zh"] for req in requests if req.key in chosen)
    dropped = sorted(req.payload["label"]["zh"] for req in requests if req.key not in chosen)
    print(f"annotate: {len(chosen)}/{len(requests)} visible labels placed -> {out_png}"
          + (f"; dropped {dropped}" if dropped else ""), flush=True)
    out_png.with_suffix(".placed.json").write_text(json.dumps({"placed": placed, "dropped": dropped},
                                                              ensure_ascii=False))
    return out_png


def _save_jpeg(im, dest: Path, quality: int, max_bytes: int | None) -> None:
    """Save as JPEG, stepping quality down (to 70 at most) until the file fits max_bytes."""
    while True:
        im.save(dest, quality=quality, optimize=True, progressive=True)
        if max_bytes is None or dest.stat().st_size <= max_bytes or quality <= 70:
            return
        quality -= 4


def _fit_width(im, width: int):
    from PIL import Image

    if im.width <= width:
        return im
    return im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)


def gallery_jpeg(src: Path, dest: Path, max_width: int = 3840, quality: int = 90, small_width: int | None = 1280,
                 max_bytes: int | None = 4_000_000) -> Path:
    """JPEG copy for the committed gallery/ (<= max_width px, <= max_bytes), plus dest_small.jpg at
    small_width px for the README."""
    from PIL import Image

    im = Image.open(src).convert("RGB")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _save_jpeg(_fit_width(im, max_width), dest, quality, max_bytes)
    if small_width:
        _save_jpeg(_fit_width(im, small_width), dest.with_name(f"{dest.stem}_small.jpg"), 88, None)
    return dest
