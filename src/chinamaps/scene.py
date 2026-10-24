"""Build an oblique-render scene: out/scenes/<name>/{dem.npy, dem.tif, texture.png,
mask.png, scene.json, preview.png} (contract in docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import numpy as np
import yaml

from chinamaps import dem, shade, texture
from chinamaps.net import OUT_DIR, REPO_ROOT

PREVIEW_MAX = 1600
TEXSHADE_ALPHA = 0.7


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_config(name: str) -> dict:
    """Scene config merged over the defaults."""
    cfg = yaml.safe_load((REPO_ROOT / "config" / "scenes.yaml").read_text())
    if name not in cfg["scenes"]:
        raise KeyError(f"unknown scene {name!r}; expected one of {sorted(cfg['scenes'])}")
    return _merge(cfg.get("defaults", {}), cfg["scenes"][name])


def lake_mask(frame, res: float) -> np.ndarray:
    """Natural Earth lakes rasterised on the frame grid at res (bool)."""
    from rasterio.features import rasterize

    from chinamaps.sources import naturalearth as ne

    clip = frame.lonlat_polygon().buffer(0.3)
    lk = ne.read("ne_10m_lakes")
    lk = lk[lk.intersects(clip)]
    shp = frame.shape(res)
    if lk.empty:
        return np.zeros(shp, dtype=bool)
    geoms = lk.to_crs(frame.crs).geometry.values
    return rasterize(((g, 1) for g in geoms), out_shape=shp, transform=frame.transform(res), fill=0,
                     dtype="uint8").astype(bool)


def write_geotiff(path: Path, a: np.ndarray, frame, res: float) -> None:
    import rasterio

    profile = dict(driver="GTiff", width=a.shape[1], height=a.shape[0], count=1, dtype="float32",
                   crs=frame.crs, transform=frame.transform(res), compress="deflate", predictor=3,
                   tiled=True, blockxsize=512, blockysize=512)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(a.astype(np.float32), 1)


def save_png(path: Path, a: np.ndarray) -> None:
    from PIL import Image

    Image.fromarray(a).save(path, compress_level=4)


def preview(a: np.ndarray, size: int = PREVIEW_MAX):
    """Downsample an RGB array to at most size px on the long side."""
    from PIL import Image

    h, w = a.shape[:2]
    s = min(1.0, size / max(h, w))
    step = max(1, int(1 / s) // 2)  # cheap pre-decimation, then a proper filter
    img = Image.fromarray(np.ascontiguousarray(a[::step, ::step]))
    return img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)


def build_scene(name: str, draft: bool = False, out_dir: Path | None = None) -> Path:
    """Build DEM, texture, mask and metadata for a scene. Returns the output directory."""
    t0 = time.time()
    cfg = load_config(name)
    frame = dem.make_frame(cfg, draft)
    out = Path(out_dir or OUT_DIR / "scenes" / name)
    out.mkdir(parents=True, exist_ok=True)
    timings = {}
    print(f"scene {name}: dem {frame.dem_shape} @ {frame.dem_res:g} m, texture {frame.texture_shape} "
          f"@ {frame.texture_res:g} m", flush=True)

    z_t, land_t = dem.build_elevation(frame, cfg)
    timings["elevation"] = time.time() - t0

    f = frame.factor
    z_d = dem.block_mean(z_t, f)
    land_d = dem.block_mean(land_t.astype(np.float32), f) >= 0.5
    water_d = ~land_d | lake_mask(frame, frame.dem_res)
    np.save(out / "dem.npy", z_d)
    write_geotiff(out / "dem.tif", z_d, frame, frame.dem_res)
    save_png(out / "mask.png", np.where(water_d, 0, 255).astype(np.uint8))

    t1 = time.time()
    ts = shade.texture_shade(z_d, TEXSHADE_ALPHA, mask=land_d)
    timings["texshade"] = time.time() - t1

    t1 = time.time()
    tex = texture.build_texture(frame, cfg, z_t, land_t, ts, frame.transform(frame.dem_res))
    del z_t, land_t, ts
    timings["texture"] = time.time() - t1
    t1 = time.time()
    save_png(out / "texture.png", tex)
    preview(tex).save(out / "preview.png")
    del tex
    timings["write"] = time.time() - t1

    meta = _merge(cfg, {
        "name": name,
        "draft_build": draft,  # "draft" itself holds the render settings from the defaults
        "crs": frame.crs,
        "bounds_proj": list(frame.bounds),
        "dem_shape": list(frame.dem_shape),
        "texture_shape": list(frame.texture_shape),
        "dem_res": frame.dem_res,
        "texture_res": frame.texture_res,
        "z_min": float(z_d.min()),
        "z_max": float(z_d.max()),
        "center_lonlat": list(frame.center_lonlat),
        "land_fraction": float(land_d.mean()),
        "hillshade_zfactor": shade.default_zfactor(frame.texture_res),
        "build_seconds": round(time.time() - t0, 1),
    })
    (out / "scene.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    print(f"scene {name}: z {meta['z_min']:.0f}..{meta['z_max']:.0f} m in {meta['build_seconds']} s "
          f"({', '.join(f'{k} {v:.0f}s' for k, v in timings.items())}) -> {out}", flush=True)
    return out
