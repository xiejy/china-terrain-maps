"""chinamaps command line.

    chinamaps smoke                      environment + data-access checks
    chinamaps scene <name> [--draft]     DEM + texture for an oblique scene
    chinamaps render <name> [--draft]    Blender Cycles render of a scene
    chinamaps city <name>                3D city model (GLB + footprints)
    chinamaps render-city <name> [--draft]
    chinamaps webdata                    vector PMTiles + web assets
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from chinamaps.net import OUT_DIR, REPO_ROOT

BLENDER_PY = REPO_ROOT / ".venv-blender" / "bin" / "python"


def _blender(script: str, *args: str) -> int:
    cmd = [str(BLENDER_PY), str(REPO_ROOT / "blender" / script), *args]
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chinamaps")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("smoke")
    for name in ("scene", "render", "render-city"):
        s = sub.add_parser(name)
        s.add_argument("name")
        s.add_argument("--draft", action="store_true")
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
        args = [str(OUT_DIR / "scenes" / a.name)] + (["--draft"] if a.draft else [])
        return _blender("render_scene.py", *args)
    if a.cmd == "city":
        from chinamaps.city import build_city

        build_city(a.name)
        return 0
    if a.cmd == "render-city":
        args = [str(OUT_DIR / "cities" / a.name)] + (["--draft"] if a.draft else [])
        return _blender("render_city.py", *args)
    if a.cmd == "webdata":
        from chinamaps.webdata import build_webdata

        build_webdata()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
