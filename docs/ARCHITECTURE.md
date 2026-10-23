# Architecture and data contracts

Four independent tracks meet at file-based contracts under `out/` (gitignored)
and `web/public/` (committed, small).

```
config/*.yaml ──► chinamaps scene <name> ──► out/scenes/<name>/ ──► blender/render_scene.py ──► out/renders/<name>.png
                                                                                         └─► chinamaps.annotate ──► gallery/
config/cities.yaml ──► chinamaps city <name> ──► out/cities/<name>/ ──► blender/render_city.py ──► out/renders/city-<name>.png
                                                       └──────────────► web/public/cities/
Natural Earth + labels.yaml ──► chinamaps webdata ──► web/public/data/china.pmtiles ──► web/ (Vite + MapLibre)
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
| `scene.json` | The merged config (defaults + scene) plus `crs`, `bounds_proj` `[xmin, ymin, xmax, ymax]` in metres, `dem_shape`, `texture_shape`, `z_min`, `z_max`, and `center_lonlat`. |

Renders go to `out/renders/<name>.png` (final) or `out/renders/<name>_draft.png`. The renderer also
writes `out/renders/<name>_labels.json`, which holds the screen positions of `config/labels.yaml`
features. `chinamaps.annotate` composites those labels in CJK type.

## City contract: `out/cities/<name>/`
Written by `chinamaps.city.build_city(name)`, read by `blender/render_city.py` and the web viewer.

| file | content |
|---|---|
| `city.glb` | glTF 2.0, metres, **Y-up**. Local frame: x = easting − origin_e, y = elevation (m above sea level), z = −(northing − origin_n). Nodes are `terrain` (ground mesh, textured with the embedded ground image) and `buildings` (LoD1 extrusions). |
| `city_web.glb` | The same model compressed for the browser (gltfpack/meshopt, target ≤ 8 MB). |
| `buildings.geoparquet` | EPSG:4326 footprints with `id`, `height`, `height_source` (`overture_height`, `overture_floors`, `gba`, or `default`), `base_z`, `class`, and `source_dataset`. |
| `ground.png` | Ground texture (Sentinel-2 RGB plus water and roads) covering the bbox. |
| `city.json` | `name`, `title`, `epsg`, `bbox`, `origin` `[e, n]`, `terrain_res`, `n_buildings`, `height_source_counts`, `z_min`, `z_max`, plus the config `camera`/`sun`. |

## Web contract: `web/public/`
- `data/china.pmtiles`: a vector tileset with layers `boundary` (property `china`), `scs_dash`,
  `province`, `river`, `lake`, `place`, and `label` (zh/en physical features).
- `cities/<name>.glb` and `cities/<name>.json` are copies of `city_web.glb` and `city.json`.
  `cities/<name>_buildings.pmtiles` holds layer `building` with properties `height` and `base_z`.
- `scenes.json` holds viewpoints for the oblique scenes (`center`, `zoom`, `bearing`, `pitch`).
- The DEM is not hosted. The browser reads Mapterhorn `planet.pmtiles` on source.coop (falling back
  to AWS terrarium tiles), and both send CORS `*`.

## Worldview
`config` `worldview: cn` uses `chinamaps.sources.naturalearth.countries("cn")`. That is Natural
Earth's China point-of-view layer with CHN, HKG, MAC and SCR merged, plus `scs_dashes()` for the
South China Sea dash line.
