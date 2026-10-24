"""Tests for the render/label track: camera + sun math (blender/scenegeom.py), label selection
and projection, collision placement, compositing, and tiny end-to-end Blender renders."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image

from chinamaps import annotate as A
from chinamaps.net import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "blender"))
import scenegeom as G

BLENDER_PY = REPO_ROOT / ".venv-blender" / "bin" / "python"
needs_blender = pytest.mark.skipif(not BLENDER_PY.exists(), reason="no .venv-blender")


@pytest.fixture(autouse=True)
def offline_fonts(monkeypatch):
    """Never download fonts here (a cold cache would retry gstatic for minutes): use a cached copy, else
    the system CJK fallback, else skip."""
    from chinamaps import fonts

    def local(key: str) -> Path:
        cached = fonts.FONT_DIR / fonts.FONTS[key][0]
        if cached.exists() and cached.stat().st_size > 0:
            return cached
        try:
            return fonts.fallback_font()
        except FileNotFoundError:
            pytest.skip("no CJK font available offline")

    monkeypatch.setattr(fonts, "font_path", local)
    A.font.cache_clear()
    yield
    A.font.cache_clear()


# --------------------------------------------------------------------------- sun / camera math

@pytest.mark.parametrize("az,el,expect", [
    (0, 0, (0, 1, 0)), (90, 0, (1, 0, 0)), (180, 0, (0, -1, 0)), (270, 0, (-1, 0, 0)), (123, 90, (0, 0, 1)),
])
def test_compass_vector(az, el, expect):
    assert np.allclose(G.compass_vector(az, el), expect, atol=1e-12)


def test_sun_from_wsw_lights_west_facing_slopes():
    s = G.compass_vector(255, 20)  # direction toward the sun
    assert s[0] < 0 and s[1] < 0 and s[2] > 0
    west_slope_normal = np.array([-0.5, 0, 0.866])
    east_slope_normal = np.array([0.5, 0, 0.866])
    assert west_slope_normal @ s > east_slope_normal @ s


def test_look_basis_orthonormal_and_upright():
    r, u, f = G.look_basis(-G.compass_vector(150, 22))
    for v in (r, u, f):
        assert math.isclose(np.linalg.norm(v), 1.0, rel_tol=1e-9)
    assert abs(r @ u) < 1e-9 and abs(r @ f) < 1e-9 and abs(u @ f) < 1e-9
    assert np.allclose(np.cross(r, u), -f)  # Blender camera: right x up = backwards (+Z local)
    assert u[2] > 0 and abs(r[2]) < 1e-9


def test_camera_from_south_has_east_on_the_right():
    r, _, _ = G.look_basis(-G.compass_vector(180, 30))
    assert r[0] > 0.99


def slab_points(w=100.0, h=80.0, top=5.0, base=-3.0) -> np.ndarray:
    return np.array([[x, y, z] for x in (-w / 2, w / 2) for y in (-h / 2, h / 2) for z in (base, top)])


@pytest.mark.parametrize("az,el,focal", [(150, 22, 40), (190, 26, 50), (105, 30, 50), (345, 28, 35)])
def test_fit_camera_frames_points_inside_margins(az, el, focal):
    pts = slab_points()
    margins = (0.04, 0.04, 0.05, 0.15)
    fit = G.fit_camera(pts, np.zeros(3), G.compass_vector(az, el), focal, 16 / 9, 1.0, margins)
    px = G.to_pixels(pts, fit["location"], fit["basis"], focal, 1600, 900, fit["shift_x"], fit["shift_y"])
    x, y = px[:, 0] / 1600, px[:, 1] / 900
    assert x.min() >= margins[0] - 1e-6 and x.max() <= 1 - margins[1] + 1e-6
    assert y.min() >= margins[3] - 1e-6 and y.max() <= 1 - margins[2] + 1e-6
    # tight on at least one axis, centred in the margin box on both
    assert (x.max() - x.min() > 1 - margins[0] - margins[1] - 1e-3) or (y.max() - y.min() > 1 - margins[2] - margins[3] - 1e-3)
    assert math.isclose((x.min() + x.max()) / 2, 0.5 + (margins[0] - margins[1]) / 2, abs_tol=1e-3)
    assert math.isclose((y.min() + y.max()) / 2, 0.5 + (margins[3] - margins[2]) / 2, abs_tol=1e-3)
    assert fit["near"] > 0 and fit["far"] > fit["near"]
    # the view direction keeps the configured depression angle exactly (lens shift, no tilt)
    assert math.isclose(math.degrees(math.asin(-fit["basis"][2][2])), el, abs_tol=1e-9)


def test_fit_camera_distance_factor_scales_distance():
    pts = slab_points()
    a = G.fit_camera(pts, np.zeros(3), G.compass_vector(150, 22), 40, 16 / 9, 1.0)
    b = G.fit_camera(pts, np.zeros(3), G.compass_vector(150, 22), 40, 16 / 9, 1.2)
    assert math.isclose(b["distance"], 1.2 * a["distance"], rel_tol=1e-9)


def test_to_pixels_target_at_centre_without_shift():
    d = G.compass_vector(30, 40)
    basis = G.look_basis(-d)
    px = G.to_pixels(np.zeros((1, 3)), 10 * d, basis, 50, 1920, 1080)
    assert np.allclose(px[0, :2], (960, 540))
    assert math.isclose(px[0, 2], 10.0)


def test_half_tans_auto_sensor_fit():
    tx, ty = G.half_tans(36, 16 / 9)
    assert math.isclose(tx, 0.5) and math.isclose(ty, 0.5 * 9 / 16)
    tx, ty = G.half_tans(36, 0.5)  # portrait: sensor width applies to the height
    assert math.isclose(ty, 0.5) and math.isclose(tx, 0.25)


# --------------------------------------------------------------------------- mesh helpers

def test_block_mean_and_decimation():
    a = np.arange(7 * 9, dtype=np.float32).reshape(7, 9)
    m = G.block_mean(a, 2)
    assert m.shape == (3, 4)
    assert m[0, 0] == pytest.approx(a[:2, :2].mean())
    assert G.decimation_factor((2469, 2214), 2048) == 2
    assert G.decimation_factor((1000, 800), 2048) == 1
    assert max(np.ceil(np.array((6273, 8592)) / G.decimation_factor((6273, 8592), 3000))) <= 3000


def test_relief_smoothing_and_sharpening():
    rng = np.random.default_rng(1)
    z = rng.normal(0, 1, (60, 80)).astype(np.float32)
    assert G.gaussian_blur(z, 1.5).std() < 0.5 * z.std()           # needles averaged out
    np.testing.assert_allclose(G.gaussian_blur(np.full((9, 9), 7.0), 2.0), 7.0, atol=1e-5)
    tower = np.zeros((41, 41), np.float32)
    tower[18:23, 18:23] = 100.0
    sharp = G.shape_relief(tower, sharpen={"sigma": 3, "amount": 0.8})
    assert sharp.max() > tower.max()                              # unsharp mask steepens the tower
    assert G.shape_relief(tower).tolist() == tower.tolist()        # no-op without settings


def test_grid_faces_point_up():
    h, w = 3, 4
    faces = G.grid_faces(h, w)
    assert faces.shape == ((h - 1) * (w - 1), 4)
    ys, xs = np.mgrid[0:h, 0:w]
    verts = np.column_stack([xs.ravel(), -ys.ravel(), np.zeros(h * w)])  # row 0 = north (+y)
    a, b, c = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    assert np.all(np.cross(b - a, c - b)[:, 2] > 0)


def test_bilinear():
    g = np.add.outer(np.arange(4.0) * 10, np.arange(5.0))  # value = 10*row + col
    assert G.bilinear(g, np.array([1.5]), np.array([2.25]))[0] == pytest.approx(24.0)
    assert G.bilinear(g, np.array([-3.0]), np.array([9.0]))[0] == pytest.approx(30.0)  # clamped


def test_frame_region_rect_and_polygon():
    fx, fy = np.array([0.1, 0.5, 0.9, 0.5]), np.array([0.5, 0.5, 0.5, 0.95])
    assert G.in_frame_region(fx, fy, [0.2, 0.2, 0.8, 0.8]).tolist() == [False, True, False, False]
    tri = [[0.0, 0.0], [1.0, 0.0], [0.5, 1.0]]  # apex at the top centre
    assert G.in_frame_region(fx, fy, tri).tolist() == [False, True, False, True]


def test_grade_identity_endpoints_and_grey():
    rgb = np.array([[0.0, 0.0, 0.0], [255.0, 255.0, 255.0], [128.0, 128.0, 128.0], [200.0, 80.0, 40.0]])
    assert np.allclose(G.grade(rgb), rgb, atol=1e-3)
    out = G.grade(rgb, saturation=1.3, contrast=0.3)
    assert np.allclose(out[:2], rgb[:2], atol=1e-3)            # black and white stay put
    assert np.allclose(out[2], out[2, 0], atol=1e-3)           # grey stays neutral
    assert np.ptp(out[3]) > np.ptp(rgb[3])                     # colours get more saturated
    assert G.grade(rgb[2:3], contrast=0.3)[0, 0] < 128.5       # mid-grey barely moves


# --------------------------------------------------------------------------- label selection

def guilin_meta() -> dict:
    from pyproj import Transformer

    crs = "EPSG:32649"
    tr = Transformer.from_crs(4326, crs, always_xy=True)
    x0, y0 = tr.transform(110.30, 24.65)
    x1, y1 = tr.transform(110.62, 24.95)
    return {"name": "t", "crs": crs, "bounds": [110.30, 24.65, 110.62, 24.95], "bounds_proj": [x0, y0, x1, y1]}


def test_select_labels_bounds_rank_and_alternates():
    meta = guilin_meta()
    feats = [
        {"zh": "阳朔", "en": "Yangshuo", "kind": "city", "rank": 3, "lon": 110.49, "lat": 24.78},
        {"zh": "桂林", "en": "Guilin", "kind": "city", "rank": 2, "lon": 110.29, "lat": 25.27},  # outside
        {"zh": "山", "en": "R", "kind": "range", "rank": 1, "lon": 100.0, "lat": 30.0,
         "points": [[110.40, 24.80], [110.55, 24.70]]},
    ]
    out = A.select_labels(meta, feats, max_rank=3, snap_rivers=False)
    assert [o["zh"] for o in out] == ["阳朔", "山"]
    assert out[1]["lon"] == pytest.approx(110.40, abs=1e-4) and len(out[1]["alts"]) == 1
    assert [o["zh"] for o in A.select_labels(meta, feats, max_rank=2, snap_rivers=False)] == ["山"]


def test_sample_dem_bilinear_and_peak():
    bounds = [0.0, 0.0, 100.0, 50.0]  # 10 m pixels, 5 x 10 grid
    yy, xx = np.mgrid[0:5, 0:10]
    dem = (xx * 2.0 + yy * 0.0).astype(np.float32)
    assert A.sample_dem(dem, bounds, 55.0, 25.0) == pytest.approx(10.0)  # pixel centre col 5
    dem2 = dem.copy()
    dem2[2, 6] = 999
    assert A.sample_dem(dem2, bounds, 55.0, 25.0, peak=True) == 999


def test_river_snapping_and_alternates():
    from shapely.geometry import LineString

    river = LineString([(0, 0), (1000, 0), (1000, 1000)])
    assert A.snap_to_river(river, (400, 50)) == pytest.approx((400.0, 0.0))
    alts = A.along_river(river, (400, 0), n=4)  # samples every 400 m; those within 200 m are skipped
    assert alts == [[1000.0, 0.0], [1000.0, 400.0], [1000.0, 800.0]]


def test_labels_yaml_is_well_formed():
    cfg = A.load_config()
    kinds = {"peak", "range", "plateau", "basin", "plain", "desert", "river", "lake", "sea", "island", "city",
             "region"}
    feats = cfg["features"]
    assert 80 <= len(feats) <= 160
    for f in feats:
        assert {"zh", "en", "kind", "lon", "lat", "rank"} <= set(f), f
        assert f["kind"] in kinds and f["rank"] in (1, 2, 3), f
        assert 60 <= f["lon"] <= 145 and 0 <= f["lat"] <= 56, f
    names = {f["zh"] for f in feats}
    for must in ("喜马拉雅山脉", "珠穆朗玛峰", "青藏高原", "塔里木盆地", "长江", "黄河", "南海诸岛", "黄岩岛",
                 "曾母暗沙", "台湾岛", "北京", "香港", "漓江", "三江并流", "雅鲁藏布大峡谷"):
        assert must in names, must
    assert A.scene_settings(cfg, "china")["max_rank"] == 2
    assert A.scene_settings(cfg, "nope") == {"max_rank": 3, "scale": 1.0, "peak_elevations": True}
    assert A.scene_settings(cfg, "china", small=True)["max_rank"] == 1


def test_prepare_labels_writes_projected_json(tmp_path):
    meta = guilin_meta()
    x0, y0, x1, y1 = meta["bounds_proj"]
    (tmp_path / "scene.json").write_text(json.dumps(meta))
    np.save(tmp_path / "dem.npy", np.full((40, 30), 150.0, np.float32))
    labels_yaml = tmp_path / "labels.yaml"
    labels_yaml.write_text(yaml.safe_dump({"features": [
        {"zh": "阳朔", "en": "Yangshuo", "kind": "city", "rank": 3, "lon": 110.49, "lat": 24.78}]},
        allow_unicode=True))
    out = A.prepare_labels(tmp_path, labels_yaml)
    d = json.loads(out.read_text())
    (lab,) = d["labels"]
    assert x0 < lab["x"] < x1 and y0 < lab["y"] < y1
    assert lab["z"] == pytest.approx(150.0)


# --------------------------------------------------------------------------- placement

def test_place_labels_priority_and_fallback():
    big = A.Box(100, 100, 200, 140)
    reqs = [
        A.Request(1, (2,), [A.Box(110, 110, 190, 130), A.Box(110, 150, 190, 170)]),  # 1st collides
        A.Request(0, (1,), [big]),
        A.Request(2, (3,), [A.Box(-10, 0, 20, 10), A.Box(120, 120, 130, 125)]),  # off-frame, collides
    ]
    chosen = A.place_labels(reqs, 400, 300, pad=0)
    assert chosen == {0: 0, 1: 1}


def test_place_labels_respects_blocked_and_markers():
    title = A.Box(0, 0, 100, 50)
    reqs = [A.Request(0, (1,), [A.Box(10, 10, 60, 30), A.Box(10, 60, 60, 80)], marker=A.Box(5, 85, 9, 89)),
            A.Request(1, (2,), [A.Box(0, 82, 20, 95)])]
    chosen = A.place_labels(reqs, 400, 300, blocked=[title], pad=0, margin=0)
    assert chosen == {0: 1}  # second label hits the first label's marker


def test_place_labels_repair_moves_a_city_name_aside():
    # a city dot with its name to the right, and a basin name that only fits where that name is
    city = A.Request(0, (0,), [A.Box(110, 95, 160, 105), A.Box(40, 95, 90, 105)], marker=A.Box(96, 96, 104, 104))
    basin = A.Request(1, (1,), [A.Box(108, 90, 200, 110)])
    assert A.place_labels([city, basin], 400, 300, pad=0, margin=0, repair=False) == {0: 0}
    assert A.place_labels([city, basin], 400, 300, pad=0, margin=0) == {0: 1, 1: 0}  # the name hops left
    blocked = A.Box(30, 90, 95, 110)  # nowhere to hop: the basin stays unplaced
    assert A.place_labels([city, basin], 400, 300, blocked=[blocked], pad=0, margin=0) == {0: 0}


def test_candidate_boxes_prefer_above_for_peaks():
    peak = A.candidate_boxes({"kind": "peak"}, 40, 20, 100, 100, 3, 2, 12)
    city = A.candidate_boxes({"kind": "city"}, 40, 20, 100, 100, 3, 2, 12)
    assert peak[0].y1 <= 100 - 3 and peak[0].x0 < 100 < peak[0].x1
    assert city[0].x0 >= 100 + 3
    area = A.candidate_boxes({"kind": "plateau"}, 40, 20, 100, 100, 0, 2, 12)
    assert (area[0].x0 + area[0].x1) / 2 == 100 and (area[0].y0 + area[0].y1) / 2 == 100


def test_visibility_rules():
    assert not A.visible({"kind": "city", "in_frame": False})
    assert not A.visible({"kind": "peak", "in_frame": True, "occluded": True, "visible_frac": 0.0})
    assert A.visible({"kind": "city", "in_frame": True, "occluded": True, "visible_frac": 0.33})
    assert A.visible({"kind": "range", "in_frame": True, "occluded": True, "visible_frac": 0.0})
    assert not A.visible({"kind": "river", "in_frame": True, "occluded": True})


def test_priority_marked_points_first_then_rank():
    city = A.priority({"kind": "city", "rank": 1})
    plateau = A.priority({"kind": "plateau", "rank": 1})
    river = A.priority({"kind": "river", "rank": 1})
    rank2_city = A.priority({"kind": "city", "rank": 2})
    rank2_basin = A.priority({"kind": "basin", "rank": 2})
    rank3_city = A.priority({"kind": "city", "rank": 3})
    island = A.priority({"kind": "island", "rank": 1, "marker": False})  # unmarked: flexible like an area
    # city dots (4 fixed boxes) claim space before area names (11 offsets each) can flow around them
    assert city < rank2_city < plateau < river < rank2_basin < rank3_city
    assert plateau[0] == island[0] == 1


def test_water_names_lean_left_for_chinese():
    """左斜宋体: the top of a Chinese water name sits further LEFT than its bottom."""
    f = A.font("serif", 60)
    block = A.Block("江江江", "", f, f, 0.0, 0.0, italic=True)
    m = np.asarray(A.render_block_mask(block, "left")) > 128
    ys, xs = np.nonzero(m)
    top, bottom = xs[ys < np.percentile(ys, 20)], xs[ys > np.percentile(ys, 80)]
    assert top.min() < bottom.min() and top.max() < bottom.max()
    assert m[:, 0].sum() == 0 and m[:, -1].sum() == 0  # nothing clipped at the mask edges


def test_north_angle():
    assert A.north_angle({"azimuth": 180, "elevation": 30}) == pytest.approx(0, abs=1e-6)    # looking north
    assert abs(A.north_angle({"azimuth": 0, "elevation": 30})) == pytest.approx(180, abs=1e-6)  # looking south
    assert A.north_angle({"azimuth": 270, "elevation": 30}) == pytest.approx(-90, abs=1e-6)  # looking east
    assert 0 < A.north_angle({"azimuth": 165, "elevation": 35}) < 30
    assert A.north_angle({}) is None


def test_credits_carry_exaggeration_and_osm():
    assert "×18" in A.credits({"exaggeration": 18}, {})
    city = A.credits({"kind": "city"}, {"epsg": 32650, "sources": {"sentinel2": {"year": 2026}}})
    assert "OpenStreetMap" in city and "Shi et al. 2023" in city and "Sentinel data 2026" in city


def test_small_annotation_rescales_positions(tmp_path):
    img = np.full((400, 800, 3), (70, 110, 70), np.uint8)
    Image.fromarray(img).save(tmp_path / "r.png")
    labels = [{"zh": "拉萨", "en": "Lhasa", "kind": "city", "rank": 1, "px": 600, "py": 300, "in_frame": True},
              {"zh": "日喀则", "en": "Shigatse", "kind": "city", "rank": 3, "px": 200, "py": 300, "in_frame": True}]
    (tmp_path / "l.json").write_text(json.dumps({"scene": "x", "labels": labels, "title": {"zh": "测", "en": "T"}}))
    cfg = tmp_path / "labels.yaml"
    cfg.write_text("scenes:\n  x: {small: {max_rank: 1}}\nfeatures: []\n")
    out = A.annotate(tmp_path / "r.png", tmp_path / "l.json", None, tmp_path / "s.png", labels_yaml=cfg,
                     width=400, small=True)
    a = np.asarray(Image.open(out)).astype(int)
    assert a.shape[:2] == (200, 400)
    changed = np.abs(a - np.asarray(Image.fromarray(img).resize((400, 200))).astype(int)).sum(-1) > 40
    assert changed[140:160, 295:305].any()          # Lhasa dot at half the coordinates
    assert not changed[140:160, 95:105].any()       # rank-3 label dropped by small.max_rank


# --------------------------------------------------------------------------- compositing

def test_annotate_draws_labels_title_and_credits(tmp_path):
    w, h = 800, 450
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = (220, 228, 236)
    img[150:, :] = (70, 110, 70)
    Image.fromarray(img).save(tmp_path / "r.png")
    labels = [
        {"zh": "青藏高原", "en": "Tibetan Plateau", "kind": "plateau", "rank": 1, "px": 300, "py": 250,
         "in_frame": True, "occluded": False},
        {"zh": "拉萨", "en": "Lhasa", "kind": "city", "rank": 1, "px": 500, "py": 300, "in_frame": True,
         "occluded": False},
        {"zh": "藏", "en": "Hidden", "kind": "peak", "rank": 1, "px": 600, "py": 300, "in_frame": True,
         "occluded": True, "visible_frac": 0.0},
    ]
    (tmp_path / "l.json").write_text(json.dumps({"scene": "x", "width": w, "height": h, "labels": labels,
                                                 "title": {"zh": "测试", "en": "Test"}}))
    out = A.annotate(tmp_path / "r.png", tmp_path / "l.json", None, tmp_path / "o.png")
    a = np.asarray(Image.open(out)).astype(int)
    b = img.astype(int)
    changed = np.abs(a - b).sum(-1) > 30
    assert changed[230:275, 220:380].any()            # plateau label
    assert changed[285:315, 495:560].any()            # city marker + name
    assert not changed[280:320, 590:640].any()        # occluded peak dropped
    assert changed[10:60, 10:120].any()               # title block
    assert changed[h - 25:, w - 300:].any()           # credits


def test_attribution_gets_a_plate_on_mixed_backgrounds():
    from PIL import Image

    im = Image.new("RGBA", (800, 400), (245, 245, 240, 255))
    im.paste((30, 30, 30, 255), (0, 0, 680, 400))  # dark slab on the left, pale sky on the right
    plain = Image.new("RGBA", (800, 400), (245, 245, 240, 255))
    box = A.draw_attribution(im, "Copernicus DEM, GEBCO, Natural Earth")
    A.draw_attribution(plain, "Copernicus DEM, GEBCO, Natural Earth")
    x1, y0, y1 = int(box.x1), int(box.y0), int(box.y1)
    strip = (x1 - 6, y0, x1 - 1, y1)  # light side, mostly between glyphs
    assert np.asarray(im.convert("L").crop(strip)).mean() < np.asarray(plain.convert("L").crop(strip)).mean() - 20


def test_gallery_jpeg_downsizes(tmp_path):
    Image.new("RGB", (3000, 1000), (10, 20, 30)).save(tmp_path / "a.png")
    out = A.gallery_jpeg(tmp_path / "a.png", tmp_path / "g" / "a.jpg", max_width=1200, small_width=600)
    assert Image.open(out).size == (1200, 400)
    assert Image.open(tmp_path / "g" / "a_small.jpg").size == (600, 200)


def test_gallery_jpeg_size_cap(tmp_path):
    rng = np.random.default_rng(0)
    Image.fromarray(rng.integers(0, 255, (600, 900, 3), dtype=np.uint8)).save(tmp_path / "n.png")
    big = A.gallery_jpeg(tmp_path / "n.png", tmp_path / "n.jpg", small_width=None, max_bytes=None)
    capped = A.gallery_jpeg(tmp_path / "n.png", tmp_path / "c.jpg", small_width=None,
                            max_bytes=big.stat().st_size // 2)
    assert capped.stat().st_size < big.stat().st_size
    assert not (tmp_path / "n_small.jpg").exists()


# --------------------------------------------------------------------------- Blender end to end

def tiny_scene(d: Path) -> None:
    from pyproj import Transformer

    d.mkdir(parents=True)
    h, w, res = 60, 80, 30.0
    yy, xx = np.mgrid[0:h, 0:w]
    dem = (300 + 60 * np.exp(-((xx - 40) ** 2 + (yy - 30) ** 2) / 200)).astype(np.float32)
    np.save(d / "dem.npy", dem)
    tex = np.zeros((2 * h, 2 * w, 3), np.uint8)
    tex[:] = (90, 150, 80)
    tex[:20, :20] = (230, 30, 30)
    Image.fromarray(tex).save(d / "texture.png")
    Image.fromarray(np.full((h, w), 255, np.uint8)).save(d / "mask.png")
    cx, cy = 445000.0, 2742000.0
    b = [cx - w * res / 2, cy - h * res / 2, cx + w * res / 2, cy + h * res / 2]
    lon, lat = Transformer.from_crs("EPSG:32649", 4326, always_xy=True).transform(cx, cy)
    meta = {"name": "tiny", "crs": "EPSG:32649", "bounds_proj": b, "dem_shape": [h, w],
            "texture_shape": [2 * h, 2 * w], "exaggeration": 2, "haze": 0.2, "mesh_max": 2048,
            "camera": {"azimuth": 150, "elevation": 25, "focal_mm": 40, "distance": 1.0},
            "sun": {"azimuth": 255, "elevation": 20, "strength": 4.0, "angle": 1.0},
            "title": {"zh": "小", "en": "Tiny"}, "draft": True}
    (d / "scene.json").write_text(json.dumps(meta))
    (d / "labels_proj.json").write_text(json.dumps({"labels": [
        {"zh": "峰", "en": "Peak", "kind": "peak", "rank": 1, "lon": lon, "lat": lat, "x": cx, "y": cy, "z": 500}]}))


@needs_blender
def test_render_scene_end_to_end(tmp_path):
    tiny_scene(tmp_path / "tiny")
    out = tmp_path / "r" / "tiny_draft.png"
    subprocess.run([str(BLENDER_PY), str(REPO_ROOT / "blender" / "render_scene.py"), str(tmp_path / "tiny"),
                    "--draft", "--width", "192", "--height", "108", "--samples", "4", "--threads", "2",
                    "--out", str(out)], check=True, capture_output=True, timeout=600)
    img = np.asarray(Image.open(out))
    assert img.shape == (108, 192, 3)
    alpha = np.asarray(Image.open(out.with_name("tiny_draft_rgba.png")))[..., 3]
    assert 0.15 < (alpha > 128).mean() < 0.95  # the slab fills part of the frame, sky the rest
    info = json.loads(out.with_name("tiny_draft_labels.json").read_text())
    (lab,) = info["labels"]
    assert lab["in_frame"] and not lab["occluded"]
    assert 192 * 0.25 < lab["px"] < 192 * 0.75 and 108 * 0.2 < lab["py"] < 108 * 0.8
    assert info["render"]["samples"] == 4
    A.annotate(out, out.with_name("tiny_draft_labels.json"), tmp_path / "tiny" / "scene.json",
               tmp_path / "r" / "lab.png")


@needs_blender
def test_render_city_end_to_end(tmp_path):
    import trimesh

    d = tmp_path / "c"
    d.mkdir()
    n = 11
    xs, ys = np.meshgrid(np.linspace(0, 500, n), np.linspace(0, 400, n))
    verts = np.column_stack([xs.ravel(), np.full(n * n, 50.0), -ys.ravel()])
    i = np.arange(n * n).reshape(n, n)
    a, b, c, e = i[:-1, :-1].ravel(), i[:-1, 1:].ravel(), i[1:, 1:].ravel(), i[1:, :-1].ravel()
    faces = np.vstack([np.column_stack([a, c, b]), np.column_stack([a, e, c])])
    uv = np.column_stack([xs.ravel() / 500, ys.ravel() / 400])
    ground = trimesh.Trimesh(verts, faces, process=False, visual=trimesh.visual.TextureVisuals(
        uv=uv, image=Image.new("RGB", (64, 64), (100, 140, 90))))
    tower = trimesh.creation.box(extents=[40, 200, 40])
    tower.apply_translation([250, 150, -200])
    sc = trimesh.Scene()
    sc.add_geometry(ground, node_name="terrain", geom_name="terrain")
    sc.add_geometry(tower, node_name="buildings", geom_name="buildings")
    sc.export(d / "city.glb")
    (d / "city.json").write_text(json.dumps({
        "name": "c", "title": {"zh": "城", "en": "C"}, "epsg": 32648,
        "landmarks": [{"name": "Tower", "local": [250, 50, -200], "height": 200, "top_z": 250}],
        "camera": {"azimuth": 225, "elevation": 30, "focal_mm": 35, "distance": 1.0},
        "sun": {"azimuth": 250, "elevation": 18, "strength": 4.0, "angle": 0.8}}))
    out = tmp_path / "r" / "city-c_draft.png"
    subprocess.run([str(BLENDER_PY), str(REPO_ROOT / "blender" / "render_city.py"), str(d), "--draft",
                    "--width", "192", "--height", "108", "--samples", "4", "--threads", "2", "--out", str(out)],
                   check=True, capture_output=True, timeout=600)
    info = json.loads(out.with_name("city-c_draft_labels.json").read_text())
    (lab,) = info["labels"]
    assert lab["kind"] == "landmark" and lab["in_frame"] and lab["en"] == "Tower"
    assert Image.open(out).size == (192, 108)


def test_cli_passes_output_path_to_blender():
    """Blender writes where cli._annotate reads, so CHINAMAPS_OUT is honoured end to end."""
    import argparse

    from chinamaps import cli

    a = argparse.Namespace(draft=True, width=640, height=None, samples=8, threads=None)
    args = cli._render_args(a, "china_draft")
    assert args[:2] == ["--out", str(cli.RENDER_DIR / "china_draft.png")]
    assert "--draft" in args and args[args.index("--width") + 1] == "640" and "--height" not in args
