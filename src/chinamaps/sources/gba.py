"""GlobalBuildingAtlas LoD1 footprints with heights (CC BY-NC 4.0).

5 x 5 degree GeoParquet tiles on source.coop, spatially sorted in row groups of
16,384 rows; read by bbox with row-group pruning. Heights are complete but
saturate around 165-200 m, so tall landmarks should keep other sources.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import geopandas as gpd
import shapely

from chinamaps.net import SOURCE_COOP
from chinamaps.sources.overture import Bbox, read_parquet_bbox

GBA_BASE = f"{SOURCE_COOP}/tge-labs/globalbuildingatlas-lod1"
LICENSE = "CC BY-NC 4.0"


def tile_name(lon: float, lat: float) -> str:
    """5-degree tile covering (lon, lat), e.g. e105_n30_e110_n25.parquet."""
    w = int(math.floor(lon / 5) * 5)
    n = int(math.floor(lat / 5) * 5) + 5

    def fmt(v: int, pos: str, neg: str) -> str:
        return f"{pos if v >= 0 else neg}{abs(v)}"

    return f"{fmt(w, 'e', 'w')}_{fmt(n, 'n', 's')}_{fmt(w + 5, 'e', 'w')}_{fmt(n - 5, 'n', 's')}.parquet"


def tile_url(tile: str) -> str:
    return f"{GBA_BASE}/{tile}"


def read_bbox(tile: str, bbox: Bbox) -> gpd.GeoDataFrame:
    """GBA footprints (EPSG:4326) with id, height, var, source intersecting bbox."""
    t = read_parquet_bbox(tile_url(tile), bbox, ["source", "id", "height", "var", "geometry", "bbox"])
    return gpd.GeoDataFrame(
        {
            "id": t["id"].to_numpy(zero_copy_only=False),
            "height": t["height"].to_numpy(zero_copy_only=False),
            "var": t["var"].to_numpy(zero_copy_only=False),
            "source": t["source"].to_numpy(zero_copy_only=False),
        },
        geometry=shapely.from_wkb(t["geometry"].to_numpy(zero_copy_only=False)),
        crs=4326,
    )


def extract(tile: str, bbox: Bbox, dest: Path) -> gpd.GeoDataFrame:
    """Cached bbox extract as GeoParquet."""
    dest = Path(dest)
    if dest.exists():
        return gpd.read_parquet(dest)
    gdf = read_bbox(tile, bbox)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    gdf.to_parquet(tmp)
    tmp.replace(dest)
    dest.with_suffix(".json").write_text(json.dumps({"tile": tile, "bbox": list(bbox), "n": len(gdf)}))
    return gdf
