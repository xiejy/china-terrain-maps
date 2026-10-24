"""Tests for the 3D city track: GeoParquet footer/row-group helpers, the
height cascade, footprint conflation, meshes and the GLB round trip."""

from __future__ import annotations

import io
import json

import geopandas as gpd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import shapely
from PIL import Image
from shapely.geometry import Polygon, box

from chinamaps import city
from chinamaps.sources import gba, overture, sentinel2

EPSG = 32650
CFG = {
    "floor_height": 3.2,
    "gba_min_overlap": 0.5,
    "default_heights": {"residential": 18, "commercial": 24, "industrial": 12, "default": 12,
                        "small_area_m2": 60, "small": 6},
}


# --- GeoParquet footer and row groups ------------------------------------------

def _write_geoparquet(path, groups: list[list[tuple[float, float, float, float]]]) -> None:
    """One row group per list of (xmin, ymin, xmax, ymax) boxes, with GeoParquet metadata."""
    tables = []
    for g, boxes in enumerate(groups):
        geoms = [box(*b) for b in boxes]
        tables.append(pa.table({
            "id": [f"{g}-{i}" for i in range(len(boxes))],
            "geometry": shapely.to_wkb(np.array(geoms)),
            "bbox": pa.array([dict(zip(("xmin", "ymin", "xmax", "ymax"), map(float, b))) for b in boxes]),
        }))
    table = pa.concat_tables(tables)
    allb = np.array([b for grp in groups for b in grp])
    geo = {"version": "1.1.0", "primary_column": "geometry",
           "columns": {"geometry": {"encoding": "WKB", "geometry_types": ["Polygon"],
                                    "bbox": [allb[:, 0].min(), allb[:, 1].min(), allb[:, 2].max(), allb[:, 3].max()]}}}
    table = table.replace_schema_metadata({"geo": json.dumps(geo)})
    with pq.ParquetWriter(path, table.schema) as w:
        start = 0
        for grp in groups:
            w.write_table(table.slice(start, len(grp)))
            start += len(grp)


@pytest.fixture
def gpq(tmp_path):
    path = tmp_path / "t.parquet"
    _write_geoparquet(path, [
        [(0, 0, 1, 1), (1, 1, 2, 2)],        # rg 0: [0, 0, 2, 2]
        [(10, 10, 11, 11), (12, 12, 13, 13)],  # rg 1: [10, 10, 13, 13]
        [(1.5, 0, 3, 1), (20, 20, 21, 21)],   # rg 2: [1.5, 0, 21, 21]
    ])
    return path


def test_parse_geo_metadata_from_footer_tail(gpq):
    tail = gpq.read_bytes()[-16384:]
    geo = overture.parse_geo_metadata(tail)
    assert geo["primary_column"] == "geometry"
    assert overture.geo_bbox(geo) == [0, 0, 21, 21]


def test_parse_geo_metadata_synthetic_and_missing():
    doc = json.dumps({"primary_column": "geometry", "columns": {"geometry": {"bbox": [1, 2, 3, 4]}}}).encode()
    n = len(doc)
    varint = bytes([(n & 0x7F) | 0x80, n >> 7]) if n > 127 else bytes([n])
    blob = b"junk\x19<\x18\x03geo\x18" + varint + doc + b"\x00\x18\x0fschema_versions"
    assert overture.geo_bbox(overture.parse_geo_metadata(blob)) == [1, 2, 3, 4]
    assert overture.parse_geo_metadata(b"no metadata here") is None


def test_rowgroup_bounds_and_pruning(gpq):
    bounds = overture.rowgroup_bounds(pq.ParquetFile(gpq).metadata)
    np.testing.assert_allclose(bounds, [[0, 0, 2, 2], [10, 10, 13, 13], [1.5, 0, 21, 21]])
    assert overture.prune_row_groups(bounds, (0.5, 0.5, 1.2, 1.2)) == [0]
    assert overture.prune_row_groups(bounds, (10.5, 10.5, 10.6, 10.6)) == [1, 2]  # rg 2 envelope is loose
    assert overture.prune_row_groups(bounds, (50, 50, 60, 60)) == []
    bounds[1, :] = np.nan  # missing statistics are kept
    assert overture.prune_row_groups(bounds, (50, 50, 60, 60)) == [1]


def test_read_parquet_bbox_filters_rows(gpq):
    t = overture.read_parquet_bbox(gpq, (10.5, 10.5, 12.5, 12.5), ["id", "geometry", "bbox"])
    assert sorted(t["id"].to_pylist()) == ["1-0", "1-1"]
    t = overture.read_parquet_bbox(gpq, (2.5, 0.2, 2.6, 0.3))
    assert t["id"].to_pylist() == ["2-0"]
    assert overture.read_parquet_bbox(gpq, (50, 50, 60, 60)).num_rows == 0


def test_flag_fraction_and_flatten():
    flags = [{"values": ["is_tunnel"], "between": [0.2, 0.7]}, {"values": ["is_bridge"], "between": None}]
    assert overture.flag_fraction(flags, "is_tunnel") == pytest.approx(0.5)
    assert overture.flag_fraction(flags, "is_bridge") == 1.0
    assert overture.flag_fraction(None, "is_tunnel") == 0.0
    t = pa.table({
        "id": ["a"], "names": [{"primary": "Road", "common": None, "rules": None}],
        "sources": [[{"dataset": "OpenStreetMap"}]], "road_flags": [flags], "rail_flags": [None],
        "geometry": shapely.to_wkb(np.array([shapely.LineString([(0, 0), (1, 1)])])),
    })
    g = overture.to_geodataframe(t)
    assert g.loc[0, "name"] == "Road" and g.loc[0, "source_dataset"] == "OpenStreetMap"
    assert g.loc[0, "is_tunnel"] == pytest.approx(0.5) and g.crs.to_epsg() == 4326


# --- small source helpers ----------------------------------------------------------

def test_gba_tile_name():
    assert gba.tile_name(106.57, 29.56) == "e105_n30_e110_n25.parquet"
    assert gba.tile_name(114.16, 22.29) == "e110_n25_e115_n20.parquet"


def test_mgrs_100k():
    assert sentinel2.mgrs_100k(106.57, 29.56) == "48RXT"
    assert sentinel2.mgrs_100k(114.165, 22.29) == "50QKK"


def test_s2_quality_prefers_uniform_water():
    rng = np.random.default_rng(0)
    water = np.zeros((50, 50), bool)
    water[25:] = True
    clean = np.full((3, 50, 50), 0.05, np.float32)
    blotchy = clean.copy()
    blotchy[2, 25:] += rng.uniform(0, 0.15, (25, 50)).astype(np.float32)
    assert sentinel2.quality(clean, water)["score"] < sentinel2.quality(blotchy, water)["score"]
    gappy = clean.copy()
    gappy[:, :30] = np.nan
    assert sentinel2.quality(gappy, water)["score"] == 99.0


def test_true_color_range():
    rgb = np.random.default_rng(1).uniform(0, 0.3, (3, 20, 20)).astype(np.float32)
    rgb[:, 0, 0] = np.nan
    out = sentinel2.true_color(rgb)
    assert out.dtype == np.uint8 and out.shape == (20, 20, 3) and out.max() == 255


def test_load_config_titles_and_defaults():
    for name in ("chongqing-yuzhong", "hongkong-victoria-harbour"):
        c = city.load_config(name)
        assert isinstance(c["title"]["en"], str) and "," in c["title"]["en"]
        assert c["terrain_res"] == 5 and "camera" in c and "sun" in c
    with pytest.raises(KeyError):
        city.load_config("atlantis")


# --- heights cascade -----------------------------------------------------------------

def _gdf(rows: list[dict]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=EPSG)


def test_height_cascade_each_source():
    sq = lambda x, s=20: box(x, 0, x + s, s)  # noqa: E731
    bld = _gdf([
        {"id": "h", "height": 50.0, "num_floors": 3, "class": None, "geometry": sq(0)},
        {"id": "f", "height": None, "num_floors": 10, "class": None, "geometry": sq(100)},
        {"id": "g", "height": None, "num_floors": None, "class": None, "geometry": sq(200)},
        {"id": "r", "height": None, "num_floors": None, "class": "apartments", "geometry": sq(300)},
        {"id": "s", "height": None, "num_floors": None, "class": "commercial", "geometry": sq(400, 5)},
        {"id": "bad", "height": 0.5, "num_floors": 900, "class": None, "geometry": sq(500)},
        {"id": "lu", "height": None, "num_floors": None, "class": None, "geometry": sq(600)},
        {"id": "one", "height": None, "num_floors": 1, "class": None, "geometry": sq(700)},
        {"id": "roof", "height": None, "num_floors": 1, "class": "roof", "geometry": sq(800)},
    ])
    g = _gdf([
        {"id": "g1", "height": 77.0, "geometry": box(205, 0, 225, 20)},  # 75% of 'g'
        {"id": "g2", "height": 99.0, "geometry": box(315, 0, 400, 20)},  # 25% of 'r': too little
        {"id": "g3", "height": 40.0, "geometry": box(700, 0, 720, 20)},
        {"id": "g4", "height": 40.0, "geometry": box(800, 0, 820, 20)},
    ])
    lu = _gdf([{"class": "commercial", "geometry": box(590, -10, 700, 40)}])
    out = city.assign_heights(bld, g, CFG, lu).set_index("id")
    assert out.loc["h", ["height", "height_source"]].tolist() == [50.0, "overture_height"]
    assert out.loc["f", "height"] == pytest.approx(32.0) and out.loc["f", "height_source"] == "overture_floors"
    assert out.loc["g", ["height", "height_source"]].tolist() == [77.0, "gba"]
    assert out.loc["r", ["height", "height_source"]].tolist() == [18, "default"]
    assert out.loc["s", "height"] == 6  # 25 m2 < small_area_m2
    assert out.loc["bad", ["height", "height_source"]].tolist() == [12, "default"]  # invalid values ignored
    assert out.loc["lu", "height"] == 24  # commercial land use
    assert out.loc["one", ["height", "height_source"]].tolist() == [40.0, "gba"]  # suspect single floor
    assert out.loc["roof", ["height", "height_source"]].tolist() == [pytest.approx(3.2), "overture_floors"]


def test_gba_match_best_overlap():
    geoms = np.array([box(0, 0, 10, 10)])
    gg = np.array([box(0, 0, 4, 10), box(3, 0, 10, 10)])
    h, f = city.gba_match(geoms, gg, np.array([10.0, 30.0]), 0.5)
    assert h[0] == 30.0 and f[0] == pytest.approx(0.7)
    h, _ = city.gba_match(geoms, gg, np.array([10.0, 30.0]), 0.8)
    assert np.isnan(h[0])


def test_clean_footprints_drops_tiny_and_fixes_invalid():
    bow = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])  # self-intersecting bowtie
    g = _gdf([{"id": "a", "geometry": bow}, {"id": "b", "geometry": box(0, 0, 2, 2)},
              {"id": "c", "geometry": box(0, 0, 30, 30)}])
    out = city.clean_footprints(g)
    assert list(out["id"]) == ["a", "c"] and out.is_valid.all()
    assert out.geometry.iloc[0].geom_type == "MultiPolygon"


def test_attach_parts_replaces_covered_parent():
    bld = _gdf([{"id": "p", "height": 100.0, "height_source": "overture_height", "class": "office",
                 "source_dataset": "OpenStreetMap", "geometry": box(0, 0, 20, 20)},
                {"id": "q", "height": 30.0, "height_source": "gba", "class": None,
                 "source_dataset": "x", "geometry": box(50, 0, 70, 20)}])
    parts = _gdf([
        {"id": "p1", "building_id": "p", "height": 40.0, "num_floors": None, "min_height": None,
         "source_dataset": None, "geometry": box(0, 0, 20, 10)},
        {"id": "p2", "building_id": "p", "height": None, "num_floors": None, "min_height": 40.0,
         "source_dataset": None, "geometry": box(0, 10, 20, 20)},
        {"id": "q1", "building_id": "q", "height": 50.0, "num_floors": None, "min_height": None,
         "source_dataset": None, "geometry": box(50, 0, 55, 5)},
    ])
    out = city.attach_parts(bld, parts, CFG).set_index("id")
    assert "p" not in out.index and "q" in out.index  # q's part covers only 6%
    assert out.loc["p2", ["height", "min_height", "height_source"]].tolist() == [100.0, 40.0, "overture_height"]
    assert out.loc["p1", "part_of"] == "p" and out.loc["p1", "class"] == "office"


def test_conflate_gba_replaces_ml_duplicates():
    bld = _gdf([
        {"id": "osm", "height": 30.0, "height_source": "overture_height", "source_dataset": "OpenStreetMap",
         "geometry": box(0, 0, 20, 20)},
        {"id": "ml_dup", "height": 18.0, "height_source": "default", "source_dataset": "ml",
         "geometry": box(100, 6, 120, 26)},  # 6 m off a GBA footprint
        {"id": "ml_alone", "height": 6.0, "height_source": "default", "source_dataset": "ml",
         "geometry": box(300, 0, 310, 10)},
    ])
    g = _gdf([{"id": "1", "height": 64.0, "source": "ours2", "geometry": box(100, 0, 120, 20)},
              {"id": "2", "height": 12.0, "source": "osm", "geometry": box(1, 1, 19, 19)}])  # = osm building
    out = city.conflate_gba(bld, g).set_index("id")
    assert sorted(out.index) == ["gba:1", "ml_alone", "osm"]
    assert out.loc["gba:1", ["height", "height_source"]].tolist() == [64.0, "gba"]


def test_separate_coplanar():
    bld = _gdf([{"height": 30.0, "base_z": 10.0, "geometry": box(0, 0, 50, 50)},
                {"height": 30.0, "base_z": 10.0, "geometry": box(10, 10, 20, 20)},
                {"height": 30.0, "base_z": 10.0, "geometry": box(100, 0, 110, 10)}])
    out = city.separate_coplanar(bld)
    assert out["height"].tolist() == [30.0, pytest.approx(29.4), 30.0]


def test_base_heights_use_footprint_minimum():
    grid = city.Grid(EPSG, 0, 0, 100, 100)
    dem = np.tile(np.linspace(100, 0, 21)[:, None], (1, 21))  # north edge 100 m, south 0 m (5 m nodes)
    z = city.base_heights(np.array([box(10, 10, 30, 50)]), dem, grid, 5.0)
    assert z[0] == pytest.approx(10.0)


# --- meshes and GLB ----------------------------------------------------------------

def test_extrude_square_with_hole():
    grid = city.Grid(EPSG, 0, 0, 100, 100)
    ring = box(10, 10, 30, 30).difference(box(15, 15, 25, 25))
    m = city.extrude(np.array([ring]), np.array([5.0]), np.array([25.0]), grid)
    assert len(m.positions) == 8 * 4 + 8  # 8 wall quads + 8 roof vertices
    assert len(m.indices) == 16 + 8
    p, i, n = m.positions, m.indices, m.normals
    a, b, c = p[i[:, 0]], p[i[:, 1]], p[i[:, 2]]
    face = np.cross(b - a, c - a)
    assert (np.einsum("ij,ij->i", face, n[i[:, 0]]) > 0).all()  # winding agrees with normals
    roof = n[i[:, 0], 1] > 0.99
    assert roof.sum() == 8 and np.allclose(a[roof][:, 1], 25)
    centre = np.array([20.0, 15.0, -20.0])
    walls = ~roof
    mid = (a[walls] + b[walls] + c[walls]) / 3
    outward = np.einsum("ij,ij->i", (mid - centre)[:, [0, 2]], n[i[walls, 0]][:, [0, 2]])
    hole = np.abs(mid[:, [0, 2]] - centre[[0, 2]]).max(axis=1) < 5.01
    assert (outward[~hole] > 0).all() and (outward[hole] < 0).all()  # courtyard walls face inwards


def test_glb_round_trip(tmp_path):
    import trimesh

    grid = city.Grid(EPSG, 1000, 2000, 1100, 2080)
    res = 5.0
    _, (h, w) = grid.nodes(res)
    dem = (50 + np.add.outer(np.arange(h), np.arange(w)) * 0.5).astype(np.float32)
    img = np.zeros((40, 50, 3), np.uint8)
    img[:20, :25] = (255, 0, 0)  # NW quadrant red
    terrain = city.terrain_mesh(dem, grid, city.jpeg_bytes(img, 95))
    polys = np.array([box(1010, 2010, 1030, 2030), box(1050, 2040, 1060, 2070)])
    blds = city.extrude(polys, np.array([50.0, 60.0]), np.array([90.0, 70.0]), grid,
                        city.height_tint(np.array([40.0, 10.0])))
    path = tmp_path / "city.glb"
    city.write_glb(path, [terrain, blds], {"origin": [grid.xmin, grid.ymin]})

    info = city.inspect_glb(path)
    assert info["nodes"] == ["terrain", "buildings"]
    assert info["images"][0]["mime"] == "image/jpeg" and info["images"][0]["size"] == (50, 40)
    assert info["y_range"]["terrain"] == pytest.approx([float(dem.min()), float(dem.max())])

    scene = trimesh.load(path, force="scene")
    assert set(scene.geometry) >= {"terrain", "buildings"}
    t = scene.geometry["terrain"]
    np.testing.assert_allclose(t.bounds, [[0, dem.min(), -80], [100, dem.max(), 0]], atol=1e-3)
    b = scene.geometry["buildings"]
    np.testing.assert_allclose(b.bounds, [[10, 50, -70], [60, 90, -10]], atol=1e-3)
    assert len(t.faces) == 2 * (h - 1) * (w - 1)
    # UV (0, 0) is the NW corner in glTF; trimesh flips V on load
    nw = np.argmin(t.vertices[:, 0] + t.vertices[:, 2])  # x = 0, z = -(N - ymin) most negative
    se = np.argmax(t.vertices[:, 0] + t.vertices[:, 2])
    assert t.visual.uv[nw] == pytest.approx([0, 1], abs=1e-6)
    assert t.visual.uv[se] == pytest.approx([1, 0], abs=1e-6)
    tex = np.asarray(t.visual.material.baseColorTexture.convert("RGB"))
    assert tex[5, 5, 0] > 200 and tex[35, 45, 0] < 60


def test_height_tint_glass_towers():
    t = city.height_tint(np.array([3.0, 30.0, 120.0, 250.0])).astype(int)
    assert (t[0, :3] < t[1, :3]).all()             # low-rise darker than mid-rise
    r, g, b = t[3, :3]
    assert b > g > r and r < 190                   # towers above GLASS_HEIGHTS: cool glass grey-blue
    assert t[1, 0] > t[2, 0] > t[3, 0]              # red falls off toward the glass tint


def test_water_and_sand_masks():
    from rasterio.transform import from_origin

    tr = from_origin(0, 100, 1, 1)
    water = gpd.GeoSeries([box(20, 20, 80, 80)])
    wm = city.water_mask(water, tr, (100, 100))
    assert wm[50, 50] == pytest.approx(1) and wm[5, 5] == 0 and 0 < wm[50, 20] < 1  # soft edge
    img = np.zeros((100, 100, 3), np.float32)
    img[..., 2] = 0.4                               # blue water ...
    img[40:60, 40:60] = (0.55, 0.45, 0.30)          # ... with a warm sand bar inside
    img[0:10, 0:10] = (0.55, 0.45, 0.30)            # warm land outside the water polygon
    sand = city.sand_mask(img, wm)
    assert sand[50, 50] > 0.8 and sand[30, 30] == 0 and sand[5, 5] == 0
    rgb = (np.dstack([img] * 1) * 10000).transpose(2, 0, 1) / 10000
    out = city.ground_texture(rgb.astype(np.float32), tr, water, None, water_style={"color": (0, 0, 255), "alpha": 1.0,
                                                                                    "sand": False})
    assert tuple(out[50, 30]) == (0, 0, 255)       # flat harbour colour, no sand


def test_smooth_under_water_only():
    dem = np.zeros((40, 40), np.float32)
    dem[::2] = 10.0                                 # stair-steps everywhere
    wm = np.zeros_like(dem)
    wm[:, 20:] = 1.0
    out = city.smooth_under_water(dem, wm, sigma=3.0)
    assert np.ptp(out[10:30, 28:36]) < 2.0          # smoothed under water
    np.testing.assert_array_equal(out[:, :10], dem[:, :10])  # untouched on land


def test_vegetated_flags_green_canopy():
    from rasterio.transform import from_origin

    tr = from_origin(0, 100, 1, 1)
    rgb = np.full((3, 100, 100), 0.1, np.float32)
    rgb[:, :, 50:] = np.array([0.05, 0.15, 0.04])[:, None, None]   # forest on the east half
    geoms = np.array([box(10, 40, 20, 50), box(70, 40, 80, 50)])
    assert city.vegetated(geoms, rgb, tr).tolist() == [False, True]


def test_jpeg_bytes_downscale():
    data = city.jpeg_bytes(np.zeros((300, 600, 3), np.uint8), 80, max_side=200)
    assert Image.open(io.BytesIO(data)).size == (200, 100)


def test_grid_from_bbox_and_local_frame():
    g = city.Grid.from_bbox((114.14, 22.27, 114.19, 22.315), EPSG)
    assert g.xmin % 10 == 0 and g.ymax % 10 == 0 and 5000 < g.width < 5500
    tr, shape = g.raster(2.0)
    assert shape == (round(g.height / 2), round(g.width / 2)) and tr.c == g.xmin
    loc = g.to_local([g.xmin + 10], [g.ymin + 20], [5.0])
    np.testing.assert_allclose(loc, [[10, 5, -20]])
    lb = g.lonlat_bbox(0)
    assert lb[0] <= 114.14 and lb[2] >= 114.19


# --- network -------------------------------------------------------------------------

@pytest.mark.network
def test_overture_release_index_and_bbox_read():
    rel = overture.latest_release()
    assert rel[:2] == "20"
    idx = overture.file_index("buildings", "building_part", rel)
    assert idx and all(len(f["bbox"]) == 4 for f in idx)
    bbox = (114.155, 22.280, 114.165, 22.290)
    t = overture.read_bbox("buildings", "building_part", bbox, rel)
    assert t.num_rows > 10
    g = overture.to_geodataframe(t)
    assert g.intersects(box(*bbox)).all()


@pytest.mark.network
def test_sentinel2_resolves_hk_tile():
    assert sentinel2.resolve_tiles((114.14, 22.27, 114.19, 22.315), 32650, 2025, "Q4") == ["50QKK_0_0"]


@pytest.mark.network
def test_gba_bbox_read():
    g = gba.read_bbox("e110_n25_e115_n20.parquet", (114.157, 22.283, 114.160, 22.286))
    assert len(g) > 0 and g["height"].notna().all()
