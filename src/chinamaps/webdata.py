"""Web map assets (`chinamaps webdata`), written into web/public/ and web/src/generated/.

build_webdata()          every step below whose inputs exist; logs what was skipped. Rerunnable.
build_china_tiles()      Natural Earth (cn worldview) + config/labels.yaml -> data/china.pmtiles
build_city_tiles(name)   out/cities/<name>/buildings.geoparquet -> cities/<name>_buildings.pmtiles
copy_city(name)          city_web.glb / city.json -> cities/<name>.glb / .json
write_scenes()           config/scenes.yaml camera viewpoints -> scenes.json
fetch_glyphs(text)       Protomaps glyph PBFs for the non-CJK characters in the labels
write_color_relief()     palettes.yaml web_color_relief + render tints -> src/generated/color_relief.json
write_cities_index()     cities/index.json: configured cities and which of their web assets exist
build_gallery()          gallery/<stem>_small.jpg (or a downsized gallery/<stem>.jpg) -> gallery/ + index.json
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import quote

import geopandas as gpd
import requests
import yaml
from shapely.geometry import Point, box, mapping
from shapely.ops import linemerge, nearest_points, unary_union

from chinamaps.net import DATA_DIR, OUT_DIR, REPO_ROOT, download
from chinamaps.sources import naturalearth as ne

WEB = REPO_ROOT / "web"
PUBLIC = WEB / "public"
GENERATED = WEB / "src" / "generated"
CONFIG = REPO_ROOT / "config"
GALLERY = REPO_ROOT / "gallery"
REPO_URL = "https://github.com/xiejy/china-terrain-maps"
GALLERY_WIDTH = 1600  # px, web copy when the render step left no <stem>_small.jpg
GALLERY_LARGE = 2560  # px, lightbox copy (labels in the 1280 px thumbnails are too small to read)

BBOX = (60.0, 0.0, 150.0, 60.0)
CN_CODES = ("CHN", "TWN", "HKG", "MAC")
TIPPECANOE_THREADS = "2"
BUILDING_CREDITS = ("© OpenStreetMap contributors (ODbL) via Overture Maps; Shi et al. 2023 (CC BY 4.0); "
                    "GlobalBuildingAtlas (CC BY-NC 4.0)")

GLYPH_BASE = "https://raw.githubusercontent.com/protomaps/basemaps-assets/main/fonts"
FONTSTACKS = ("Noto Sans Regular", "Noto Sans Medium", "Noto Sans Italic")

# Codepoints MapLibre draws with localIdeographFontFamily (src/util/unicode_properties.g.ts,
# codePointUsesLocalIdeographFontFamily); no glyph PBF is requested for them.
LOCAL_GLYPH_RANGES = (
    (0x02EA, 0x02EB), (0x1100, 0x11FF), (0x2E80, 0x2FDF), (0x3000, 0x30FF), (0x3105, 0x312F),
    (0x3131, 0x318E), (0x31A0, 0x4DBF), (0x4E00, 0xA48C), (0xA490, 0xA4C6), (0xA960, 0xA97C),
    (0xAC00, 0xD7FB), (0xF900, 0xFAD9), (0xFE10, 0xFE1F), (0xFE30, 0xFE4F), (0xFF00, 0xFFEF),
    (0x16FE0, 0x3134F),
)

LABEL_MINZOOM = {1: 2, 2: 4, 3: 6}  # labels.yaml rank -> first zoom shown
EXTRA_POINT_MINZOOM = 5  # labels.yaml `points` (extra anchors along ranges and rivers) come in at this zoom
# Municipalities: the city label already names them, so their province label is dropped.
MUNICIPALITIES = ("北京", "天津", "上海", "重庆")
# Area and sea names are letter-spaced with EN SPACE (in the Noto Sans glyph PBFs): MapLibre's
# text-letter-spacing would also stretch the English line.
SPACED_KINDS = {"range", "plateau", "basin", "plain", "desert", "region", "sea"}

# Province names on the map, without the administrative suffix.
PROVINCE_SUFFIXES = ("维吾尔自治区", "壮族自治区", "回族自治区", "特别行政区", "自治区", "省", "市")
MERGED_PROVINCES = {
    "TWN": ("台湾省", "Taiwan"),
    "HKG": ("香港特别行政区", "Hong Kong"),
    "MAC": ("澳门特别行政区", "Macao"),
}


def log(msg: str) -> None:
    print(f"webdata: {msg}", flush=True)


def _rel(p: Path) -> str:
    return str(p.relative_to(REPO_ROOT)) if p.is_relative_to(REPO_ROOT) else str(p)


# ---------------------------------------------------------------- scenes.json

def _mercator_y(lat: float) -> float:
    """Web Mercator y in [0, 1] (0 = north edge)."""
    s = math.sin(math.radians(max(-85.0511, min(85.0511, lat))))
    return 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


def _lat_from_mercator_y(y: float) -> float:
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y))))


def fit_zoom(bounds: Iterable[float], viewport: tuple[int, int] = (1280, 800), tile: int = 512) -> float:
    """MapLibre zoom that fits lon/lat bounds into a viewport (pixels), north up, no pitch."""
    w, s, e, n = bounds
    dx = (e - w) / 360.0
    dy = _mercator_y(s) - _mercator_y(n)
    return math.log2(min(viewport[0] / (tile * dx), viewport[1] / (tile * dy)))


def scene_viewpoint(cfg: dict, viewport: tuple[int, int] = (1280, 800)) -> dict:
    """MapLibre camera for an oblique scene, looking the same way as the Blender camera.

    camera.azimuth is the bearing from the scene centre to the camera, so the view direction
    (MapLibre bearing) is the opposite one. camera.elevation is the look-down angle from the
    horizontal, so MapLibre pitch (from vertical) is its complement.
    """
    w, s, e, n = cfg["bounds"]
    cam = cfg.get("camera", {})
    y = (_mercator_y(s) + _mercator_y(n)) / 2
    zoom = fit_zoom(cfg["bounds"], viewport) - math.log2(max(0.25, float(cam.get("distance", 1.0))))
    return {
        "center": [round((w + e) / 2, 5), round(_lat_from_mercator_y(y), 5)],
        "zoom": round(zoom, 2),
        "bearing": round((float(cam.get("azimuth", 180)) + 180.0) % 360.0, 1),
        "pitch": round(min(85.0, max(0.0, 90.0 - float(cam.get("elevation", 90)))), 1),
    }


def scenes_json(scenes_yaml: Path = CONFIG / "scenes.yaml") -> list[dict]:
    doc = yaml.safe_load(scenes_yaml.read_text())
    defaults = doc.get("defaults", {})
    out = []
    for name, sc in doc["scenes"].items():
        cfg = {**defaults, **sc}
        out.append({
            "name": name,
            "title": cfg.get("title", {"zh": name, "en": name}),
            "bounds": cfg["bounds"],
            **scene_viewpoint(cfg),
            # Blender exaggeration is relative to the scene scale; the web slider is capped at 5x.
            "exaggeration": round(min(5.0, max(1.0, float(cfg.get("exaggeration", 1.5)))), 1),
        })
    return out


def write_scenes(dest: Path = PUBLIC / "scenes.json") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(scenes_json(), ensure_ascii=False, indent=1))
    log(f"wrote {_rel(dest)}")
    return dest


# ---------------------------------------------------------------- colour relief

def _num(z: float) -> int | float:
    return int(z) if float(z).is_integer() else float(z)


def color_relief_stops(palettes_yaml: Path = CONFIG / "palettes.yaml") -> list[list]:
    """palettes.yaml web_color_relief as sorted [elevation_m, '#rrggbb'] stops."""
    stops = yaml.safe_load(palettes_yaml.read_text())["web_color_relief"]
    stops = sorted(([float(z), str(c)] for z, c in stops), key=lambda s: s[0])
    return [[_num(z), c] for z, c in stops]


def render_relief_stops(palettes_yaml: Path = CONFIG / "palettes.yaml", lowland: str = "warm_humid") -> list[list]:
    """The Blender hypsometric tints (land.shared_high + one lowland ramp, plus the ocean ramp)
    as web stops, so the web map can match the oblique renders. Sea level 0 counts as land."""
    pal = yaml.safe_load(palettes_yaml.read_text())

    def hexc(r: float, g: float, b: float) -> str:
        return f"#{int(r):02x}{int(g):02x}{int(b):02x}"

    land = {float(z): hexc(r, g, b) for z, r, g, b in pal["land"]["shared_high"] + pal["land"][lowland] if z >= 0}
    sea = {min(float(z), -1.0): hexc(r, g, b) for z, r, g, b in pal["ocean"]}
    stops = sorted({**sea, **land}.items())
    return [[_num(z), c] for z, c in stops]


def write_color_relief(dest: Path = GENERATED / "color_relief.json") -> Path:
    """{"web": web_color_relief, "render": Blender tints} for the web palette switch."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"web": color_relief_stops(), "render": render_relief_stops()}) + "\n")
    log(f"wrote {_rel(dest)}")
    return dest


# ---------------------------------------------------------------- GeoJSON layers

def _clean(v):
    """JSON-safe scalar (NaN -> None, numpy -> python)."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "item"):
        v = v.item()
        if isinstance(v, float) and math.isnan(v):
            return None
    return v


def _feature(geom, props: dict, minzoom: int | None = None) -> dict:
    f = {"type": "Feature", "properties": {k: _clean(v) for k, v in props.items() if _clean(v) is not None},
         "geometry": mapping(geom)}
    if minzoom is not None:
        f["tippecanoe"] = {"minzoom": int(max(0, minzoom))}
    return f


def _minzoom(v, default: int, offset: float = 0.0) -> int:
    v = _clean(v)
    return default if v is None else math.floor(max(0.0, float(v) + offset))


def boundary_features() -> list[dict]:
    gdf = ne.boundary_lines("cn", BBOX)
    return [_feature(r.geometry, {"china": bool(r.china), "a": r.a, "b": r.b}, 0) for r in gdf.itertuples()]


def scs_dash_features() -> list[dict]:
    return [_feature(g, {"kind": "scs"}, 0) for g in ne.scs_dashes().geometry]


def china_provinces() -> gpd.GeoDataFrame:
    """Provincial-level divisions (cn worldview): Taiwan, Hong Kong and Macao each dissolved to one."""
    gdf = ne.provinces("cn")
    rows = []
    for r in gdf[gdf["adm0_a3"] == "CHN"].itertuples():
        rows.append({"zh": r.name_zh, "en": r.name_en if isinstance(r.name_en, str) else r.name,
                     "lon": r.longitude, "lat": r.latitude, "geometry": r.geometry})
    for code, (zh, en) in MERGED_PROVINCES.items():
        part = gdf[gdf["adm0_a3"] == code]
        if len(part):
            geom = unary_union(part.geometry.values)
            p = geom.representative_point() if code != "TWN" else Point(120.95, 23.75)
            rows.append({"zh": zh, "en": en, "lon": p.x, "lat": p.y, "geometry": geom})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)


def province_line_features(provs: gpd.GeoDataFrame | None = None, eps: float = 1e-3) -> list[dict]:
    """Edges shared by two provinces (not coasts, not the national border)."""
    provs = china_provinces() if provs is None else provs
    geoms = list(provs.geometry.values)
    lines = []
    for i, g in enumerate(geoms):
        for h in geoms[i + 1:]:
            if not g.envelope.buffer(eps).intersects(h.envelope) or not g.buffer(eps).intersects(h):
                continue
            edge = ne.linemerge_any(g.boundary.intersection(h.buffer(eps)))
            if edge is not None and not edge.is_empty and edge.length > 0:
                lines.append(edge)
    if not lines:
        return []
    merged = unary_union(lines)
    merged = linemerge(merged) if merged.geom_type == "MultiLineString" else merged
    parts = getattr(merged, "geoms", [merged])
    return [_feature(p, {"kind": "province"}, 3) for p in parts if p.length > 0]


def province_label_features(provs: gpd.GeoDataFrame | None = None) -> list[dict]:
    provs = china_provinces() if provs is None else provs
    out = []
    for r in provs.itertuples():
        zh = str(r.zh)
        short = next((zh[: -len(s)] for s in PROVINCE_SUFFIXES if zh.endswith(s) and len(zh) > len(s) + 1), zh)
        if short in MUNICIPALITIES:
            continue
        out.append(_feature(Point(r.lon, r.lat), {"zh": short, "zh_full": zh, "en": r.en}, 4))
    return out


def spaced(text: str, sep: str = "\u2002") -> str:
    return sep.join(text)


def _clip(gdf: gpd.GeoDataFrame, bbox=BBOX) -> gpd.GeoDataFrame:
    gdf = gdf[gdf.intersects(box(*bbox))]
    return gdf.clip(box(*bbox))


def river_features() -> list[dict]:
    gdf = _clip(ne.read("ne_10m_rivers_lake_centerlines_scale_rank"))
    out = []
    for r in gdf.itertuples():
        props = {"zh": r.name_zh, "en": r.name_en if isinstance(r.name_en, str) else r.name,
                 "scalerank": int(r.scalerank), "kind": "lake" if "Lake" in str(r.featurecla) else "river"}
        out.append(_feature(r.geometry, props, _minzoom(r.min_zoom, 6, -1.0)))
    return out


def lake_features() -> list[dict]:
    gdf = _clip(ne.read("ne_10m_lakes"))
    out = []
    for r in gdf.itertuples():
        props = {"zh": r.name_zh, "en": r.name_en if isinstance(r.name_en, str) else r.name,
                 "scalerank": int(r.scalerank)}
        out.append(_feature(r.geometry, props, _minzoom(r.min_zoom, 5, -1.0)))
    return out


def glacier_features() -> list[dict]:
    gdf = _clip(ne.read("ne_10m_glaciated_areas"))
    return [_feature(r.geometry, {"scalerank": int(r.scalerank)}, _minzoom(r.min_zoom, 4, -1.0))
            for r in gdf.itertuples()]


def place_capital(adm0: str, adm0cap, featurecla: str) -> str:
    """'country', 'province' or '' in the cn worldview.

    Taipei, Hong Kong and Macao are provincial-level seats, and Taiwan's county seats (Natural
    Earth admin-1 capitals there) are ordinary places.
    """
    if adm0 in ("TWN", "HKG", "MAC"):
        return "province" if adm0cap == 1 or "region capital" in featurecla else ""
    if adm0cap == 1:
        return "country"
    return "province" if "Admin-1 capital" in featurecla or "region capital" in featurecla else ""


def place_features() -> list[dict]:
    """Populated places in and near China that have a Chinese name.

    China membership follows the cn-worldview polygon, not Natural Earth's de facto ADM0: a place
    inside it (e.g. Itanagar in Zangnan) is Chinese, and a foreign admin-1 seat there is not styled
    as a capital.
    """
    import shapely

    gdf = _clip(ne.read("ne_10m_populated_places"))
    gdf = gdf[gdf["NAME_ZH"].notna()]
    cn_poly = ne.china("cn")
    shapely.prepare(cn_poly)
    inside = shapely.contains_xy(cn_poly, gdf["LONGITUDE"].to_numpy(float), gdf["LATITUDE"].to_numpy(float))
    out = []
    for r, in_cn in zip(gdf.itertuples(), inside):
        china = r.ADM0_A3 in CN_CODES or bool(in_cn)
        capital = place_capital(r.ADM0_A3, r.ADM0CAP, str(r.FEATURECLA))
        if china and r.ADM0_A3 not in CN_CODES:
            capital = ""
        props = {"zh": r.NAME_ZH, "en": r.NAME_EN if isinstance(r.NAME_EN, str) else r.NAME,
                 "rank": int(r.SCALERANK), "labelrank": _minzoom(r.LABELRANK, 10), "capital": capital,
                 "china": china, "pop": _minzoom(r.POP_MAX, 0)}
        # Chinese places come in one zoom earlier than neighbours, provincial capitals by z4.
        mz = _minzoom(r.MIN_ZOOM, 6, -1.0 if china else 0.0)
        if china and capital:
            mz = min(mz, 4)
        out.append(_feature(Point(r.LONGITUDE, r.LATITUDE), props, mz))
    return out


def _river_lines_by_name() -> dict[str, list]:
    gdf = ne.read("ne_10m_rivers_lake_centerlines_scale_rank")
    lines: dict[str, list] = {}
    for col in ("name", "name_en"):
        for name, geom in zip(gdf[col], gdf.geometry):
            if isinstance(name, str):
                lines.setdefault(name, []).append(geom)
    return lines


def label_features(labels_yaml: Path = CONFIG / "labels.yaml", places: list[dict] | None = None) -> list[dict]:
    """Physical-feature labels from config/labels.yaml (layer 'label').

    City entries that duplicate a Natural Earth place (same Chinese name nearby) are left to the
    'place' layer. River anchors snap onto their Natural Earth line when ne_river is given.
    """
    if not labels_yaml.exists():
        log(f"skip label layer: {_rel(labels_yaml)} not found")
        return []
    feats = yaml.safe_load(labels_yaml.read_text()).get("features", [])
    place_xy = {}
    for f in places or []:
        place_xy.setdefault(f["properties"].get("zh"), []).append(f["geometry"]["coordinates"])
    rivers = _river_lines_by_name() if any(f.get("ne_river") for f in feats) else {}
    out = []
    for f in feats:
        lon, lat = float(f["lon"]), float(f["lat"])
        kind = f.get("kind", "region")
        if kind == "city" and any(abs(x - lon) < 0.5 and abs(y - lat) < 0.5 for x, y in place_xy.get(f["zh"], [])):
            continue
        pt = Point(lon, lat)
        cands = [g for n in f.get("ne_river") or [] for g in rivers.get(n, [])]
        if cands:
            line = unary_union(cands)
            snapped = nearest_points(line, pt)[0]
            if snapped.distance(pt) < 1.5:
                pt = snapped
        rank = int(f.get("rank", 3))
        props = {"zh": f["zh"], "en": f.get("en", ""), "kind": kind, "rank": rank,
                 "elevation": f.get("elevation"), "marker": bool(f.get("marker", kind in ("peak", "city", "island")))}
        if kind in SPACED_KINDS:
            props["zh_sp"] = spaced(f["zh"])
        # marked island names from z2: the 南海诸岛 inset shows the South China Sea at about z3.
        # labels.yaml web_minzoom overrides (e.g. 台湾岛, whose name would cover 台北 at the national view).
        minzoom = 2 if kind == "island" and props["marker"] else LABEL_MINZOOM.get(rank, 6)
        minzoom = int(f.get("web_minzoom", minzoom))
        out.append(_feature(pt, props, minzoom))
        # extra anchors: rivers are labelled again along their course (ranges are not: on the web their
        # names would repeat across one view)
        for lon2, lat2 in (f.get("points") or []) if kind == "river" else []:
            out.append(_feature(Point(float(lon2), float(lat2)), {**props, "extra": True},
                                max(minzoom, EXTRA_POINT_MINZOOM)))
    return out


def china_layers() -> dict[str, list[dict]]:
    """All layers of data/china.pmtiles as GeoJSON feature lists."""
    provs = china_provinces()
    places = place_features()
    return {
        "boundary": boundary_features(),
        "scs_dash": scs_dash_features(),
        "province": province_line_features(provs),
        "province_label": province_label_features(provs),
        "river": river_features(),
        "lake": lake_features(),
        "glacier": glacier_features(),
        "place": places,
        "label": label_features(places=places),
    }


# ---------------------------------------------------------------- tippecanoe

def tippecanoe_bin(name: str = "tippecanoe") -> Path:
    for d in (Path(sys.executable).parent, REPO_ROOT / ".venv" / "bin"):
        if (d / name).exists():
            return d / name
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"{name} not found (pip wheel tippecanoe==2.72.0)")
    return Path(found)


def _write_geojsonseq(features: Iterable[dict], path: Path) -> int:
    n = 0
    with open(path, "w") as fh:
        for f in features:
            fh.write(json.dumps(f, ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1
    return n


def run_tippecanoe(layers: dict[str, Iterable[dict]], dest: Path, args: list[str]) -> Path:
    """Write each layer to GeoJSONSeq in a temp dir and tile them into one .pmtiles file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "web").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=OUT_DIR / "web") as tmp:
        cmd = [str(tippecanoe_bin()), "-o", str(dest), "--force", "--quiet", *args]
        for name, feats in layers.items():
            p = Path(tmp) / f"{name}.geojsons"
            if _write_geojsonseq(feats, p):
                cmd += ["-L", f"{name}:{p}"]
        env = {**os.environ, "TIPPECANOE_MAX_THREADS": TIPPECANOE_THREADS}
        subprocess.run(cmd, check=True, env=env, cwd=tmp)
    return dest


def build_china_tiles(dest: Path = PUBLIC / "data" / "china.pmtiles", layers: dict | None = None) -> Path:
    layers = china_layers() if layers is None else layers
    counts = {k: len(v) for k, v in layers.items()}
    run_tippecanoe(layers, dest, [
        "-Z0", "-z10", "-r1", "--no-feature-limit", "--drop-densest-as-needed",
        "--simplification=4", "--detect-shared-borders", "--buffer=8",
        "-n", "China overlays (cn worldview)", "-A", "Natural Earth",
    ])
    log(f"wrote {_rel(dest)} ({dest.stat().st_size / 1e6:.1f} MB) {counts}")
    return dest


# ---------------------------------------------------------------- glyphs

def _is_local_glyph(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in LOCAL_GLYPH_RANGES)


def glyph_ranges(texts: Iterable[str]) -> list[int]:
    """Start codepoints of the 256-wide PBF ranges needed for the non-CJK characters."""
    cps = set(range(0x20, 0x7F))
    for t in texts:
        cps.update(ord(c) for c in t)
    return sorted({cp // 256 * 256 for cp in cps if not _is_local_glyph(cp) and cp >= 0x20})


def layer_texts(layers: dict[str, list[dict]]) -> Iterator[str]:
    for feats in layers.values():
        for f in feats:
            for k in ("zh", "zh_sp", "zh_full", "en"):
                v = f["properties"].get(k)
                if isinstance(v, str):
                    yield v


def fetch_glyphs(texts: Iterable[str], dest: Path = PUBLIC / "fonts") -> list[Path]:
    out = []
    ranges = glyph_ranges(texts)
    for stack in FONTSTACKS:
        for lo in ranges:
            name = f"{lo}-{lo + 255}.pbf"
            target = dest / stack / name
            try:
                src = download(f"{GLYPH_BASE}/{quote(stack)}/{name}", DATA_DIR / "glyphs" / stack / name)
            except requests.HTTPError as e:
                log(f"skip glyphs {stack}/{name}: {e}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            out.append(target)
    log(f"glyphs: {len(out)} PBFs for {list(FONTSTACKS)} ranges {ranges}")
    return out


# ---------------------------------------------------------------- cities

def city_names(cities_yaml: Path = CONFIG / "cities.yaml") -> dict[str, dict]:
    return yaml.safe_load(cities_yaml.read_text()).get("cities", {})


def building_features(parquet: Path) -> Iterator[dict]:
    gdf = gpd.read_parquet(parquet, columns=["height", "base_z", "height_source", "geometry"])
    gdf = gdf.to_crs(4326) if gdf.crs and gdf.crs.to_epsg() != 4326 else gdf
    for r in gdf.itertuples():
        if r.geometry is None or r.geometry.is_empty:
            continue
        props = {"height": round(float(r.height), 1), "base_z": round(float(r.base_z), 1),
                 "height_source": r.height_source}
        yield _feature(r.geometry, props)


def build_city_tiles(name: str, out_dir: Path = OUT_DIR / "cities", dest_dir: Path = PUBLIC / "cities") -> Path | None:
    src = out_dir / name / "buildings.geoparquet"
    if not src.exists():
        log(f"skip {name} buildings: {src} not built yet")
        return None
    dest = dest_dir / f"{name}_buildings.pmtiles"
    run_tippecanoe({"building": building_features(src)}, dest, [
        "-Z12", "-z16", "-r1", "--no-feature-limit", "--drop-smallest-as-needed",
        "--simplification=2", "--no-tiny-polygon-reduction-at-maximum-zoom",
        "-n", f"{name} buildings", "-A", BUILDING_CREDITS,
    ])
    log(f"wrote {_rel(dest)} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def copy_city(name: str, out_dir: Path = OUT_DIR / "cities", dest_dir: Path = PUBLIC / "cities") -> dict[str, bool]:
    """Copy the browser GLB and city.json; returns which files are now present."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src_name, ext in (("city_web.glb", "glb"), ("city.json", "json")):
        src = out_dir / name / src_name
        if src.exists():
            shutil.copyfile(src, dest_dir / f"{name}.{ext}")
            log(f"copied {name}/{src_name} -> cities/{name}.{ext}")
        else:
            log(f"skip {name}.{ext}: {src} not built yet")
    return {ext: (dest_dir / f"{name}.{ext}").exists() for ext in ("glb", "json")}


def landmark_names(labels_yaml: Path = CONFIG / "labels.yaml") -> dict[str, str]:
    """English -> Chinese landmark names from config/labels.yaml (empty if absent)."""
    if not labels_yaml.exists():
        return {}
    return yaml.safe_load(labels_yaml.read_text()).get("landmarks", {}) or {}


def cities_index(dest_dir: Path = PUBLIC / "cities") -> list[dict]:
    """cities/index.json: every configured city and which web assets exist for it."""
    zh = landmark_names()
    out = []
    for name, cfg in city_names().items():
        w, s, e, n = cfg["bbox"]
        out.append({
            "name": name, "title": cfg.get("title", {"zh": name, "en": name}), "bbox": cfg["bbox"],
            "center": [round((w + e) / 2, 5), round((s + n) / 2, 5)],
            "camera": cfg.get("camera", {}),
            "landmarks": [{"name": lm["name"], "zh": zh.get(lm["name"], ""), "lon": lm["lon"], "lat": lm["lat"]}
                          for lm in cfg.get("landmarks", [])],
            "glb": (dest_dir / f"{name}.glb").exists(),
            "json": (dest_dir / f"{name}.json").exists(),
            "buildings": (dest_dir / f"{name}_buildings.pmtiles").exists(),
        })
    return out


def write_cities_index(dest_dir: Path = PUBLIC / "cities") -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    p = dest_dir / "index.json"
    p.write_text(json.dumps(cities_index(dest_dir), ensure_ascii=False, indent=1))
    log(f"wrote {_rel(p)}")
    return p


# ---------------------------------------------------------------- gallery

def gallery_entries(scenes_yaml: Path = CONFIG / "scenes.yaml") -> list[dict]:
    """Every render the gallery may hold: scenes as <name>, cities as city-<name> (cli.py stems)."""
    out = []
    for name, sc in (yaml.safe_load(scenes_yaml.read_text()).get("scenes") or {}).items():
        out.append({"stem": name, "kind": "scene", "name": name, "title": sc.get("title", {"zh": name, "en": name})})
    for name, cfg in city_names().items():
        out.append({"stem": f"city-{name}", "kind": "city", "name": name,
                    "title": cfg.get("title", {"zh": name, "en": name})})
    return out


def _web_jpeg(src: Path, dest: Path, width: int = GALLERY_WIDTH, quality: int = 84) -> tuple[int, int]:
    from PIL import Image

    with Image.open(src) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        im.save(dest, "JPEG", quality=quality, optimize=True, progressive=True)
        return im.size


def build_gallery(src_dir: Path = GALLERY, dest_dir: Path = PUBLIC / "gallery",
                  scenes_yaml: Path = CONFIG / "scenes.yaml") -> Path:
    """Copy the gallery renders that exist into the site and list them in gallery/index.json.

    <stem>_small.jpg is copied as is; otherwise <stem>.jpg is downsized to GALLERY_WIDTH. Stale web
    copies of renders that are gone are removed, so the index only lists images that exist.
    """
    from PIL import Image

    dest_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for e in gallery_entries(scenes_yaml):
        small, full = src_dir / f"{e['stem']}_small.jpg", src_dir / f"{e['stem']}.jpg"
        dest = dest_dir / f"{e['stem']}_small.jpg"
        large = dest_dir / f"{e['stem']}_{GALLERY_LARGE}.jpg"
        if small.exists():
            shutil.copyfile(small, dest)
            with Image.open(dest) as im:
                size = im.size
        elif full.exists():
            size = _web_jpeg(full, dest)
        else:
            dest.unlink(missing_ok=True)
            large.unlink(missing_ok=True)
            continue
        if full.exists():
            _web_jpeg(full, large, GALLERY_LARGE, quality=82)
        else:
            large.unlink(missing_ok=True)
        items.append({**e, "src": f"gallery/{dest.name}", "width": size[0], "height": size[1],
                      "large": f"gallery/{large.name}" if large.exists() else None,
                      "full": f"{REPO_URL}/blob/main/gallery/{full.name}" if full.exists() else None})
    p = dest_dir / "index.json"
    p.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    log(f"wrote {_rel(p)}: {len(items)} images" + ("" if items else f" (none in {_rel(src_dir)} yet)"))
    return p


# ---------------------------------------------------------------- all

def build_webdata(china: bool = True, cities: bool = True) -> None:
    write_color_relief()
    write_scenes()
    if china:
        layers = china_layers()
        build_china_tiles(layers=layers)
        fetch_glyphs(layer_texts(layers))
    if cities:
        for name in city_names():
            build_city_tiles(name)
            copy_city(name)
        write_cities_index()
    build_gallery()
    total = sum(p.stat().st_size for p in PUBLIC.rglob("*") if p.is_file())
    log(f"web/public total {total / 1e6:.1f} MB")
