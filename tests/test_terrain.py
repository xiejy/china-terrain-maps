"""Terrain track: shading maths, tile selection, frames, drainage and the scene contract."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from chinamaps import dem, shade, texture, tint
from chinamaps.sources import copernicus as cop
from chinamaps.sources import gebco


def plane(a: float, b: float, n: int = 16, res: float = 30.0) -> np.ndarray:
    """z = a * east + b * north on a north-up grid (row 0 = north)."""
    x = np.arange(n) * res
    y = (n - 1 - np.arange(n)) * res
    return (a * x[None, :] + b * y[:, None]).astype(np.float32)


# ---------------------------------------------------------------- shading


def test_horn_recovers_plane_gradient():
    p, q = shade.horn(plane(0.3, -0.2), 30.0)
    assert p.dtype == np.float32 and q.dtype == np.float32
    np.testing.assert_allclose(p[1:-1, 1:-1], 0.3, atol=1e-5)
    np.testing.assert_allclose(q[1:-1, 1:-1], -0.2, atol=1e-5)
    p2, _ = shade.horn(plane(0.3, -0.2), 30.0, zf=2.0)
    np.testing.assert_allclose(p2[1:-1, 1:-1], 0.6, atol=1e-5)


@pytest.mark.parametrize("a,b", [(0.4, 0.0), (0.0, 0.4), (0.3, 0.3), (-0.25, 0.1), (0.05, -0.6)])
def test_mdow_weights_match_gdal_formula(a, b):
    p, q = shade.horn(plane(a, b), 30.0)
    w = [x[5, 5] for x in shade.mdow_weights(p, q)]
    assert sum(w) == pytest.approx(1.0, abs=1e-6)
    # gdaldem: x = -dz/dx, y = dz/dy(north); weights are sin^2(aspect - az) with aspect = atan2(y, x)
    asp = math.atan2(b, -a)
    expect = [math.sin(asp - math.radians(az)) ** 2 / 2 for az in shade.MDOW_AZIMUTHS]
    np.testing.assert_allclose(w, expect, atol=1e-5)
    # and the polynomial form from gdaldem_lib.cpp
    x, y = -a, b
    r2 = x * x + y * y
    w225 = 0.5 * r2 - x * y
    poly = np.array([w225, x * x, r2 - w225, y * y]) / (2 * r2)
    np.testing.assert_allclose(w, poly, atol=1e-5)


def test_mdow_plane_and_flat():
    p, q = shade.horn(plane(0.3, 0.2), 30.0)
    hs = shade.mdow(p, q, 45.0)
    w = shade.mdow_weights(p, q)
    manual = sum(wi * shade.hillshade(p, q, az, 45.0) for wi, az in zip(w, shade.MDOW_AZIMUTHS))
    np.testing.assert_allclose(hs, manual, atol=1e-6)
    assert hs.dtype == np.float32
    flat = np.zeros((8, 8), np.float32)
    pf, qf = shade.horn(flat, 30.0)
    np.testing.assert_allclose(shade.mdow(pf, qf, 45.0), math.sin(math.radians(45)), atol=1e-6)


def test_hillshade_faces_light():
    # a slope rising to the east faces west: lit by a west sun, dark under an east sun
    p, q = shade.horn(plane(1.0, 0.0), 30.0)
    assert shade.hillshade(p, q, 270, 30)[5, 5] > 0.8
    assert shade.hillshade(p, q, 90, 30)[5, 5] == 0.0
    assert shade.slope_deg(p, q)[5, 5] == pytest.approx(45.0, abs=1e-3)


def test_texture_shade_scaled():
    rng = np.random.default_rng(0)
    z = np.cumsum(np.cumsum(rng.normal(size=(128, 160)), 0), 1).astype(np.float32)
    t = shade.texture_shade(z, 0.7)
    assert t.shape == z.shape and t.dtype == np.float32
    assert t.min() >= -1 and t.max() <= 1 and np.isfinite(t).all()


def test_zfactor_scale():
    assert shade.default_zfactor(30) == pytest.approx(1.0)
    assert 3.0 <= shade.default_zfactor(500) <= 5.0


# ---------------------------------------------------------------- tiles


def test_tile_names():
    g30, g90 = cop.product("glo30"), cop.product("glo90")
    assert cop.tile_id(27, 86, g30) == "Copernicus_DSM_COG_10_N27_00_E086_00_DEM"
    assert cop.tile_id(-1, -71, g90) == "Copernicus_DSM_COG_30_S01_00_W071_00_DEM"
    assert cop.tile_url(27, 86, g30) == (
        "https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N27_00_E086_00_DEM/"
        "Copernicus_DSM_COG_10_N27_00_E086_00_DEM.tif")
    assert cop.tile_url(30, 100, g90).startswith("https://copernicus-dem-90m.s3.amazonaws.com/Copernicus_DSM_COG_30_")


def test_cells_in_bbox():
    assert cop.cells_in_bbox(86.45, 27.70, 87.30, 28.35) == [(27, 86), (27, 87), (28, 86), (28, 87)]
    assert cop.cells_in_bbox(110.3, 24.65, 110.62, 24.95) == [(24, 110)]
    assert cop.cells_in_bbox(-1.5, -0.5, 0.5, 0.5) == [(-1, -2), (-1, -1), (-1, 0), (0, -2), (0, -1), (0, 0)]


def test_choose_ppd_and_read_plan():
    g30, ovr2, g90 = cop.product("glo30"), cop.product("glo30-ovr2"), cop.product("glo90")
    assert cop.choose_ppd(g30, 111320 / 30) == 3600
    assert cop.choose_ppd(g30, 111320 / 60) == 1800
    assert cop.choose_ppd(ovr2, 111320 / 500) == 225  # overview 2 averaged 2x
    assert cop.choose_ppd(ovr2, 111320 / 30) == 450  # capped by the product
    assert cop.choose_ppd(g90, 111320 / 90) == 1200
    assert cop.read_plan(g30, 3600) == (-1, 1)
    assert cop.read_plan(g30, 1800) == (0, 1)
    assert cop.read_plan(g30, 225) == (2, 2)  # GDAL OVERVIEW_LEVEL is 0-based over overviews
    assert cop.read_plan(g90, 300) == (1, 1)
    assert gebco.choose_level(240) == (240, -1)
    assert gebco.choose_level(111) == (120, 0)
    assert gebco.choose_level(30) == (30, 2)


def test_mosaic_places_tiles(tmp_path):
    import rasterio
    from rasterio.transform import from_origin

    prod, ppd = cop.product("glo90"), 4
    paths = {}
    for lat, lon, v in [(27, 86, 1.0), (28, 87, 2.0)]:
        p = tmp_path / f"{lat}_{lon}.tif"
        half = 0.5 / prod.ppd
        with rasterio.open(p, "w", driver="GTiff", width=ppd, height=ppd, count=1, dtype="float32",
                           crs="EPSG:4326", transform=from_origin(lon - half, lat + 1 + half, 1 / ppd, 1 / ppd)) as d:
            d.write(np.full((ppd, ppd), v, np.float32), 1)
        paths[(lat, lon)] = p
    cells = cop.cells_in_bbox(86.2, 27.2, 87.8, 28.8)
    a, tr = cop.mosaic(paths, cells, ppd, prod)
    assert a.shape == (8, 8)
    assert (a[4:, :4] == 1).all() and (a[:4, 4:] == 2).all()  # (27,86) is south-west, (28,87) north-east
    assert np.isnan(a[:4, :4]).all() and np.isnan(a[4:, 4:]).all()  # no tile -> NaN (sea)
    assert tr.c == pytest.approx(86 - 0.5 / prod.ppd) and tr.f == pytest.approx(29 + 0.5 / prod.ppd)


# ---------------------------------------------------------------- frames


@pytest.mark.parametrize("name", ["china", "himalaya-everest", "hengduan-three-rivers",
                                  "sichuan-basin-tibet-edge", "guilin-karst"])
@pytest.mark.parametrize("draft", [False, True])
def test_frame_shapes_consistent(name, draft):
    from chinamaps.scene import load_config

    cfg = load_config(name)
    f = dem.make_frame(cfg, draft)
    H, W = f.dem_shape
    Ht, Wt = f.texture_shape
    assert (Ht, Wt) == (H * f.factor, W * f.factor)
    xmin, ymin, xmax, ymax = f.bounds
    assert xmax - xmin == pytest.approx(W * f.dem_res) and ymax - ymin == pytest.approx(Ht * f.texture_res)
    assert all(v % f.dem_res == 0 for v in f.bounds)
    assert f.dem_res == cfg["dem_res"] * (2 if draft else 1)
    # the frame covers the configured lon/lat bounds
    from shapely.geometry import box

    assert f.lonlat_polygon().buffer(1e-6).contains(box(*cfg["bounds"]))
    t = f.transform(f.texture_res)
    assert (t.c, t.f, t.a, t.e) == (xmin, ymax, f.texture_res, -f.texture_res)


def test_block_mean():
    a = np.arange(16, dtype=np.float32).reshape(4, 4)
    np.testing.assert_allclose(dem.block_mean(a, 2), [[2.5, 4.5], [10.5, 12.5]])
    assert dem.block_mean(a, 1) is not a


def test_drainage_valley():
    # a V-shaped valley draining south: everything should reach the outlet at the bottom middle
    n = 21
    x = np.abs(np.arange(n) - n // 2).astype(np.float32)
    y = np.arange(n)[::-1].astype(np.float32)
    z = 10 * x[None, :] + 0.5 * y[:, None] + 100
    parent, acc = dem.drainage(z)
    outlet = (n - 1) * n + n // 2
    assert parent[outlet] == -1
    assert acc[outlet] > 0.8 * n * n
    # flow never goes uphill on this depression-free surface
    zf = z.ravel()
    ok = parent >= 0
    assert (zf[parent[ok]] <= zf[ok] + 1e-6).all()


def test_drainage_fills_pit():
    z = np.full((9, 9), 50.0, np.float32)
    z[4, 4] = 10.0  # a pit in a plateau; one low exit on the east edge
    z[4, 8] = 0.0
    parent, acc = dem.drainage(z)
    i = 4 * 9 + 8
    assert parent[i] == -1 and acc[i] >= 70


# ---------------------------------------------------------------- tint and vectors


def test_ramp_hits_stops():
    elev, cols = tint.land_stops("warm_humid")
    out = tint.ramp(elev.astype(np.float32), elev, cols, gamma=0.7)
    np.testing.assert_allclose(out, cols, atol=1e-6)
    w = {c: np.full((2,), 1 / 3, np.float32) for c in tint.CLIMATES}
    col = tint.land_color(np.array([0.0, 8000.0], np.float32), w)
    assert col.shape == (2, 3) and (col >= 0).all() and (col <= 1).all()


def test_snow_cover():
    z = np.array([3000, 7000, 7000], np.float32)
    slope = np.array([10, 10, 70], np.float32)
    s = tint.snow_cover(z, slope, np.full(3, 28.0, np.float32))
    assert s[0] == 0 and s[1] == pytest.approx(1.0) and s[2] == 0


def test_snow_lower_on_north_faces():
    pal = tint.palettes({"snow": {"aspect_m": 300}})
    z = np.full(2, 5750.0 + 100, np.float32)        # just above the 28 N snowline
    s = tint.snow_cover(z, np.full(2, 10.0, np.float32), np.full(2, 28.0, np.float32), 0.0, pal,
                        np.array([1.0, -1.0], np.float32))
    assert s[0] > s[1] > 0                          # the north-facing cell holds more snow


def test_palette_override_merges_dicts_and_replaces_lists():
    base = tint.palettes()
    over = tint.palettes({"snow": {"humid_drop_m": 1100}, "land": {"shared_high": [[7000, 1, 2, 3], [0, 4, 5, 6]]}})
    assert over["snow"]["humid_drop_m"] == 1100 and over["snow"]["fade_m"] == base["snow"]["fade_m"]
    assert over["land"]["shared_high"] == [[7000, 1, 2, 3], [0, 4, 5, 6]]
    assert over["land"]["arid"] == base["land"]["arid"]
    assert base["snow"]["humid_drop_m"] != 1100     # the cached palette is untouched


def test_slope_tint_two_tone():
    cfg = {"steep": [48, 96, 58], "flat": [176, 192, 124], "deg": [6, 20]}
    col = np.full((3, 3), 0.5, np.float32)
    out = tint.slope_tint(col, np.array([0.0, 13.0, 40.0], np.float32), cfg)
    np.testing.assert_allclose(out[0], np.array(cfg["flat"]) / 255, atol=1e-6)
    np.testing.assert_allclose(out[2], np.array(cfg["steep"]) / 255, atol=1e-6)
    assert out[2, 1] < out[1, 1] < out[0, 1]


def test_render_vectors_seamless():
    line = np.array([[3.3, 2.1], [60.7, 37.9], [121.2, 9.4]])
    layer = texture.Layer("line", [line], (200, 0, 0), 2.5)
    whole = texture.render_vectors([layer], 0, 0, 128, 48)
    left = texture.render_vectors([layer], 0, 0, 64, 48)
    right = texture.render_vectors([layer], 64, 0, 64, 48)
    assert whole.shape == (48, 128, 4)
    np.testing.assert_allclose(np.concatenate([left, right], axis=1), whole, atol=2 / 255)
    assert whole[..., 3].max() > 0.9


def test_dashes_and_smooth_line():
    from shapely.geometry import LineString

    d = texture.dashes(LineString([(0, 0), (100, 0)]), 6, 4)
    assert len(d) == 10 and all(s.length == pytest.approx(6) for s in d)
    xy = np.array([[0, 0], [1, 0], [1, 1], [2, 1], [2, 2], [3, 2], [3, 3.0]])
    sm = texture.smooth_line(xy, 2)
    np.testing.assert_allclose(sm[0], xy[0])
    np.testing.assert_allclose(sm[-1], xy[-1])


# ---------------------------------------------------------------- end to end (network)


@pytest.mark.network
def test_build_scene_contract(tmp_path):
    from PIL import Image

    from chinamaps.scene import build_scene

    out = build_scene("guilin-karst", draft=True, out_dir=tmp_path / "guilin")
    meta = json.loads((out / "scene.json").read_text())
    z = np.load(out / "dem.npy")
    assert z.dtype == np.float32 and np.isfinite(z).all()
    assert list(z.shape) == meta["dem_shape"]
    tex = Image.open(out / "texture.png")
    assert tex.mode == "RGB" and [tex.height, tex.width] == meta["texture_shape"]
    mask = np.asarray(Image.open(out / "mask.png"))
    assert mask.shape == z.shape and set(np.unique(mask)) <= {0, 255}
    for k in ("crs", "bounds_proj", "z_min", "z_max", "center_lonlat", "dem_res", "texture_res", "camera", "sun"):
        assert k in meta
    assert meta["draft"]["width"] == 1600 and meta["draft_build"] is True  # render settings kept
    xmin, ymin, xmax, ymax = meta["bounds_proj"]
    assert (xmax - xmin) / meta["dem_res"] == z.shape[1] and (ymax - ymin) / meta["texture_res"] == tex.height
    assert 0 < meta["z_min"] < 200 < 600 < meta["z_max"] < 1100
    assert max(Image.open(out / "preview.png").size) <= 1600
