"""Oblique Cycles render of a 3D city model (inputs: docs/ARCHITECTURE.md city contract).

    .venv-blender/bin/python blender/render_city.py out/cities/<name> [--draft] [--out PATH]

Writes out/renders/city-<name>[_draft].png (flattened over a sky gradient), <stem>_rgba.png and
<stem>_labels.json (title + landmark screen positions for chinamaps.annotate).
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bl_common as bc
from scenegeom import compass_vector, fit_camera, in_frame_region

SKY_STRENGTH = 0.1
EXPOSURE_BIAS = -0.1     # off-white walls facing a low sun would clip at the ground-matched exposure
MARGINS = (0.03, 0.03, 0.04, 0.13)
SLAB_FRACTION = 0.035    # slab depth below the lowest ground point, as a fraction of the extent
BUILDING_COLOR = (0.80, 0.80, 0.82)  # linear; neutral white (towers carry a glass tint in the vertex colours)
BUILDING_ROUGHNESS = (0.45, 0.65)
BANK_COLOR = (96, 92, 84)  # sRGB; replaces stretched texels on faces steeper than ~50 deg
WATER_ROUGHNESS = 0.15
LANDMARK_LIFT = 4.0      # metres above the roof


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("city_dir", type=Path)
    p.add_argument("--draft", action="store_true")
    p.add_argument("--out", type=Path, help="output PNG (default out/renders/city-<name>[_draft].png)")
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--samples", type=int)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--override", help='JSON deep-merged over the settings, e.g. \'{"camera": {"azimuth": 150}}\'')
    p.add_argument("--exposure", type=float)
    return p.parse_args(argv)


def import_city(path: Path) -> tuple[bpy.types.Object, bpy.types.Object | None]:
    bpy.ops.import_scene.gltf(filepath=str(path))
    objs = {o.name.split(".")[0]: o for o in bpy.context.scene.objects if o.type == "MESH"}
    return objs["terrain"], objs.get("buildings")


def vertices(obj) -> np.ndarray:
    co = np.empty(len(obj.data.vertices) * 3, np.float32)
    obj.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    m = np.array(obj.matrix_world)
    return co @ m[:3, :3].T + m[:3, 3]


def ground_material(terrain, water_png: Path | None = None, water_roughness: float = WATER_ROUGHNESS) -> None:
    """Keep the embedded ground image, fully matte on land; water (water_mask.png from build_city, else
    dark-blue pixels) is smoother with a sheen. Steep faces (river embankments, cuttings), where the
    texture stretches, blend to a plain bank colour."""
    mat = terrain.data.materials[0]
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    tex = next((n for n in nt.nodes if n.bl_idname == "ShaderNodeTexImage"), None)
    bsdf.inputs["Roughness"].default_value = 1.0
    bsdf.inputs["Metallic"].default_value = 0.0
    bsdf.inputs["Specular IOR Level"].default_value = 0.0
    if tex is None:
        return
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    nz = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(geo.outputs["Normal"], nz.inputs["Vector"])
    steep = nt.nodes.new("ShaderNodeMapRange")  # normal z: 0.70 (45 deg) -> 0, 0.56 (56 deg) -> 1
    steep.inputs["From Min"].default_value = 0.70
    steep.inputs["From Max"].default_value = 0.56
    nt.links.new(nz.outputs["Z"], steep.inputs["Value"])
    mix = nt.nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    bank = bc.srgb_to_linear(np.asarray(BANK_COLOR) / 255.0)
    bc.socket(mix.inputs, "B_Color").default_value = (*bank, 1.0)
    nt.links.new(steep.outputs["Result"], bc.socket(mix.inputs, "Factor_Float"))
    nt.links.new(tex.outputs["Color"], bc.socket(mix.inputs, "A_Color"))
    nt.links.new(bc.socket(mix.outputs, "Result_Color"), bsdf.inputs["Base Color"])
    rough = nt.nodes.new("ShaderNodeMapRange")
    rough.inputs["To Min"].default_value = 1.0
    rough.inputs["To Max"].default_value = water_roughness
    spec = nt.nodes.new("ShaderNodeMapRange")
    spec.inputs["To Min"].default_value = 0.0
    spec.inputs["To Max"].default_value = 0.5
    # water is flat: no sheen where a water polygon overlaps an embankment (white glare streaks)
    flat = nt.nodes.new("ShaderNodeMapRange")
    flat.inputs["From Min"].default_value = 0.94
    flat.inputs["From Max"].default_value = 0.985
    nt.links.new(nz.outputs["Z"], flat.inputs["Value"])
    wet = nt.nodes.new("ShaderNodeMath")
    wet.operation = "MULTIPLY"
    nt.links.new(flat.outputs["Result"], wet.inputs[1])
    nt.links.new(wet.outputs[0], rough.inputs["Value"])
    nt.links.new(wet.outputs[0], spec.inputs["Value"])
    nt.links.new(rough.outputs["Result"], bsdf.inputs["Roughness"])
    nt.links.new(spec.outputs["Result"], bsdf.inputs["Specular IOR Level"])
    if water_png is not None and water_png.exists():
        wm = bc.image_node(nt, water_png, "Non-Color", "Linear")
        nt.links.new(wm.outputs["Color"], wet.inputs[0])
        return
    sep = nt.nodes.new("ShaderNodeSeparateColor")
    nt.links.new(tex.outputs["Color"], sep.inputs["Color"])
    diff = nt.nodes.new("ShaderNodeMath")
    diff.operation = "SUBTRACT"
    nt.links.new(sep.outputs["Blue"], diff.inputs[0])
    nt.links.new(sep.outputs["Red"], diff.inputs[1])
    water = nt.nodes.new("ShaderNodeMapRange")  # linear B - R: water ~0.1, land/roads ~0
    water.inputs["From Min"].default_value = 0.03
    water.inputs["From Max"].default_value = 0.08
    nt.links.new(diff.outputs[0], water.inputs["Value"])
    nt.links.new(water.outputs["Result"], wet.inputs[0])


def building_material(buildings) -> None:
    """Warm off-white, modulated by the per-building tint in the Color attribute, with slight
    roughness variation."""
    mat, bsdf = bc.principled("buildings", BUILDING_COLOR, 0.55)
    nt = mat.node_tree
    attrs = [a.name for a in buildings.data.color_attributes]
    if attrs:
        col = nt.nodes.new("ShaderNodeVertexColor")
        col.layer_name = attrs[0]
        mul = nt.nodes.new("ShaderNodeMix")
        mul.data_type = "RGBA"
        mul.blend_type = "MULTIPLY"
        bc.socket(mul.inputs, "Factor_Float").default_value = 1.0
        bc.socket(mul.inputs, "A_Color").default_value = (*BUILDING_COLOR, 1.0)
        nt.links.new(col.outputs["Color"], bc.socket(mul.inputs, "B_Color"))
        nt.links.new(bc.socket(mul.outputs, "Result_Color"), bsdf.inputs["Base Color"])
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 0.02  # per metre: varies building to building
    rough = nt.nodes.new("ShaderNodeMapRange")
    rough.inputs["From Min"].default_value = 0.3
    rough.inputs["From Max"].default_value = 0.7
    rough.inputs["To Min"].default_value, rough.inputs["To Max"].default_value = BUILDING_ROUGHNESS
    nt.links.new(noise.outputs["Fac"], rough.inputs["Value"])
    nt.links.new(rough.outputs["Result"], bsdf.inputs["Roughness"])
    buildings.data.materials.clear()
    buildings.data.materials.append(mat)


def perimeter_ring(co: np.ndarray, tol_frac: float = 1e-4) -> np.ndarray:
    """Boundary vertices of a rectangular grid mesh, clockwise from above (N, E, S, W edges)."""
    lo, hi = co[:, :2].min(0), co[:, :2].max(0)
    tol = float((hi - lo).max()) * tol_frac
    x, y = co[:, 0], co[:, 1]
    n = np.flatnonzero(np.abs(y - hi[1]) < tol)
    e = np.flatnonzero(np.abs(x - hi[0]) < tol)
    s = np.flatnonzero(np.abs(y - lo[1]) < tol)
    w = np.flatnonzero(np.abs(x - lo[0]) < tol)
    ring = np.concatenate([n[np.argsort(x[n])], e[np.argsort(-y[e])], s[np.argsort(-x[s])], w[np.argsort(y[w])]])
    pts = co[ring]
    keep = np.ones(len(pts), bool)
    keep[1:] = np.any(np.abs(np.diff(pts[:, :2], axis=0)) > tol, axis=1)
    pts = pts[keep]
    if len(pts) > 1 and np.all(np.abs(pts[0, :2] - pts[-1, :2]) <= tol):
        pts = pts[:-1]
    return pts


def build_skirt(scene, ground: np.ndarray, z_base: float):
    top = perimeter_ring(ground)
    n = len(top)
    bot = top.copy()
    bot[:, 2] = z_base
    k = np.arange(n)
    faces = np.column_stack([k, (k + 1) % n, (k + 1) % n + n, k + n])
    me = bc.mesh_from_arrays("skirt", np.vstack([top, bot]), faces, smooth=False)
    return bc.link_object(scene, "skirt", me, bc.skirt_material(z_base, float(np.median(top[:, 2]))))


def landmark_labels(meta: dict) -> tuple[list[dict], np.ndarray]:
    import yaml

    names = {}
    labels_yaml = bc.REPO_ROOT / "config" / "labels.yaml"
    if labels_yaml.exists():
        names = yaml.safe_load(labels_yaml.read_text()).get("landmarks", {}) or {}
    labels, pts = [], []
    for lm in meta.get("landmarks", []):
        if "local" not in lm:
            continue
        x, y, z = lm["local"]  # glTF frame: y up, z = -north
        top = lm.get("top_z", y + lm.get("height", 0.0))
        labels.append({"zh": names.get(lm["name"], lm["name"]), "en": lm["name"], "kind": "landmark", "rank": 1,
                       "height": lm.get("height")})
        pts.append((x, -z, top + LANDMARK_LIFT))
    return labels, np.array(pts, float).reshape(-1, 3)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    t0 = time.time()
    city_dir = a.city_dir.resolve()
    meta = bc.load_json(city_dir / "city.json")
    name = meta.get("name") or city_dir.name
    meta["name"] = name
    meta = bc.live_settings(meta, "cities", a.override)
    blk = bc.render_block(meta, a.draft)
    width, height = a.width or blk["width"], a.height or blk["height"]
    samples = a.samples or blk["samples"]
    stem = f"city-{name}_draft" if a.draft else f"city-{name}"
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
    terrain, buildings = import_city(city_dir / "city.glb")
    ground_material(terrain, city_dir / "water_mask.png", float(meta.get("water_roughness", WATER_ROUGHNESS)))
    if buildings is not None:
        building_material(buildings)
    ground = vertices(terrain)
    extent = float(np.ptp(ground[:, :2], axis=0).max())
    z_base = float(ground[:, 2].min()) - SLAB_FRACTION * extent
    build_skirt(scene, ground, z_base)
    pts = ground[:: max(1, len(ground) // 20000)]
    if buildings is not None:
        bv = vertices(buildings)
        pts = np.vstack([pts, bv[:: max(1, len(bv) // 20000)]])
        del bv
    lo, hi = ground.min(0), ground.max(0)
    del ground
    cam_cfg = meta["camera"]
    if cam_cfg.get("frame"):  # fit only part of the model; the slab edges may leave the picture
        f = (pts[:, :2] - lo[:2]) / (hi[:2] - lo[:2])
        pts = pts[in_frame_region(f[:, 0], f[:, 1], cam_cfg["frame"])]
    else:
        corners = np.array([[x, y, z_base] for x in (lo[0], hi[0]) for y in (lo[1], hi[1])])
        pts = np.vstack([pts, corners])
    t_setup = time.time() - t0

    bc.add_sun(scene, sun["azimuth"], sun["elevation"], sun["strength"], sun.get("angle", 1.0), bc.sun_color(sun))
    bc.setup_world(scene, sun["azimuth"], sun["elevation"], sky["strength"], saturation=sky["saturation"])
    pmin, pmax = pts[:, :2].min(0), pts[:, :2].max(0)
    target = np.array([(pmin[0] + pmax[0]) / 2, (pmin[1] + pmax[1]) / 2, float(np.median(pts[:, 2]))])
    fit = fit_camera(pts, target, compass_vector(cam_cfg["azimuth"], cam_cfg["elevation"]), cam_cfg["focal_mm"],
                     width / height, cam_cfg.get("distance", 1.0), MARGINS)
    cam = bc.add_camera(scene, fit, cam_cfg["focal_mm"])
    bc.setup_render(scene, width, height, samples, a.threads, exposure)
    bc.setup_haze(scene, float(meta.get("haze", 0.12)), meta.get("haze_color", bc.HAZE), fit["near"], fit["far"])
    labels, anchors = landmark_labels(meta)
    placed = bc.screen_labels(scene, cam, labels, anchors, occlusion_tol=0.002, lifts=(0.0, 30.0))

    print(f"render city-{name}: {width}x{height} @ {samples} spp, exposure {exposure:+.2f}, "
          f"camera distance {fit['distance']:.0f} m", flush=True)
    secs = bc.render(scene, rgba_png)
    bc.composite_background(rgba_png, out_png, grading=meta.get("grade"))
    mpx = width * height / 1e6
    info = {"scene": name, "kind": "city", "draft": a.draft, "width": width, "height": height,
            "image": out_png.name,
            "render": {"samples": samples, "seconds": round(secs, 1), "setup_seconds": round(t_setup, 1),
                       "exposure": exposure, "threads": a.threads,
                       "s_per_mpx_sample": round(secs / (mpx * samples), 4),
                       "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024},
            "camera": {**cam_cfg, "location": [round(float(v), 2) for v in fit["location"]],
                       "shift": [round(fit["shift_x"], 4), round(fit["shift_y"], 4)]},
            "title": meta.get("title", {}), "labels": placed}
    labels_json.write_text(json.dumps(info, indent=1, ensure_ascii=False))
    print(f"wrote {out_png} ({secs:.1f} s render, {secs / (mpx * samples):.3f} s/Mpx/sample, "
          f"total {time.time() - t0:.1f} s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
