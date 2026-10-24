# Architecture and data contracts

Four independent tracks meet at file-based contracts under `out/` (gitignored)
and `web/public/` and `gallery/` (committed, small).

```
config/*.yaml ──► chinamaps scene <name> ──► out/scenes/<name>/ ──► blender/render_scene.py ──► out/renders/<name>.png
                                                                                         └─► chinamaps.annotate ──► gallery/
config/cities.yaml ──► chinamaps city <name> ──► out/cities/<name>/ ──► blender/render_city.py ──► out/renders/city-<name>.png
                                                       └──────────────► web/public/cities/
Natural Earth + labels.yaml ──► chinamaps webdata ──► web/public/data/china.pmtiles ──► web/ (Vite + MapLibre)
gallery/ ─────────────────────► chinamaps webdata ──► web/public/gallery/
```

## Python environments
- `.venv` (Python 3.13): the `chinamaps` package and its pinned geo stack (see `pyproject.toml`).
- `.venv-blender` (Python 3.13): `bpy==5.2.2`, numpy, pillow, pyyaml. **No rasterio here**, so
  Blender scripts read only `.npy`, `.png` and `.json` files.

## Network rules (all code)
- Unsigned `https://` only. Rasters are read with `rasterio.open("/vsicurl/https://…")`. Never
  `/vsis3/`, boto3 or signed S3 clients: the `AWS_*` env vars are placeholders.
- source.coop data must use the **path-style** endpoint `chinamaps.net.SOURCE_COOP`
  (`https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop`).
- GeoParquet on S3 is read with `pyarrow.parquet.ParquetFile(chinamaps.net.HTTPRangeFile(url))`,
  pruning row groups by their `bbox` column statistics. DuckDB extensions and the Overture STAC
  are unreachable.
- Everything downloaded is cached under `chinamaps.net.DATA_DIR` (`data/`).

## Scene contract: `out/scenes/<name>/`
Written by `chinamaps.scene.build_scene(name, draft=False)`, read by `blender/render_scene.py`.

| file | content |
|---|---|
| `dem.npy` | float32 `(H, W)`, metres above sea level (negative = bathymetry), row 0 = north edge, regular grid in the scene CRS at `dem_res`. No NaNs: sea is bathymetry when `bathymetry: true`, else 0. |
| `dem.tif` | Same grid as GeoTIFF (for GIS inspection). |
| `texture.png` | RGB 8-bit `(Ht, Wt)` covering exactly the same extent as `dem.npy` at `texture_res`, north up. Shading is baked in softly; Blender adds the cast shadows. |
| `mask.png` | 8-bit `(H, W)`: 255 = land, 0 = sea. Used for a separate water material. |
| `scene.json` | The merged config (defaults + scene) plus `crs`, `bounds_proj` `[xmin, ymin, xmax, ymax]` in metres, `dem_shape`, `texture_shape`, `z_min`, `z_max`, and `center_lonlat` (also `land_fraction`, `hillshade_zfactor`, `build_seconds`). |
| `preview.png` | Small north-up preview of the texture (for inspection only). |
| `labels_proj.json` | Written at render time by `chinamaps.annotate.prepare_labels`: the `config/labels.yaml` features inside the scene, projected to the scene CRS with DEM elevations. Read by Blender; removed by `render --no-labels`. |

Renders go to `out/renders/<name>.png` (final) or `out/renders/<name>_draft.png`, flattened over a sky
gradient, next to `<stem>_rgba.png` (the transparent raw render). The renderer also
writes `out/renders/<name>_labels.json`, which holds the screen positions of `config/labels.yaml`
features, the camera and the vertical `exaggeration`. `chinamaps.annotate` composites those labels in
CJK type, with a north arrow and the exaggeration in the credits, into `<stem>_labeled.png` (placements
in `<stem>_labeled.placed.json`). Final renders also give
`gallery/<name>.jpg` (≤ 3840 px) and `gallery/<name>_small.jpg` (1280 px, re-annotated with the scene's
`small` label settings rather than downsampled). Like `chinamaps.net.OUT_DIR`, the Blender scripts
honour `CHINAMAPS_OUT`, and the CLI passes them an explicit `--out`.

## City contract: `out/cities/<name>/`
Written by `chinamaps.city.build_city(name)`, read by `blender/render_city.py` and the web viewer.

| file | content |
|---|---|
| `city.glb` | glTF 2.0, metres, **Y-up**. Local frame: x = easting − origin_e, y = elevation (m above sea level), z = −(northing − origin_n). Nodes are `terrain` (ground mesh, textured with the embedded ground image) and `buildings` (LoD1 extrusions). |
| `city_web.glb` | The same model compressed for the browser (gltfpack/meshopt, target ≤ 8 MB). |
| `buildings.geoparquet` | EPSG:4326 footprints with `id`, `name`, `height`, `height_source` (`overture_height`, `overture_floors`, `gba`, or `default`), `base_z`, `min_height`, `class`, `source_dataset` (`OpenStreetMap`, `doi:10.5281/zenodo.8174931`, `GlobalBuildingAtlas:<source>`, …), and `part_of` (parent id for building parts). |
| `ground.png` | Ground texture (Sentinel-2 RGB plus water and roads) covering the bbox. |
| `water_mask.png` | 8-bit, same grid as `ground.png`: 255 = water (smoothed Overture water polygons). Used by `render_city.py` for the water material; not a web asset. |
| `city.json` | `name`, `title`, `epsg`, `bbox`, `origin` `[e, n]`, `terrain_res`, `n_buildings`, `height_source_counts`, `z_min`, `z_max`, plus the config `camera`/`sun`, `landmarks`, `sources` (Overture release, GBA tile, DTM, Sentinel-2 year/quarter/tiles), `attribution` and `license_note`. |
| `preview.png` | Top-down preview of ground and buildings (for inspection only). |

## Web contract: `web/public/`
- `data/china.pmtiles`: a vector tileset with layers `boundary` (property `china`), `scs_dash`,
  `province`, `province_label`, `river`, `lake`, `glacier`, `place`, and `label` (zh/en physical
  features).
- `fonts/<fontstack>/<range>.pbf`: Protomaps Noto Sans glyphs for the non-CJK label characters (CJK is
  drawn with local fonts). `web/src/generated/color_relief.json` holds the relief ramps (web and render
  palettes).
- `cities/<name>.glb` and `cities/<name>.json` are copies of `city_web.glb` and `city.json`.
  `cities/<name>_buildings.pmtiles` holds layer `building` with properties `height` and `base_z`.
- `scenes.json` holds viewpoints for the oblique scenes (`center`, `zoom`, `bearing`, `pitch`).
- In layer `label`, each `points` anchor of a river in `config/labels.yaml` is an extra feature
  (`extra: true`, from z5); `web_minzoom` overrides a label's first zoom.
- `gallery/index.json` lists the renders that exist: `{stem, kind: scene|city, name, title{zh,en},
  src: gallery/<stem>_small.jpg, large: gallery/<stem>_2560.jpg | null, width, height, full: GitHub
  URL | null}`, next to those JPEGs (`chinamaps webdata` copies them from `gallery/`).
- `cities/index.json` carries the live `config/cities.yaml` camera; the 3D viewer prefers it over the
  build-time camera in `cities/<name>.json`.
- The DEM is not hosted. The browser reads Mapterhorn `planet.pmtiles` on source.coop (falling back
  to AWS terrarium tiles), and both send CORS `*`. Mapterhorn has no bathymetry (sea = 0 m), so the web
  relief ramp depends on the DEM (`reliefStops` in `web/src/lib/dem.ts`).

## Worldview
`config` `worldview: cn` uses `chinamaps.sources.naturalearth.countries("cn")`. That is Natural
Earth's China point-of-view layer with CHN, HKG, MAC and SCR merged, plus `scs_dashes()` for the
South China Sea dash line.

- The dash line is Natural Earth's 9-dash maritime indicator. The current PRC standard map (2013
  vertical edition onward) also draws a tenth dash east of Taiwan. It is deliberately not added: no
  authoritative digitisation is reachable from the build (bzdt.ch.mnr.gov.cn and DataV are blocked), and
  a hand-guessed line has no place in a worldview layer. Taiwan is drawn and labelled as part of China
  either way.
- A national map must show the South China Sea islands in full. The renders frame them on the slab. The
  web landing view covers the mainland and adds a 南海诸岛 inset (`web/src/main.ts`, bottom right).
- Web places follow the cn polygon: a place inside it is Chinese even if Natural Earth assigns it to
  another country, and a foreign admin-1 seat there (Itanagar) is not styled as a capital.
