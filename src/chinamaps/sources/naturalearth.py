"""Natural Earth v5.1.2 vectors (GeoJSON via raw.githubusercontent.com).

Two worldviews:
  cn      - Natural Earth's China point-of-view admin-0 layer with CHN, HKG,
            MAC and SCR (Scarborough Shoal) dissolved into one China, plus the
            China-view maritime indicator (the 9 South China Sea dashes).
  default - Natural Earth's default de facto boundaries.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import box
from shapely.ops import linemerge, unary_union

from chinamaps.net import DATA_DIR, download

NE_VERSION = "v5.1.2"
NE_BASE = f"https://raw.githubusercontent.com/nvkelso/natural-earth-vector/{NE_VERSION}/geojson"
NE_DIR = DATA_DIR / "naturalearth" / NE_VERSION

LAYERS = (
    "ne_10m_admin_0_countries",
    "ne_10m_admin_0_countries_chn",
    "ne_10m_admin_0_boundary_lines_land",
    "ne_10m_admin_0_boundary_lines_disputed_areas",
    "ne_10m_admin_0_boundary_lines_maritime_indicator_chn",
    "ne_10m_admin_1_states_provinces",
    "ne_10m_land",
    "ne_10m_coastline",
    "ne_10m_minor_islands",
    "ne_10m_rivers_lake_centerlines_scale_rank",
    "ne_10m_lakes",
    "ne_10m_glaciated_areas",
    "ne_10m_geography_regions_polys",
    "ne_10m_geography_regions_elevation_points",
    "ne_10m_geography_marine_polys",
    "ne_10m_populated_places",
)

CN_MERGE = ("CHN", "HKG", "MAC", "SCR")


def layer_path(name: str) -> Path:
    if name not in LAYERS:
        raise KeyError(f"unknown Natural Earth layer {name!r}")
    return download(f"{NE_BASE}/{name}.geojson", NE_DIR / f"{name}.geojson")


@lru_cache(maxsize=None)
def _read(name: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(layer_path(name))
    return gdf.set_crs(4326, allow_override=True)


def read(name: str) -> gpd.GeoDataFrame:
    """Read a layer (cached in memory; returns a copy)."""
    return _read(name).copy()


def countries(worldview: str = "cn") -> gpd.GeoDataFrame:
    """Admin-0 countries. In the cn worldview China is one merged feature."""
    if worldview == "default":
        return read("ne_10m_admin_0_countries")
    if worldview != "cn":
        raise ValueError(worldview)
    gdf = read("ne_10m_admin_0_countries_chn")
    code = gdf["ADM0_A3"]
    china = gdf[code.isin(CN_MERGE)]
    rest = gdf[~code.isin(CN_MERGE)]
    merged = china[china["ADM0_A3"] == "CHN"].iloc[[0]].copy()
    merged["geometry"] = [unary_union(china.geometry.values)]
    return gpd.GeoDataFrame(pd.concat([rest, merged], ignore_index=True), crs=4326)


def china(worldview: str = "cn"):
    """China (multi)polygon for the given worldview."""
    c = countries(worldview)
    return c[c["ADM0_A3"] == "CHN"].geometry.iloc[0]


def scs_dashes() -> gpd.GeoDataFrame:
    """China-view maritime indicator line (the South China Sea dashes)."""
    return read("ne_10m_admin_0_boundary_lines_maritime_indicator_chn")


def boundary_lines(worldview: str = "cn", bbox: tuple[float, float, float, float] | None = None) -> gpd.GeoDataFrame:
    """International land boundary lines for drawing.

    For the cn worldview the lines are the shared edges of the merged country
    polygons, so internal CHN/HKG/MAC lines disappear and disputed sections
    follow the China-view polygons. Column ``china`` marks China's own border.
    """
    if worldview == "default":
        gdf = read("ne_10m_admin_0_boundary_lines_land")
        return gdf.clip(bbox) if bbox else gdf
    c = countries("cn")
    if bbox:
        c = c[c.intersects(box(*bbox))]
    eps = 1e-3
    rows = []
    geoms = list(c.geometry.values)
    codes = list(c["ADM0_A3"].values)
    for i, (code, g) in enumerate(zip(codes, geoms)):
        for j in range(i + 1, len(geoms)):
            if not g.buffer(eps).intersects(geoms[j]):
                continue
            edge = g.boundary.intersection(geoms[j].buffer(eps))
            edge = linemerge_any(edge)
            if edge is not None and not edge.is_empty and edge.length > 0:
                rows.append({"a": code, "b": codes[j], "china": "CHN" in (code, codes[j]), "geometry": edge})
    gdf = gpd.GeoDataFrame(rows, columns=["a", "b", "china", "geometry"], geometry="geometry", crs=4326)
    return gdf.clip(bbox) if bbox else gdf


def linemerge_any(geom):
    """Reduce an intersection result to (multi)lines, merged where possible."""
    lines = [p for p in getattr(geom, "geoms", [geom]) if p.geom_type in ("LineString", "MultiLineString")]
    if not lines:
        return None
    u = unary_union(lines)
    return linemerge(u) if u.geom_type == "MultiLineString" else u


def provinces(worldview: str = "cn") -> gpd.GeoDataFrame:
    """Admin-1 divisions of China (cn worldview includes Taiwan, HK and Macao)."""
    gdf = read("ne_10m_admin_1_states_provinces")
    codes = ("CHN", "TWN", "HKG", "MAC") if worldview == "cn" else ("CHN",)
    return gdf[gdf["adm0_a3"].isin(codes)].reset_index(drop=True)
