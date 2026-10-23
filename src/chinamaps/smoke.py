"""Environment and data-access smoke tests (`chinamaps smoke`).

Writes out/smoke.json and returns a non-zero exit code if any check fails.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from chinamaps.net import OUT_DIR, REPO_ROOT, SOURCE_COOP, head_size, vsicurl

PLAYWRIGHT = Path("/opt/node22/lib/node_modules/playwright")
OVERTURE = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com"

WEBGL_JS = """
const { chromium } = require(process.argv[1]);
(async () => {
  const b = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader',
    '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
  const p = await b.newPage();
  const ok = await p.evaluate(() => !!document.createElement('canvas').getContext('webgl2'));
  console.log(ok ? 'webgl2' : 'none'); await b.close();
})();
"""

BPY_SNIPPET = """
import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.engine = 'CYCLES'; sc.cycles.device = 'CPU'; sc.cycles.samples = 4
sc.render.resolution_x = sc.render.resolution_y = 64
bpy.ops.mesh.primitive_cube_add()
cam = bpy.data.objects.new('cam', bpy.data.cameras.new('cam'))
sc.collection.objects.link(cam); sc.camera = cam; cam.location = (0, -6, 0); cam.rotation_euler = (1.5708, 0, 0)
sc.render.filepath = '/dev/null'
bpy.ops.render.render(write_still=False)
print('bpy', bpy.app.version_string)
"""


def _gdal():
    import rasterio

    return rasterio.__gdal_version__


def _glo90():
    import rasterio

    url = vsicurl(
        "https://copernicus-dem-90m.s3.amazonaws.com/Copernicus_DSM_COG_30_N30_00_E100_00_DEM/"
        "Copernicus_DSM_COG_30_N30_00_E100_00_DEM.tif"
    )
    with rasterio.open(url, OVERVIEW_LEVEL=0) as ds:
        a = ds.read(1)
    return f"{a.shape} min={a.min():.0f} max={a.max():.0f}"


def _gebco():
    import rasterio
    from rasterio.windows import from_bounds

    with rasterio.open(vsicurl(f"{SOURCE_COOP}/ausantarctic/gebco/GEBCO_2026.tif")) as ds:
        v = ds.read(1, window=from_bounds(114.99, 14.99, 115.01, 15.01, ds.transform))
    depth = float(v.mean())
    assert depth < -2000, depth
    return f"15N 115E depth {depth:.0f} m"


def _overture():
    import re

    import pyarrow.parquet as pq

    from chinamaps.net import HTTPRangeFile, session

    r = session().get(f"{OVERTURE}/?list-type=2&prefix=release/&delimiter=/", timeout=60)
    rel = sorted(re.findall(r"<Prefix>(release/[^<]+)</Prefix>", r.text))[-1]
    r = session().get(f"{OVERTURE}/?list-type=2&prefix={rel}theme=buildings/type=building/&max-keys=1", timeout=60)
    key = re.search(r"<Key>([^<]+)</Key>", r.text).group(1)
    url = f"{OVERTURE}/{key}"
    pf = pq.ParquetFile(HTTPRangeFile(url, head_size(url)))
    n = pf.read_row_group(0, columns=["id"]).num_rows
    return f"{rel} row group 0: {n} rows"


def _bpy():
    py = REPO_ROOT / ".venv-blender" / "bin" / "python"
    out = subprocess.run([str(py), "-c", BPY_SNIPPET], capture_output=True, text=True, timeout=300)
    line = [l for l in out.stdout.splitlines() if l.startswith("bpy ")]
    assert out.returncode == 0 and line, out.stderr[-500:]
    return line[0]


def _webgl():
    out = subprocess.run(
        ["node", "-e", WEBGL_JS, str(PLAYWRIGHT)], capture_output=True, text=True, timeout=120
    )
    assert "webgl2" in out.stdout, out.stdout + out.stderr[-500:]
    return "webgl2 via SwiftShader"


CHECKS = [
    ("gdal", _gdal),
    ("glo90_vsicurl", _glo90),
    ("gebco_window", _gebco),
    ("overture_rowgroup", _overture),
    ("bpy_cycles", _bpy),
    ("chromium_webgl2", _webgl),
]


def run() -> int:
    results, failed = {}, 0
    for name, fn in CHECKS:
        t = time.time()
        try:
            results[name] = {"ok": True, "detail": fn(), "seconds": round(time.time() - t, 1)}
        except Exception as e:  # report every check, fail at the end
            failed += 1
            results[name] = {"ok": False, "detail": repr(e)[:500], "seconds": round(time.time() - t, 1)}
        r = results[name]
        print(f"{'PASS' if r['ok'] else 'FAIL'} {name:20s} {r['seconds']:6.1f}s  {r['detail']}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "smoke.json").write_text(json.dumps(results, indent=2))
    return 1 if failed else 0
