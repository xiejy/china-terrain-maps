"""chinamaps command line.

    chinamaps smoke                      environment + data-access checks
    chinamaps scene <name> [--draft]     DEM + texture for an oblique scene
    chinamaps render <name> [--draft]    labels -> Blender Cycles render -> annotated PNG
    chinamaps annotate <name> [--draft]  re-run only the label/title compositing
    chinamaps city <name>                3D city model (GLB + footprints)
    chinamaps render-city <name> [--draft]
    chinamaps webdata                    vector PMTiles + web assets

render/render-city also take --width/--height/--samples/--threads overrides. Final (non-draft)
renders also get gallery/<name>.jpg (<= 3840 px, <= 4 MB) and gallery/<name>_small.jpg: the render
re-annotated at 1280 px with the scene's `small` label settings (fewer, larger labels for the README).
Outputs follow CHINAMAPS_OUT (the Blender scripts get an explicit --out).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from chinamaps.net import OUT_DIR, REPO_ROOT

BLENDER_PY = REPO_ROOT / ".venv-blender" / "bin" / "python"
RENDER_DIR = OUT_DIR / "renders"
GALLERY = REPO_ROOT / "gallery"
SMALL_WIDTH = 1280


def _blender(script: str, *args: str) -> int:
    cmd = [str(BLENDER_PY), str(REPO_ROOT / "blender" / script), *args]
    return subprocess.call(cmd)


def _render_args(a: argparse.Namespace, stem: str) -> list[str]:
    out = ["--out", str(RENDER_DIR / f"{stem}.png")] + (["--draft"] if a.draft else [])
    for k in ("width", "height", "samples", "threads"):
        v = getattr(a, k, None)
        if v is not None:
            out += [f"--{k}", str(v)]
    return out


def _annotate(stem: str, scene_json: Path, gallery_name: str | None) -> Path:
    from chinamaps.annotate import annotate, gallery_jpeg

    src, labels = RENDER_DIR / f"{stem}.png", RENDER_DIR / f"{stem}_labels.json"
    out = annotate(src, labels, scene_json, RENDER_DIR / f"{stem}_labeled.png")
    if gallery_name:
        print(f"gallery: {gallery_jpeg(out, GALLERY / f'{gallery_name}.jpg', small_width=None)}")
        small = annotate(src, labels, scene_json, RENDER_DIR / f"{stem}_small_labeled.png", width=SMALL_WIDTH,
                         small=True)
        print(f"gallery: {gallery_jpeg(small, GALLERY / f'{gallery_name}_small.jpg', max_width=SMALL_WIDTH,
                                       quality=88, small_width=None, max_bytes=None)}")
    return out


def render_scene(a: argparse.Namespace) -> int:
    scene_dir = OUT_DIR / "scenes" / a.name
    if not a.no_labels:
        from chinamaps.annotate import prepare_labels

        prepare_labels(scene_dir)
    else:
        (scene_dir / "labels_proj.json").unlink(missing_ok=True)
    stem = f"{a.name}_draft" if a.draft else a.name
    rc = _blender("render_scene.py", str(scene_dir), *_render_args(a, stem))
    if rc == 0:
        _annotate(stem, scene_dir / "scene.json", None if a.draft else a.name)
    return rc


def render_city(a: argparse.Namespace) -> int:
    city_dir = OUT_DIR / "cities" / a.name
    stem = f"city-{a.name}_draft" if a.draft else f"city-{a.name}"
    rc = _blender("render_city.py", str(city_dir), *_render_args(a, stem))
    if rc == 0:
        _annotate(stem, city_dir / "city.json", None if a.draft else f"city-{a.name}")
    return rc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chinamaps")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("smoke")
    for name in ("scene", "render", "render-city", "annotate"):
        s = sub.add_parser(name)
        s.add_argument("name")
        s.add_argument("--draft", action="store_true")
        if name in ("render", "render-city"):
            for k in ("width", "height", "samples", "threads"):
                s.add_argument(f"--{k}", type=int)
        if name == "render":
            s.add_argument("--no-labels", action="store_true")
    s = sub.add_parser("city")
    s.add_argument("name")
    sub.add_parser("webdata")
    a = p.parse_args(argv)

    if a.cmd == "smoke":
        from chinamaps.smoke import run

        return run()
    if a.cmd == "scene":
        from chinamaps.scene import build_scene

        build_scene(a.name, draft=a.draft)
        return 0
    if a.cmd == "render":
        return render_scene(a)
    if a.cmd == "annotate":
        stem = f"{a.name}_draft" if a.draft else a.name
        city = a.name.startswith("city-")
        src = OUT_DIR / ("cities" if city else "scenes") / a.name.removeprefix("city-")
        _annotate(stem, src / ("city.json" if city else "scene.json"), None if a.draft else a.name)
        return 0
    if a.cmd == "city":
        from chinamaps.city import build_city

        build_city(a.name)
        return 0
    if a.cmd == "render-city":
        return render_city(a)
    if a.cmd == "webdata":
        from chinamaps.webdata import build_webdata

        build_webdata()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
