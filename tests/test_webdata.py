"""Tests for chinamaps.webdata (web map assets)."""

from __future__ import annotations

import json
import math

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box

from chinamaps import webdata as wd
from chinamaps.sources import naturalearth as ne

# ---------------------------------------------------------------- scenes.json math

@pytest.mark.parametrize("azimuth, bearing", [(155, 335), (190, 10), (180, 0), (0, 180), (300, 120), (345, 165)])
def test_bearing_looks_away_from_camera(azimuth, bearing):
    vp = wd.scene_viewpoint({"bounds": [100, 20, 110, 30], "camera": {"azimuth": azimuth, "elevation": 30}})
    assert vp["bearing"] == bearing
    assert 0 <= vp["bearing"] < 360


@pytest.mark.parametrize("elevation, pitch", [(34, 56), (26, 64), (90, 0), (0, 85), (-10, 85), (100, 0)])
def test_pitch_is_complement_of_elevation_clamped(elevation, pitch):
    vp = wd.scene_viewpoint({"bounds": [100, 20, 110, 30], "camera": {"azimuth": 180, "elevation": elevation}})
    assert vp["pitch"] == pitch


def test_fit_zoom_whole_world_is_zero():
    assert wd.fit_zoom((-180, -85.0511, 180, 85.0511), viewport=(512, 512)) == pytest.approx(0, abs=1e-3)


def test_fit_zoom_halves_with_extent():
    z1 = wd.fit_zoom((100, 20, 101, 21))
    z2 = wd.fit_zoom((100, 20, 102, 22))
    assert z1 - z2 == pytest.approx(1, abs=0.02)


def test_center_uses_mercator_midpoint_and_distance_zooms_out():
    vp = wd.scene_viewpoint({"bounds": [70, -10, 140, 10], "camera": {"distance": 1.0}})
    assert vp["center"] == [105.0, pytest.approx(0, abs=1e-6)]
    vp2 = wd.scene_viewpoint({"bounds": [70, 2, 140, 56]})
    assert vp2["center"][1] > (2 + 56) / 2  # Mercator stretches the north
    far = wd.scene_viewpoint({"bounds": [70, -10, 140, 10], "camera": {"distance": 2.0}})
    assert vp["zoom"] - far["zoom"] == pytest.approx(1, abs=0.02)


def test_scenes_json_from_config():
    scenes = wd.scenes_json()
    names = {s["name"] for s in scenes}
    assert "china" in names
    for s in scenes:
        assert set(s) >= {"name", "title", "center", "zoom", "bearing", "pitch", "bounds"}
        w, so, e, n = s["bounds"]
        assert w <= s["center"][0] <= e and so <= s["center"][1] <= n
        assert 0 <= s["pitch"] <= 85 and 0 <= s["bearing"] < 360
        assert 1 <= s["exaggeration"] <= 5
        assert 0 < s["zoom"] < 16


def test_scenes_json_merges_defaults(tmp_path):
    p = tmp_path / "scenes.yaml"
    p.write_text(
        "defaults: {camera: {azimuth: 90, elevation: 40}}\n"
        "scenes:\n"
        "  a: {title: {zh: 甲, en: A}, bounds: [100, 30, 101, 31]}\n"
        "  b: {bounds: [100, 30, 101, 31], camera: {azimuth: 270, elevation: 20}, exaggeration: 20}\n"
    )
    a, b = wd.scenes_json(p)
    assert (a["bearing"], a["pitch"]) == (270, 50)
    assert (b["bearing"], b["pitch"], b["exaggeration"]) == (90, 70, 5.0)
    assert b["title"] == {"zh": "b", "en": "b"}


# ---------------------------------------------------------------- palettes, glyphs, small helpers

def test_color_relief_stops_sorted_hex():
    for stops in (wd.color_relief_stops(), wd.render_relief_stops()):
        zs = [z for z, _ in stops]
        assert zs == sorted(zs) and len(set(zs)) == len(zs)
        assert all(isinstance(c, str) and c.startswith("#") and len(c) == 7 for _, c in stops)
    render = {z: c for z, c in wd.render_relief_stops()}
    assert 0 in render and -1 in render and render[0] != render[-1]  # sea level is land


def test_write_color_relief(tmp_path):
    p = wd.write_color_relief(tmp_path / "color_relief.json")
    doc = json.loads(p.read_text())
    assert set(doc) == {"web", "render"}


def test_glyph_ranges_skip_cjk():
    assert wd.glyph_ranges([]) == [0]
    assert wd.glyph_ranges(["珠穆朗玛峰", "Ürümqi"]) == [0]
    assert wd.glyph_ranges(["Lhāsa"]) == [0, 256]
    assert wd.glyph_ranges(["青 藏", "a–b"]) == [0, 8192]
    assert wd.glyph_ranges(["（香港）", "ソウル", "서울"]) == [0]  # fullwidth, kana, hangul are local


def test_spaced():
    assert wd.spaced("青藏高原") == "青 藏 高 原"


@pytest.mark.parametrize("adm0, cap, fc, expected", [
    ("CHN", 1, "Admin-0 capital", "country"),
    ("CHN", 0, "Admin-1 capital", "province"),
    ("TWN", 1, "Admin-0 capital", "province"),
    ("TWN", 0, "Admin-1 capital", ""),
    ("HKG", 0, "Admin-0 region capital", "province"),
    ("IND", 1, "Admin-0 capital", "country"),
    ("CHN", 0, "Populated place", ""),
])
def test_place_capital_cn_worldview(adm0, cap, fc, expected):
    assert wd.place_capital(adm0, cap, fc) == expected


def test_feature_drops_nan_and_sets_minzoom():
    f = wd._feature(Point(1, 2), {"a": float("nan"), "b": 3, "c": None}, 4.7)
    assert f["properties"] == {"b": 3}
    assert f["tippecanoe"] == {"minzoom": 4}
    assert f["geometry"]["type"] == "Point"
    assert "tippecanoe" not in wd._feature(Point(1, 2), {})


# ---------------------------------------------------------------- GeoJSON layers (offline)

def test_province_lines_are_shared_edges_only():
    provs = gpd.GeoDataFrame(
        {"zh": ["甲", "乙", "丙"], "en": ["a", "b", "c"], "lon": [0.5, 1.5, 5.5], "lat": [0.5, 0.5, 0.5]},
        geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1), box(5, 0, 6, 1)], crs=4326,
    )
    feats = wd.province_line_features(provs)
    assert len(feats) == 1
    line = LineString(feats[0]["geometry"]["coordinates"])
    assert line.length == pytest.approx(1.0, abs=0.01)
    assert line.centroid.x == pytest.approx(1.0, abs=1e-3)


def test_province_labels_strip_suffix():
    provs = gpd.GeoDataFrame(
        {"zh": ["新疆维吾尔自治区", "四川省", "上海市", "香港特别行政区"], "en": list("abcd"),
         "lon": [85, 103, 121, 114], "lat": [41, 30, 31, 22]},
        geometry=[Point(0, 0)] * 4, crs=4326,
    )
    names = [f["properties"]["zh"] for f in wd.province_label_features(provs)]
    assert names == ["新疆", "四川", "香港"]  # municipalities are named by their city label


def test_label_features_from_yaml(tmp_path):
    p = tmp_path / "labels.yaml"
    p.write_text(
        "features:\n"
        "  - {zh: 青藏高原, en: Tibetan Plateau, kind: plateau, rank: 1, lon: 88.5, lat: 33.2}\n"
        "  - {zh: 珠穆朗玛峰, en: Everest, kind: peak, rank: 1, lon: 86.925, lat: 27.988, elevation: 8848.86}\n"
        "  - {zh: 北京, en: Beijing, kind: city, rank: 1, lon: 116.39, lat: 39.9}\n"
        "  - {zh: 阳朔, en: Yangshuo, kind: city, rank: 3, lon: 110.49, lat: 24.78}\n"
        "  - {zh: 台湾岛, en: Taiwan Island, kind: island, rank: 1, lon: 121.0, lat: 23.8, marker: false}\n"
        "  - {zh: 西沙群岛, en: Xisha Islands, kind: island, rank: 2, lon: 112.3, lat: 16.5}\n"
        "  - {zh: 怒江, en: Nu, kind: river, rank: 2, lon: 98.85, lat: 26.3, points: [[98.8, 27.8]]}\n"
    )
    places = [wd._feature(Point(116.4, 39.9), {"zh": "北京"})]
    feats = wd.label_features(p, places=places)
    by = {f["properties"]["zh"]: f for f in feats}
    assert "北京" not in by  # duplicate of a Natural Earth place
    assert by["阳朔"]["tippecanoe"]["minzoom"] == wd.LABEL_MINZOOM[3]
    assert by["青藏高原"]["properties"]["zh_sp"] == wd.spaced("青藏高原")
    assert "zh_sp" not in by["珠穆朗玛峰"]["properties"]
    assert by["珠穆朗玛峰"]["properties"]["elevation"] == pytest.approx(8848.86)
    assert by["珠穆朗玛峰"]["properties"]["marker"] is True
    assert by["台湾岛"]["properties"]["marker"] is False
    assert by["西沙群岛"]["tippecanoe"]["minzoom"] == 2  # island names reach the 南海诸岛 inset zoom
    nu = [f for f in feats if f["properties"]["zh"] == "怒江"]
    assert len(nu) == 2 and nu[1]["properties"]["extra"] is True
    assert list(nu[1]["geometry"]["coordinates"]) == [98.8, 27.8]
    assert nu[1]["tippecanoe"]["minzoom"] == max(wd.LABEL_MINZOOM[2], wd.EXTRA_POINT_MINZOOM)


def test_label_features_missing_yaml(tmp_path):
    assert wd.label_features(tmp_path / "nope.yaml") == []


# ---------------------------------------------------------------- tippecanoe + cities (offline)

def _is_pmtiles(p) -> bool:
    return p.exists() and p.read_bytes()[:7] == b"PMTiles"


def test_tippecanoe_available():
    import subprocess

    out = subprocess.run([str(wd.tippecanoe_bin()), "--version"], capture_output=True, text=True, check=False)
    assert "tippecanoe v2" in (out.stdout + out.stderr)


def test_run_tippecanoe(tmp_path):
    layers = {
        "place": [wd._feature(Point(116.4, 39.9), {"zh": "北京"}, 2)],
        "river": [wd._feature(LineString([(100, 30), (110, 31)]), {"scalerank": 1}, 0)],
        "empty": [],
    }
    out = wd.run_tippecanoe(layers, tmp_path / "t.pmtiles", ["-Z0", "-z4"])
    assert _is_pmtiles(out)


def test_city_tiles_and_index(tmp_path, monkeypatch):
    out_dir, dest = tmp_path / "out", tmp_path / "public"
    city = out_dir / "testcity"
    city.mkdir(parents=True)
    gdf = gpd.GeoDataFrame(
        {"height": [12.0, 80.5], "base_z": [200.0, 210.0], "height_source": ["gba", "overture_height"]},
        geometry=[box(106.57, 29.56, 106.5705, 29.5605), box(106.571, 29.561, 106.5716, 29.5615)], crs=4326,
    )
    gdf.to_parquet(city / "buildings.geoparquet")
    (city / "city.json").write_text(json.dumps({"name": "testcity"}))
    p = wd.build_city_tiles("testcity", out_dir, dest)
    assert p is not None and _is_pmtiles(p)
    assert wd.copy_city("testcity", out_dir, dest) == {"glb": False, "json": True}
    assert wd.build_city_tiles("missing", out_dir, dest) is None

    monkeypatch.setattr(wd, "city_names", lambda: {
        "testcity": {"title": {"zh": "测", "en": "Test"}, "bbox": [106.5, 29.5, 106.6, 29.6],
                     "landmarks": [{"name": "Raffles City Chongqing", "lon": 106.584, "lat": 29.5687}]},
        "other": {"bbox": [0, 0, 1, 1]},
    })
    idx = {c["name"]: c for c in wd.cities_index(dest)}
    assert idx["testcity"]["buildings"] and idx["testcity"]["json"] and not idx["testcity"]["glb"]
    assert idx["testcity"]["center"] == [106.55, 29.55]
    assert not (idx["other"]["buildings"] or idx["other"]["glb"] or idx["other"]["json"])
    if wd.landmark_names():
        assert idx["testcity"]["landmarks"][0]["zh"] == "重庆来福士"



# ---------------------------------------------------------------- gallery (offline)

def test_gallery_entries_use_cli_stems(monkeypatch):
    monkeypatch.setattr(wd, "city_names", lambda: {"hk": {"title": {"zh": "港", "en": "HK"}}})
    stems = {e["stem"]: e for e in wd.gallery_entries()}
    assert "china" in stems and stems["china"]["kind"] == "scene"
    assert stems["city-hk"]["kind"] == "city" and stems["city-hk"]["name"] == "hk"


def test_build_gallery(tmp_path, monkeypatch):
    from PIL import Image

    src, dest = tmp_path / "gallery", tmp_path / "public" / "gallery"
    src.mkdir()
    scenes = tmp_path / "scenes.yaml"
    scenes.write_text("scenes:\n  a: {title: {zh: 甲, en: A}}\n  b: {}\n  c: {}\n")
    monkeypatch.setattr(wd, "city_names", lambda: {"x": {}})
    Image.new("RGB", (2560, 1440), (200, 100, 50)).save(src / "a.jpg")          # downsized
    Image.new("RGB", (800, 450), (0, 0, 0)).save(src / "city-x_small.jpg")      # copied as is
    Image.new("RGB", (900, 500), (9, 9, 9)).save(src / "city-x.jpg")
    dest.mkdir(parents=True)
    (dest / "c_small.jpg").write_bytes(b"stale")                                 # render gone -> removed

    idx = json.loads(wd.build_gallery(src, dest, scenes).read_text())
    by = {e["stem"]: e for e in idx}
    assert set(by) == {"a", "city-x"}
    assert (by["a"]["width"], by["a"]["height"]) == (wd.GALLERY_WIDTH, 900)
    assert by["a"]["src"] == "gallery/a_small.jpg" and by["a"]["title"] == {"zh": "甲", "en": "A"}
    assert by["a"]["full"].endswith("/gallery/a.jpg")
    assert by["a"]["large"] == f"gallery/a_{wd.GALLERY_LARGE}.jpg"
    with Image.open(dest / f"a_{wd.GALLERY_LARGE}.jpg") as im:
        assert im.width == wd.GALLERY_LARGE
    with Image.open(dest / f"city-x_{wd.GALLERY_LARGE}.jpg") as im:
        assert im.width == 900  # never upscaled
    assert (by["city-x"]["width"], by["city-x"]["kind"]) == (800, "city")
    assert not (dest / "c_small.jpg").exists() and not (dest / "b_small.jpg").exists()
    with Image.open(dest / "a_small.jpg") as im:
        assert im.format == "JPEG" and im.width == wd.GALLERY_WIDTH


def test_build_gallery_empty(tmp_path):
    p = wd.build_gallery(tmp_path / "none", tmp_path / "dest")
    assert json.loads(p.read_text()) == []

# ---------------------------------------------------------------- Natural Earth layers (network on first run)

@pytest.mark.network
def test_china_layers_from_natural_earth():
    layers = wd.china_layers()
    assert set(layers) >= {"boundary", "scs_dash", "province", "river", "lake", "place", "label"}
    assert sum(f["properties"]["china"] for f in layers["boundary"]) >= 5
    assert len(layers["scs_dash"]) == 9
    assert len(layers["province"]) >= 30
    beijing = [f for f in layers["place"] if f["properties"]["zh"] == "北京"]
    assert beijing and beijing[0]["properties"]["capital"] == "country"
    taipei = [f for f in layers["place"] if f["properties"]["zh"] == "台北"]
    assert taipei and taipei[0]["properties"]["capital"] == "province"
    # cn worldview: places inside China's polygon are Chinese, and foreign admin-1 seats there are not capitals
    import shapely

    cn = ne.china("cn")
    for f in layers["place"]:
        if shapely.contains_xy(cn, *f["geometry"]["coordinates"]):
            assert f["properties"]["china"], f["properties"]["en"]
    itanagar = [f for f in layers["place"] if f["properties"]["en"] == "Itanagar"]
    assert not itanagar or (itanagar[0]["properties"]["china"] and itanagar[0]["properties"]["capital"] == "")
    assert all(-1 <= f["geometry"]["coordinates"][1] <= 61 for f in layers["place"])
    if wd.CONFIG.joinpath("labels.yaml").exists():
        assert any(f["properties"]["kind"] == "range" for f in layers["label"])
    for name, feats in layers.items():
        for f in feats:
            assert 0 <= f.get("tippecanoe", {}).get("minzoom", 0) <= 10, name
            assert not any(isinstance(v, float) and math.isnan(v) for v in f["properties"].values())
