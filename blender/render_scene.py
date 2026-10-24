"""Oblique Cycles render of a terrain scene (inputs: docs/ARCHITECTURE.md scene contract).

    .venv-blender/bin/python blender/render_scene.py out/scenes/<name> [--draft] [--out PATH]

Writes out/renders/<name>[_draft].png (flattened over a sky gradient), <stem>_rgba.png (transparent
raw render) and <stem>_labels.json (screen positions of scene_dir/labels_proj.json features).
"""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bl_common as bc
from scenegeom import (
    bilinear,
    block_mean,
    compass_vector,
    decimation_factor,
    fit_camera,
    grid_faces,
    in_frame_region,
    shape_relief,
)

SCENE_SIZE = 100.0       # Blender units on the long side
SLAB_DEPTH = 3.0         # slab thickness below the lowest point (Blender units)
BATHY_SCALE = 0.3        # extra vertical scale for negative elevations (sea floor reads calmer)
MARGINS = (0.04, 0.04, 0.05, 0.15)  # left, right, bottom, top (room for the title block)
SKY_STRENGTH = 0.1
EXPOSURE_BIAS = 0.25     # stops above "flat ground shows its texture colour"
LABEL_LIFT = 0.05
TEXTURE_PER_PIXEL = 2.5  # max texture side relative to the render's long side
OCCLUSION_LIFTS = (0.0, 0.5, 1.0)  # scene units above the anchor tested for visibility


@dataclass
class Terrain:
    z: np.ndarray        # (h, w) Blender z of mesh vertices
    x: np.ndarray        # (w,) Blender x of columns
    y: np.ndarray        # (h,) Blender y of rows (row 0 = north)
    u: np.ndarray        # (w,) texture u of columns
    v: np.ndarray        # (h,) texture v of rows
    scale: float         # Blender units per metre
    center: tuple[float, float]
    factor: int
    zscale: float        # Blender units per metre of elevation (incl. exaggeration)

    def to_blender(self, ex: np.ndarray, ny: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return (np.asarray(ex) - self.center[0]) * self.scale, (np.asarray(ny) - self.center[1]) * self.scale

    def frac_index(self, bx: np.ndarray, by: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Fractional (col, row) of Blender x/y on the vertex grid."""
        col = (bx - self.x[0]) / (self.x[1] - self.x[0])
        row = (by - self.y[0]) / (self.y[1] - self.y[0])
        return col, row


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("scene_dir", type=Path)
    p.add_argument("--draft", action="store_true")
    p.add_argument("--out", type=Path, help="output PNG (default out/renders/<name>[_draft].png)")
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--samples", type=int)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--mesh-max", type=int, help="override mesh_max (longest mesh side in vertices)")
    p.add_argument("--override", help='JSON deep-merged over the settings, e.g. \'{"camera": {"azimuth": 150}}\'')
    p.add_argument("--exposure", type=float, help="override the automatic exposure (stops)")
    return p.parse_args(argv)


def elevation_to_z(z: np.ndarray, meta: dict, zscale: float) -> np.ndarray:
    out = z * zscale
    if meta.get("bathymetry"):
        out = np.where(z < 0, out * float(meta.get("bathy_scale", BATHY_SCALE)), out)
    return out.astype(np.float32)


def load_terrain(scene_dir: Path, meta: dict, mesh_max: int) -> Terrain:
    dem = np.load(scene_dir / "dem.npy", mmap_mode="r")
    xmin, ymin, xmax, ymax = (float(v) for v in meta["bounds_proj"])
    width_m, height_m = xmax - xmin, ymax - ymin
    rx, ry = width_m / dem.shape[1], height_m / dem.shape[0]
    f = decimation_factor(dem.shape, mesh_max)
    z = shape_relief(block_mean(np.asarray(dem), f), float(meta.get("dem_smooth_px", 0.0)), meta.get("dem_sharpen"))
    h, w = z.shape
    ex = xmin + (np.arange(w) + 0.5) * f * rx
    ny = ymax - (np.arange(h) + 0.5) * f * ry
    scale = SCENE_SIZE / max(width_m, height_m)
    zscale = scale * float(meta.get("exaggeration", 1.0))
    center = ((xmin + xmax) / 2, (ymin + ymax) / 2)
    terr = Terrain(elevation_to_z(z, meta, zscale), (ex - center[0]) * scale, (ny - center[1]) * scale,
                   ((ex - xmin) / width_m).astype(np.float32), ((ny - ymin) / height_m).astype(np.float32),
                   scale, center, f, zscale)
    print(f"terrain: dem {dem.shape} / {f} -> mesh {z.shape}, {width_m / 1000:.1f} x {height_m / 1000:.1f} km, "
          f"z {terr.z.min():.2f}..{terr.z.max():.2f} units", flush=True)
    return terr


def terrain_material(scene_dir: Path, meta: dict, max_tex: int, tmp_dir: Path):
    """Texture as base colour; water (mask = 0) is smoother with a slight sheen."""
    mat, bsdf = bc.principled("terrain", roughness=0.9)
    nt = mat.node_tree
    tex = bc.image_node(nt, bc.fit_image(scene_dir / "texture.png", max_tex, tmp_dir), "sRGB")
    mask = bc.image_node(nt, bc.fit_image(scene_dir / "mask.png", max_tex, tmp_dir), "Non-Color", "Linear")
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    water_rough = 0.4 if meta.get("bathymetry") else 0.2
    rough = nt.nodes.new("ShaderNodeMapRange")
    rough.inputs["To Min"].default_value = water_rough
    rough.inputs["To Max"].default_value = 0.9
    nt.links.new(mask.outputs["Color"], rough.inputs["Value"])
    nt.links.new(rough.outputs["Result"], bsdf.inputs["Roughness"])
    spec = nt.nodes.new("ShaderNodeMapRange")
    spec.inputs["To Min"].default_value = 0.45
    spec.inputs["To Max"].default_value = 0.3
    nt.links.new(mask.outputs["Color"], spec.inputs["Value"])
    nt.links.new(spec.outputs["Result"], bsdf.inputs["Specular IOR Level"])
    return mat


def build_terrain(scene, terr: Terrain, mat):
    h, w = terr.z.shape
    xx = np.broadcast_to(terr.x[None, :], (h, w))
    yy = np.broadcast_to(terr.y[:, None], (h, w))
    verts = np.column_stack([xx.ravel(), yy.ravel(), terr.z.ravel()])
    uv = np.column_stack([np.broadcast_to(terr.u[None, :], (h, w)).ravel(),
                          np.broadcast_to(terr.v[:, None], (h, w)).ravel()])
    me = bc.mesh_from_arrays("terrain", verts, grid_faces(h, w), uv)
    return bc.link_object(scene, "terrain", me, mat)


def perimeter(h: int, w: int) -> tuple[np.ndarray, np.ndarray]:
    """(row, col) of the grid boundary, clockwise seen from above, starting at the NW corner."""
    rows = np.concatenate([np.zeros(w - 1), np.arange(h - 1), np.full(w - 1, h - 1), np.arange(h - 1, 0, -1)])
    cols = np.concatenate([np.arange(w - 1), np.full(h - 1, w - 1), np.arange(w - 1, 0, -1), np.zeros(h - 1)])
    return rows.astype(int), cols.astype(int)


def build_skirt(scene, terr: Terrain, z_base: float):
    """Dark side walls from the terrain edge down to z_base, so the block reads as a slab."""
    r, c = perimeter(*terr.z.shape)
    n = len(r)
    top = np.column_stack([terr.x[c], terr.y[r], terr.z[r, c]])
    bot = top.copy()
    bot[:, 2] = z_base
    verts = np.vstack([top, bot])
    k = np.arange(n)
    k1 = (k + 1) % n
    faces = np.column_stack([k, k1, k1 + n, k + n])  # outward normals for a clockwise ring
    me = bc.mesh_from_arrays("skirt", verts, faces, smooth=False)
    return bc.link_object(scene, "skirt", me, bc.skirt_material(z_base, float(np.median(top[:, 2]))))


def camera_points(terr: Terrain, z_base: float, n: int = 160, frame=None) -> np.ndarray:
    """Subsampled terrain surface plus slab base corners, for framing.

    frame: optional region to fit instead of the whole slab, as fractions of the scene measured from
    its SW corner: [x0, y0, x1, y1] or a polygon [[x, y], ...]. Terrain outside it may leave the
    picture (a full-bleed close-up, or the irrelevant corners of a large slab).
    """
    h, w = terr.z.shape
    ri = np.unique(np.linspace(0, h - 1, min(h, n)).astype(int))
    ci = np.unique(np.linspace(0, w - 1, min(w, n)).astype(int))
    sub = terr.z[np.ix_(ri, ci)]
    fx = np.repeat((ci / (w - 1))[None, :], len(ri), 0).ravel()
    fy = np.repeat((1 - ri / (h - 1))[:, None], len(ci), 1).ravel()
    pts = np.column_stack([np.repeat(terr.x[ci][None, :], len(ri), 0).ravel(),
                           np.repeat(terr.y[ri][:, None], len(ci), 1).ravel(), sub.ravel()])
    if frame:
        return pts[in_frame_region(fx, fy, frame)]
    corners = np.array([[x, y, z_base] for x in terr.x[[0, -1]] for y in terr.y[[0, -1]]])
    return np.vstack([pts, corners])


def label_anchors(terr: Terrain, labels: list[dict]) -> np.ndarray:
    """Blender positions of label anchors on the rendered (decimated) surface; peaks snap to the local max."""
    if not labels:
        return np.zeros((0, 3))
    bx, by = terr.to_blender(np.array([lb["x"] for lb in labels]), np.array([lb["y"] for lb in labels]))
    col, row = terr.frac_index(bx, by)
    z = bilinear(terr.z, col, row)
    h, w = terr.z.shape
    for i, lb in enumerate(labels):
        if lb["kind"] == "peak":
            r0, c0 = round(row[i]), round(col[i])
            win = terr.z[max(0, r0 - 3):min(h, r0 + 4), max(0, c0 - 3):min(w, c0 + 4)]
            if win.size:
                rr, cc = np.unravel_index(int(win.argmax()), win.shape)
                row[i], col[i] = max(0, r0 - 3) + rr, max(0, c0 - 3) + cc
                bx[i], by[i], z[i] = terr.x[int(col[i])], terr.y[int(row[i])], win.max()
    return np.column_stack([bx, by, z + LABEL_LIFT])


def project_alternates(scene, cam, terr: Terrain, labels: list[dict], placed: list[dict]) -> None:
    """Screen positions of each label's alternate anchors (`alts`, scene CRS x/y) in place."""
    pseudo = [({**lb, "x": x, "y": y}, i) for i, lb in enumerate(labels) for x, y in lb.get("alts", [])]
    if not pseudo:
        return
    alts = bc.screen_labels(scene, cam, [p for p, _ in pseudo], label_anchors(terr, [p for p, _ in pseudo]),
                            lifts=OCCLUSION_LIFTS)
    for rec in placed:
        rec["alts"] = []
    for (_, i), a in zip(pseudo, alts):
        placed[i]["alts"].append({k: a[k] for k in ("px", "py", "depth", "in_frame", "occluded", "visible_frac")})


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    t0 = time.time()
    scene_dir = a.scene_dir.resolve()
    meta = bc.load_json(scene_dir / "scene.json")
    name = meta.get("name") or scene_dir.name
    meta["name"] = name
    meta = bc.live_settings(meta, "scenes", a.override)
    blk = bc.render_block(meta, a.draft)
    width, height = a.width or blk["width"], a.height or blk["height"]
    samples = a.samples or blk["samples"]
    stem = f"{name}_draft" if a.draft else name
    out_png = (a.out or bc.RENDER_DIR / f"{stem}.png").resolve()
    rgba_png = out_png.with_name(out_png.stem + "_rgba.png")
    labels_json = out_png.with_name(out_png.stem + "_labels.json")

    sun = meta["sun"]
    sky = {"strength": SKY_STRENGTH, "saturation": 0.45, **(meta.get("sky") or {})}
    exposure = a.exposure
    if exposure is None:
        bias = float(meta.get("exposure_bias", EXPOSURE_BIAS))
        exposure = bc.calibrate_exposure(sun, sky["strength"], bias, tmp=out_png.with_suffix(".calib.exr"),
                                         sky_saturation=sky["saturation"])
    scene = bc.reset()
    mesh_max = a.mesh_max or int(meta.get("mesh_max", 2048))
    if a.draft and not a.mesh_max:
        mesh_max = min(mesh_max, max(1024, width))
    terr = load_terrain(scene_dir, meta, mesh_max)
    tmp_dir = out_png.parent / f"_tmp_{stem}"
    build_terrain(scene, terr, terrain_material(scene_dir, meta, int(TEXTURE_PER_PIXEL * max(width, height)), tmp_dir))
    z_base = float(terr.z.min()) - SLAB_DEPTH
    build_skirt(scene, terr, z_base)
    t_mesh = time.time() - t0

    bc.add_sun(scene, sun["azimuth"], sun["elevation"], sun["strength"], sun.get("angle", 1.0), bc.sun_color(sun))
    bc.setup_world(scene, sun["azimuth"], sun["elevation"], sky["strength"], saturation=sky["saturation"])
    cam_cfg = meta["camera"]
    pts = camera_points(terr, z_base, frame=cam_cfg.get("frame"))
    target = np.array([*(pts[:, :2].min(0) + pts[:, :2].max(0)) / 2, float(np.median(pts[:, 2]))])
    fit = fit_camera(pts, target, compass_vector(cam_cfg["azimuth"], cam_cfg["elevation"]),
                     cam_cfg["focal_mm"], width / height, cam_cfg.get("distance", 1.0), MARGINS)
    cam = bc.add_camera(scene, fit, cam_cfg["focal_mm"])
    bc.setup_render(scene, width, height, samples, a.threads, exposure)
    bc.setup_haze(scene, float(meta.get("haze", 0.2)), meta.get("haze_color", bc.HAZE), fit["near"], fit["far"])

    labels_src = scene_dir / "labels_proj.json"
    labels = bc.load_json(labels_src)["labels"] if labels_src.exists() else []
    placed = bc.screen_labels(scene, cam, labels, label_anchors(terr, labels), lifts=OCCLUSION_LIFTS)
    project_alternates(scene, cam, terr, labels, placed)

    print(f"render {name}: {width}x{height} @ {samples} spp, exposure {exposure:+.2f}, "
          f"camera distance {fit['distance']:.1f}, {len(labels)} labels", flush=True)
    secs = bc.render(scene, rgba_png)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    bc.composite_background(rgba_png, out_png, grading=meta.get("grade"))
    mpx = width * height / 1e6
    info = {"scene": name, "draft": a.draft, "width": width, "height": height, "image": out_png.name,
            "render": {"samples": samples, "seconds": round(secs, 1), "setup_seconds": round(t_mesh, 1),
                       "mesh_shape": list(terr.z.shape), "exposure": exposure, "threads": a.threads,
                       "s_per_mpx_sample": round(secs / (mpx * samples), 4),
                       "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024},
            "camera": {**cam_cfg, "location": [round(float(v), 3) for v in fit["location"]],
                       "shift": [round(fit["shift_x"], 4), round(fit["shift_y"], 4)]},
            "exaggeration": float(meta.get("exaggeration", 1.0)), "title": meta.get("title", {}), "labels": placed}
    labels_json.write_text(json.dumps(info, indent=1, ensure_ascii=False))
    print(f"wrote {out_png} ({secs:.1f} s render, {secs / (mpx * samples):.3f} s/Mpx/sample, "
          f"total {time.time() - t0:.1f} s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
