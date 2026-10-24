"""Overture Maps GeoParquet extracts by bbox over unsigned HTTPS.

The latest release is discovered from the bucket listing. A per-release
file -> bbox index (from each file's GeoParquet footer) is cached under
data/overture/<release>/, then only row groups whose ``bbox`` column statistics
intersect the query are read. The generic helpers (S3 listing, footer bbox,
row-group pruning) are shared with the GlobalBuildingAtlas and Sentinel-2 readers.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import shapely

from chinamaps.net import DATA_DIR, HTTPRangeFile, get_tail, session

OVERTURE = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com"
OVERTURE_DIR = DATA_DIR / "overture"
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

Bbox = tuple[float, float, float, float]

# Columns read per (theme, type); nested names/sources are flattened on extract.
COLUMNS = {
    ("buildings", "building"): ["id", "names", "sources", "height", "num_floors", "min_height",
                                "class", "subtype", "has_parts", "is_underground", "geometry", "bbox"],
    ("buildings", "building_part"): ["id", "building_id", "sources", "height", "num_floors", "min_height",
                                     "is_underground", "geometry", "bbox"],
    ("base", "water"): ["id", "names", "subtype", "class", "is_salt", "is_intermittent", "geometry", "bbox"],
    ("base", "land_use"): ["id", "names", "subtype", "class", "geometry", "bbox"],
    ("transportation", "segment"): ["id", "names", "subtype", "class", "subclass", "road_flags", "rail_flags",
                                    "geometry", "bbox"],
}


# --- S3 listing ------------------------------------------------------------

def s3_list(base: str, prefix: str, delimiter: str | None = None) -> tuple[list[tuple[str, int]], list[str]]:
    """ListObjectsV2 over anonymous HTTPS: ([(key, size)], [common prefixes]), all pages."""
    keys: list[tuple[str, int]] = []
    prefixes: list[str] = []
    token = None
    while True:
        params = {"list-type": "2", "prefix": prefix}
        if delimiter:
            params["delimiter"] = delimiter
        if token:
            params["continuation-token"] = token
        r = session().get(base + "/", params=params, timeout=60)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for c in root.iter(f"{S3_NS}Contents"):
            keys.append((c.findtext(f"{S3_NS}Key"), int(c.findtext(f"{S3_NS}Size"))))
        for p in root.iter(f"{S3_NS}CommonPrefixes"):
            prefixes.append(p.findtext(f"{S3_NS}Prefix"))
        token = root.findtext(f"{S3_NS}NextContinuationToken")
        if not token:
            return keys, prefixes


def releases() -> list[str]:
    """Available Overture releases, oldest first (e.g. '2026-10-21.0')."""
    _, prefixes = s3_list(OVERTURE, "release/", "/")
    return sorted(p.split("/")[1] for p in prefixes)


def latest_release() -> str:
    return releases()[-1]


# --- GeoParquet footer bbox index -------------------------------------------

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = val = 0
    while True:
        b = buf[i]
        val |= (b & 0x7F) << shift
        i += 1
        if not b & 0x80:
            return val, i
        shift += 7


def parse_geo_metadata(tail: bytes) -> dict | None:
    """Find the GeoParquet 'geo' key/value in raw footer bytes (thrift compact).

    The key is the binary b'\\x03geo', followed by the value field header 0x18
    and a varint length.
    """
    i = tail.rfind(b"\x03geo\x18")
    if i < 0:
        return None
    n, j = _varint(tail, i + 5)
    if j + n > len(tail):
        return None
    try:
        return json.loads(tail[j : j + n])
    except ValueError:
        return None


def geo_bbox(geo: dict) -> list[float] | None:
    col = geo.get("columns", {}).get(geo.get("primary_column", "geometry"), {})
    return col.get("bbox")


def file_bbox(url: str, size: int | None = None) -> list[float]:
    """File bbox from the footer: last 16 KB first, full metadata as fallback."""
    geo = parse_geo_metadata(get_tail(url, 16384))
    bbox = geo_bbox(geo) if geo else None
    if bbox is None:
        md = pq.ParquetFile(HTTPRangeFile(url, size)).metadata
        bbox = geo_bbox(json.loads(md.metadata[b"geo"]))
    return [float(v) for v in bbox]


def file_index(theme: str, type_: str, release: str, workers: int = 4) -> list[dict]:
    """[{key, size, bbox}] for every file of a theme/type, cached per release."""
    path = OVERTURE_DIR / release / f"{theme}_{type_}.index.json"
    if path.exists():
        return json.loads(path.read_text())
    keys, _ = s3_list(OVERTURE, f"release/{release}/theme={theme}/type={type_}/")
    keys = [(k, s) for k, s in keys if k.endswith(".parquet")]
    with ThreadPoolExecutor(workers) as ex:
        bboxes = list(ex.map(lambda ks: file_bbox(f"{OVERTURE}/{ks[0]}", ks[1]), keys))
    index = [{"key": k, "size": s, "bbox": b} for (k, s), b in zip(keys, bboxes)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index))
    return index


def bbox_intersects(a, b) -> bool:
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


# --- Row-group pruning and bbox reads ---------------------------------------

def rowgroup_bounds(md: pq.FileMetaData, column: str = "bbox") -> np.ndarray:
    """(n_row_groups, 4) [xmin, ymin, xmax, ymax] envelope from the bbox column
    statistics (min of xmin/ymin, max of xmax/ymax). NaN where stats are missing."""
    names = [md.row_group(0).column(i).path_in_schema for i in range(md.num_columns)] if md.num_row_groups else []
    idx = {k: names.index(f"{column}.{k}") for k in ("xmin", "ymin", "xmax", "ymax")}
    out = np.full((md.num_row_groups, 4), np.nan)
    for r in range(md.num_row_groups):
        rg = md.row_group(r)
        for j, (k, agg) in enumerate((("xmin", "min"), ("ymin", "min"), ("xmax", "max"), ("ymax", "max"))):
            st = rg.column(idx[k]).statistics
            if st is not None and st.has_min_max:
                out[r, j] = getattr(st, agg)
    return out


def prune_row_groups(bounds: np.ndarray, bbox: Bbox) -> list[int]:
    """Row groups whose envelope intersects bbox (missing stats are kept)."""
    xmin, ymin, xmax, ymax = bounds.T
    with np.errstate(invalid="ignore"):
        miss = np.isnan(bounds).any(axis=1)
        hit = (xmin <= bbox[2]) & (xmax >= bbox[0]) & (ymin <= bbox[3]) & (ymax >= bbox[1])
    return [int(i) for i in np.flatnonzero(hit | miss)]


def filter_table_bbox(table: pa.Table, bbox: Bbox, column: str = "bbox") -> pa.Table:
    """Rows whose per-feature bbox struct intersects bbox."""
    b = table[column]
    f = lambda k: pc.struct_field(b, k)  # noqa: E731
    mask = pc.and_(
        pc.and_(pc.less_equal(f("xmin"), bbox[2]), pc.greater_equal(f("xmax"), bbox[0])),
        pc.and_(pc.less_equal(f("ymin"), bbox[3]), pc.greater_equal(f("ymax"), bbox[1])),
    )
    return table.filter(mask)


def read_parquet_bbox(url: str | Path, bbox: Bbox, columns: list[str] | None = None,
                      size: int | None = None) -> pa.Table:
    """Read only the row groups of a GeoParquet (https URL or local path) that
    intersect bbox, then filter rows by their bbox struct."""
    src = HTTPRangeFile(url, size) if str(url).startswith("http") else str(url)
    pf = pq.ParquetFile(src)
    groups = prune_row_groups(rowgroup_bounds(pf.metadata), bbox)
    schema = pf.schema_arrow
    cols = [c for c in columns if c in schema.names] if columns else None
    if not groups:
        return schema.empty_table().select(cols) if cols else schema.empty_table()
    parts = [filter_table_bbox(pf.read_row_group(g, columns=cols), bbox) for g in groups]
    return pa.concat_tables(parts)


def read_bbox(theme: str, type_: str, bbox: Bbox, release: str | None = None,
              columns: list[str] | None = None) -> pa.Table:
    """Overture features of theme/type whose bbox intersects bbox (lon/lat)."""
    release = release or latest_release()
    columns = columns or COLUMNS.get((theme, type_))
    tables = [
        read_parquet_bbox(f"{OVERTURE}/{f['key']}", bbox, columns, f["size"])
        for f in file_index(theme, type_, release)
        if bbox_intersects(f["bbox"], bbox)
    ]
    tables = [t for t in tables if t.num_rows]
    if not tables:
        raise RuntimeError(f"no Overture {theme}/{type_} features in {bbox} ({release})")
    return pa.concat_tables(tables, promote_options="permissive")


# --- Flattening to plain GeoDataFrames ---------------------------------------

def _first_dataset(sources) -> str | None:
    if sources is None or len(sources) == 0:
        return None
    ds = sorted({s.get("dataset") for s in sources if s and s.get("dataset")})
    return ",".join(ds) or None


def flag_fraction(flags, flag: str) -> float:
    """Fraction of a segment covered by a road/rail flag (e.g. 'is_tunnel')."""
    total = 0.0
    for f in flags or ():
        if f and flag in (f.get("values") or ()):
            between = f.get("between")
            total += 1.0 if not between else max(0.0, between[1] - between[0])
    return min(total, 1.0)


def to_geodataframe(table: pa.Table) -> gpd.GeoDataFrame:
    """Flatten names -> name, sources -> source_dataset, road/rail flags ->
    is_tunnel/is_bridge (covered fraction), WKB -> shapely (EPSG:4326)."""
    cols = {}
    flags = [[] for _ in range(table.num_rows)]
    for name in table.column_names:
        col = table[name]
        if name in ("geometry", "bbox"):
            continue
        if name == "names":
            cols["name"] = pc.struct_field(col, "primary").to_numpy(zero_copy_only=False)
        elif name == "sources":
            cols["source_dataset"] = [_first_dataset(s) for s in col.to_pylist()]
        elif name in ("road_flags", "rail_flags"):
            for acc, f in zip(flags, col.to_pylist()):
                acc.extend(f or ())
        else:
            cols[name] = col.to_numpy(zero_copy_only=False)
    if "road_flags" in table.column_names or "rail_flags" in table.column_names:
        cols["is_tunnel"] = np.array([flag_fraction(f, "is_tunnel") for f in flags])
        cols["is_bridge"] = np.array([flag_fraction(f, "is_bridge") for f in flags])
    geom = shapely.from_wkb(table["geometry"].to_numpy(zero_copy_only=False))
    return gpd.GeoDataFrame(cols, geometry=geom, crs=4326)


def extract(theme: str, type_: str, bbox: Bbox, dest: Path, release: str | None = None) -> gpd.GeoDataFrame:
    """Cached extract as plain GeoParquet (dest); release recorded in a sidecar."""
    dest = Path(dest)
    if dest.exists():
        return gpd.read_parquet(dest)
    release = release or latest_release()
    gdf = to_geodataframe(read_bbox(theme, type_, bbox, release))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    gdf.to_parquet(tmp)
    tmp.replace(dest)
    dest.with_suffix(".json").write_text(json.dumps({"release": release, "theme": theme, "type": type_,
                                                     "bbox": list(bbox), "n": len(gdf)}))
    return gdf
