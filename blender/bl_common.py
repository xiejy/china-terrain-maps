"""bpy helpers shared by render_scene.py and render_city.py (Blender 5.2, Cycles CPU)."""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector
from scenegeom import compass_vector, grade, srgb_to_linear

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = Path(os.environ.get("CHINAMAPS_OUT", REPO_ROOT / "out"))  # same rule as chinamaps.net.OUT_DIR
RENDER_DIR = OUT_DIR / "renders"

# light, slightly cool sky behind the slab; haze fades distant terrain toward HAZE
BG_TOP = (214, 226, 236)
BG_BOTTOM = (246, 244, 239)
HAZE = (205, 218, 230)
# slab side walls: warm dark brown, lighter at the top edge (sRGB), so the base reads as earth, not a void
SKIRT_TOP = (78, 66, 54)
SKIRT_BOTTOM = (34, 29, 25)
SKIRT_GLOW = 0.8  # emission strength of the wall colour (about what the sky fill alone would give)


def reset() -> bpy.types.Scene:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    return bpy.context.scene


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


RENDER_KEYS = ("camera", "sun", "haze", "haze_color", "sky", "mesh_max", "draft", "final", "title", "exaggeration",
               "bathy_scale", "exposure_bias", "grade", "dem_smooth_px", "dem_sharpen", "water_roughness")


def deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def live_settings(meta: dict, section: str, override: str | None = None) -> dict:
    """Overlay render-only keys (camera, sun, haze, exaggeration, mesh_max, draft/final blocks, title) from
    the current config/<section>.yaml, so tuning them does not require rebuilding the scene or city.

    override: optional JSON object deep-merged last (look development without editing the config).
    """
    import yaml

    path = REPO_ROOT / "config" / f"{section}.yaml"
    if not path.exists():
        return meta
    cfg = yaml.safe_load(path.read_text())
    entry = (cfg.get(section) or {}).get(meta.get("name", ""))
    out = dict(meta)
    if entry is not None:
        merged = deep_merge(cfg.get("defaults") or {}, entry)
        for k in RENDER_KEYS:
            if k not in merged:
                continue
            cur = out.get(k)
            mergeable = k != "title" and isinstance(cur, dict) and isinstance(merged[k], dict)
            out[k] = deep_merge(cur, merged[k]) if mergeable else merged[k]
    return deep_merge(out, json.loads(override)) if override else out


def render_block(meta: dict, draft: bool) -> dict:
    """Resolution/samples block; falls back to config/scenes.yaml defaults (scene.json may carry a
    boolean `draft` flag instead of the block, and cities have none)."""
    key = "draft" if draft else "final"
    blk = meta.get(key)
    if isinstance(blk, dict) and blk:
        return dict(blk)
    import yaml

    cfg = yaml.safe_load((REPO_ROOT / "config" / "scenes.yaml").read_text())
    return dict(cfg.get("defaults", {}).get(key) or ({"width": 1600, "height": 900, "samples": 32} if draft else
                                                     {"width": 3840, "height": 2160, "samples": 128}))


def look_quat(direction, track: str = "-Z"):
    return Vector(tuple(direction)).to_track_quat(track, "Y")


def add_sun(scene, azimuth: float, elevation: float, strength: float, angle_deg: float,
            color=(1.0, 0.94, 0.86), name: str = "sun"):
    """Sun light coming FROM compass azimuth/elevation (a Blender sun shines along local -Z)."""
    lamp = bpy.data.lights.new(name, "SUN")
    lamp.energy = strength
    lamp.angle = math.radians(angle_deg)
    lamp.color = color
    obj = bpy.data.objects.new(name, lamp)
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = look_quat(compass_vector(azimuth, elevation), "Z")
    scene.collection.objects.link(obj)
    return obj


def setup_world(scene, sun_azimuth: float, sun_elevation: float, strength: float = 0.3,
                sky_type: str = "MULTIPLE_SCATTERING", saturation: float = 0.45, camera_color=None):
    """Physical sky (no sun disc; the sun lamp is the key) as a desaturated fill light; a higher
    saturation gives cooler (bluer) shadows.

    camera_color: optional linear RGB seen by camera rays instead of the sky.
    """
    world = bpy.data.worlds.new("world")
    scene.world = world
    nt = world.node_tree
    nt.nodes.clear()
    sky = nt.nodes.new("ShaderNodeTexSky")
    sky.sky_type = sky_type
    sky.sun_disc = False
    sky.sun_elevation = math.radians(max(sun_elevation, 0.5))
    sky.sun_rotation = math.radians(sun_azimuth)  # verified: rotation == compass azimuth (x east, y north)
    hsv = nt.nodes.new("ShaderNodeHueSaturation")
    hsv.inputs["Saturation"].default_value = saturation
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.inputs["Strength"].default_value = strength
    nt.links.new(sky.outputs["Color"], hsv.inputs["Color"])
    nt.links.new(hsv.outputs["Color"], bg.inputs["Color"])
    out = nt.nodes.new("ShaderNodeOutputWorld")
    shader = bg.outputs["Background"]
    if camera_color is not None:
        lp = nt.nodes.new("ShaderNodeLightPath")
        flat = nt.nodes.new("ShaderNodeBackground")
        flat.inputs["Color"].default_value = (*camera_color, 1.0)
        mix = nt.nodes.new("ShaderNodeMixShader")
        nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs["Fac"])
        nt.links.new(shader, mix.inputs[1])
        nt.links.new(flat.outputs["Background"], mix.inputs[2])
        shader = mix.outputs["Shader"]
    nt.links.new(shader, out.inputs["Surface"])
    return world


def add_camera(scene, fit: dict, focal_mm: float, name: str = "camera"):
    cam = bpy.data.cameras.new(name)
    cam.lens = focal_mm
    cam.sensor_width = 36.0
    cam.sensor_fit = "AUTO"
    cam.shift_x, cam.shift_y = float(fit["shift_x"]), float(fit["shift_y"])
    cam.clip_start = max(fit["near"] * 0.05, 1e-3)  # framed close-ups have terrain nearer than the fit
    cam.clip_end = fit["far"] * 4.0
    obj = bpy.data.objects.new(name, cam)
    obj.location = tuple(float(v) for v in fit["location"])
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = look_quat(fit["basis"][2], "-Z")
    scene.collection.objects.link(obj)
    scene.camera = obj
    return obj


def setup_render(scene, width: int, height: int, samples: int, threads: int = 4, exposure: float = 0.0,
                 transparent: bool = True, view_transform: str = "Standard", look: str = "None") -> None:
    r = scene.render
    r.engine = "CYCLES"
    r.resolution_x, r.resolution_y, r.resolution_percentage = int(width), int(height), 100
    r.film_transparent = transparent
    r.threads_mode = "FIXED"
    r.threads = threads
    r.compositor_device = "CPU"  # the GPU default aborts without libEGL
    r.compositor_denoise_device = "CPU"
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA" if transparent else "RGB"
    r.image_settings.color_depth = "8"
    r.image_settings.compression = 15
    c = scene.cycles
    c.device = "CPU"
    c.samples = int(samples)
    c.use_adaptive_sampling = True
    c.adaptive_threshold = 0.02
    c.use_denoising = True
    c.denoiser = "OPENIMAGEDENOISE"
    c.denoising_input_passes = "RGB_ALBEDO_NORMAL"
    c.max_bounces, c.diffuse_bounces, c.glossy_bounces = 4, 2, 2
    c.transmission_bounces, c.volume_bounces, c.transparent_max_bounces = 0, 0, 2
    c.caustics_reflective = c.caustics_refractive = False
    c.seed = 7
    vs = scene.view_settings
    vs.view_transform = view_transform
    vs.look = look
    vs.exposure = exposure
    vs.gamma = 1.0
    scene.display_settings.display_device = "sRGB"


def setup_haze(scene, haze: float, color_srgb, near: float, far: float) -> None:
    """Aerial perspective: mix the image toward a haze colour by mist * haze (5.x compositor)."""
    if haze <= 0:
        return
    ms = scene.world.mist_settings
    ms.start = near
    ms.depth = max(far - near, 1e-3)
    ms.falloff = "QUADRATIC"
    scene.view_layers[0].use_pass_mist = True
    ng = bpy.data.node_groups.new("haze", "CompositorNodeTree")
    ng.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    n = ng.nodes
    rl = n.new("CompositorNodeRLayers")
    fac = n.new("ShaderNodeMath")
    fac.operation = "MULTIPLY"
    fac.inputs[1].default_value = haze
    fac2 = n.new("ShaderNodeMath")  # zero on transparent background pixels
    fac2.operation = "MULTIPLY"
    mix = n.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.blend_type = "MIX"
    lin = srgb_to_linear(np.asarray(color_srgb, float) / 255.0)
    socket(mix.inputs, "B_Color").default_value = (*lin, 1.0)
    setalpha = n.new("CompositorNodeSetAlpha")
    out = n.new("NodeGroupOutput")
    lk = ng.links
    lk.new(rl.outputs["Mist"], fac.inputs[0])
    lk.new(fac.outputs[0], fac2.inputs[0])
    lk.new(rl.outputs["Alpha"], fac2.inputs[1])
    lk.new(fac2.outputs[0], socket(mix.inputs, "Factor_Float"))
    lk.new(rl.outputs["Image"], socket(mix.inputs, "A_Color"))
    lk.new(socket(mix.outputs, "Result_Color"), setalpha.inputs["Image"])
    lk.new(rl.outputs["Alpha"], setalpha.inputs["Alpha"])
    lk.new(setalpha.outputs["Image"], out.inputs["Image"])
    scene.compositing_node_group = ng


def socket(sockets, identifier: str):
    """Socket by identifier (names like "A"/"B" repeat across data types on Mix nodes)."""
    return next(s for s in sockets if s.identifier == identifier)


def principled(name: str, color=(0.8, 0.8, 0.8), roughness: float = 0.8):
    mat = bpy.data.materials.new(name)
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF") or nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    return mat, bsdf


def fit_image(path: Path, max_side: int, tmp_dir: Path) -> Path:
    """Path of an image no larger than max_side px (a LANCZOS-reduced temporary copy if needed).

    Textures far finer than the output (e.g. 17k px for a 3.8k render) only cost memory twice
    over (Blender and Cycles each hold an RGBA copy).
    """
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        if max(im.size) <= max_side:
            return path
        s = max_side / max(im.size)
        size = (max(1, round(im.width * s)), max(1, round(im.height * s)))
        small = im.resize(size, Image.LANCZOS, reducing_gap=3.0)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out = tmp_dir / f"{path.stem}_{max_side}.png"
    small.save(out, compress_level=1)
    print(f"image {path.name}: {max(im.size)} px -> {max_side} px for this render", flush=True)
    return out


def image_node(nt, path: Path, colorspace: str = "sRGB", interpolation: str = "Cubic"):
    img = bpy.data.images.load(str(path), check_existing=True)
    img.colorspace_settings.name = colorspace
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.interpolation = interpolation
    tex.extension = "EXTEND"
    return tex


def mesh_from_arrays(name: str, verts: np.ndarray, faces: np.ndarray, uv: np.ndarray | None = None,
                     smooth: bool = True):
    """Build a mesh from (n, 3) verts and (m, k) faces with foreach_set (fast for millions of quads).

    uv: optional per-vertex (n, 2) coordinates.
    """
    me = bpy.data.meshes.new(name)
    nv, (nf, k) = len(verts), faces.shape
    me.vertices.add(nv)
    me.vertices.foreach_set("co", np.ascontiguousarray(verts, np.float32).ravel())
    me.loops.add(nf * k)
    corner = np.ascontiguousarray(faces, np.int32).ravel()
    me.loops.foreach_set("vertex_index", corner)
    me.polygons.add(nf)
    me.polygons.foreach_set("loop_start", np.arange(0, nf * k, k, dtype=np.int32))
    if uv is not None:
        layer = me.uv_layers.new(name="UVMap")
        layer.data.foreach_set("uv", np.ascontiguousarray(uv[corner], np.float32).ravel())
    me.update(calc_edges=True)
    if smooth:
        me.shade_smooth()
    return me


def link_object(scene, name: str, me, mat=None):
    obj = bpy.data.objects.new(name, me)
    if mat is not None:
        me.materials.append(mat)
    scene.collection.objects.link(obj)
    return obj


def calibrate_exposure(sun: dict, sky_strength: float, bias: float = 0.0, albedo: float = 0.18,
                       tmp: Path | None = None, sky_saturation: float = 0.45) -> float:
    """Exposure (stops) at which flat ground under this sun + sky shows its albedo unchanged.

    Renders a tiny lit plane in a scratch scene, so call it before building the real scene.
    """
    sc = reset()
    me = bpy.data.meshes.new("plane")
    me.from_pydata([(-50, -50, 0), (50, -50, 0), (50, 50, 0), (-50, 50, 0)], [], [(0, 1, 2, 3)])
    mat, _ = principled("grey", color=(albedo,) * 3, roughness=0.9)
    link_object(sc, "plane", me, mat)
    add_sun(sc, sun["azimuth"], sun["elevation"], sun["strength"], sun.get("angle", 1.0), sun_color(sun))
    setup_world(sc, sun["azimuth"], sun["elevation"], sky_strength, saturation=sky_saturation)
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    cam.location = (0, 0, 30)
    sc.collection.objects.link(cam)
    sc.camera = cam
    setup_render(sc, 16, 16, 16, 2, 0.0, transparent=False)
    sc.cycles.use_denoising = False
    sc.render.image_settings.file_format = "OPEN_EXR"
    tmp = tmp or Path(bpy.app.tempdir or "/tmp") / "chinamaps_calib.exr"
    sc.render.filepath = str(tmp)
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(str(tmp))
    rgb = np.array(img.pixels[:], dtype=np.float32).reshape(-1, 4)[:, :3].mean(0)
    tmp.unlink(missing_ok=True)
    lum = float(rgb @ np.array([0.2126, 0.7152, 0.0722]))
    return round(-math.log2(max(lum / albedo, 1e-4)) + bias, 3)


def sun_color(sun: dict) -> tuple[float, float, float]:
    """Sun lamp colour: config sun.color (RGB 0-1) or the default warm white."""
    return tuple(float(c) for c in sun.get("color", (1.0, 0.94, 0.86)))


def skirt_material(z_base: float, z_top: float, name: str = "skirt"):
    """Slab walls: vertical gradient from SKIRT_BOTTOM at z_base to SKIRT_TOP at z_top (object z)."""
    mat, bsdf = principled(name, roughness=0.9)
    nt = mat.node_tree
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    rng = nt.nodes.new("ShaderNodeMapRange")
    rng.inputs["From Min"].default_value = z_base
    rng.inputs["From Max"].default_value = z_top
    mix = nt.nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    socket(mix.inputs, "A_Color").default_value = (*srgb_to_linear(np.asarray(SKIRT_BOTTOM) / 255.0), 1.0)
    socket(mix.inputs, "B_Color").default_value = (*srgb_to_linear(np.asarray(SKIRT_TOP) / 255.0), 1.0)
    nt.links.new(geo.outputs["Position"], sep.inputs["Vector"])
    nt.links.new(sep.outputs["Z"], rng.inputs["Value"])
    nt.links.new(rng.outputs["Result"], socket(mix.inputs, "Factor_Float"))
    nt.links.new(socket(mix.outputs, "Result_Color"), bsdf.inputs["Base Color"])
    # a little self-light: walls facing away from the sun would otherwise render as a black void
    nt.links.new(socket(mix.outputs, "Result_Color"), bsdf.inputs["Emission Color"])
    bsdf.inputs["Emission Strength"].default_value = SKIRT_GLOW
    return mat


def render(scene, path: Path) -> float:
    """Render a still to path; returns wall seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scene.render.filepath = str(path)
    t = time.time()
    bpy.ops.render.render(write_still=True)
    return time.time() - t


def screen_labels(scene, cam, labels: list[dict], points: np.ndarray, occlusion_tol: float = 0.004,
                  lifts: tuple[float, ...] = (0.0,)) -> list[dict]:
    """Pixel positions (origin top-left), depth and visibility of label anchor points.

    occluded: the anchor itself is hidden. visible_frac: share of the anchor and the points
    lifted by `lifts` (scene units, +z) that the camera sees, so a town in a valley behind a
    single karst tower is still labelled while one behind a mountain range is not.
    """
    from bpy_extras.object_utils import world_to_camera_view

    depsgraph = bpy.context.evaluated_depsgraph_get()
    w, h = scene.render.resolution_x, scene.render.resolution_y
    origin = cam.matrix_world.translation.copy()

    def hidden(v: Vector) -> bool:
        ray = v - origin
        hit, *_ = scene.ray_cast(depsgraph, origin, ray.normalized(), distance=ray.length * (1 - occlusion_tol))
        return bool(hit)

    out = []
    for lab, p in zip(labels, points):
        v = Vector(tuple(float(c) for c in p))
        co = world_to_camera_view(scene, cam, v)
        blocked = [hidden(v + Vector((0.0, 0.0, dz))) for dz in lifts]
        rec = {k: lab[k] for k in lab if k not in ("x", "y")}
        rec.update(px=round(co.x * w, 2), py=round((1.0 - co.y) * h, 2), depth=round(co.z, 4),
                   in_frame=bool(co.z > 0 and 0 <= co.x <= 1 and 0 <= co.y <= 1), occluded=blocked[0],
                   visible_frac=round(1 - sum(blocked) / len(blocked), 3),
                   world=[round(float(c), 4) for c in p])
        out.append(rec)
    return out


def composite_background(rgba_path: Path, out_path: Path, top=BG_TOP, bottom=BG_BOTTOM,
                         shadow: float = 0.22, grading: dict | None = None) -> None:
    """Flatten a transparent render over a vertical gradient with a soft contact shadow.

    grading: optional {saturation, contrast} applied to the render only (not the background).
    """
    from PIL import Image, ImageFilter

    im = Image.open(rgba_path).convert("RGBA")
    w, h = im.size
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    bg = (np.array(top, np.float32) * (1 - t) + np.array(bottom, np.float32) * t) * np.ones((1, w, 1), np.float32)
    if shadow > 0:
        a = im.getchannel("A")
        r = max(2, int(w * 0.012))
        sh = a.transform(a.size, Image.AFFINE, (1, 0, 0, 0, 1, -int(r * 0.8))).filter(ImageFilter.GaussianBlur(r))
        bg *= 1 - shadow * (np.asarray(sh, np.float32)[..., None] / 255.0)
    rgba = np.asarray(im, np.float32)
    alpha = rgba[..., 3:4] / 255.0
    rgb = grade(rgba[..., :3], **grading) if grading else rgba[..., :3]
    flat = rgb * alpha + bg * (1 - alpha)  # Blender PNGs carry straight alpha
    Image.fromarray(np.clip(flat + 0.5, 0, 255).astype(np.uint8)).save(out_path, compress_level=4)
