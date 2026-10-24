# Attribution

This project is built entirely from open data and open-source software. This page lists every
source: what it is used for, its licence, and the credit it requires.

Credits also appear in the outputs themselves:
- **Terrain renders**: printed in the lower right of the image.
- **City renders**: printed in the lower right of the image.
- **City GLBs**: in `asset.copyright`.
- **City JSON**: in the `attribution` field.
- **Web map**: in the attribution control.

How the outputs themselves are licensed is in [LICENSE-OUTPUTS.md](LICENSE-OUTPUTS.md).

## Summary

| source | used for | licence |
|---|---|---|
| [Copernicus DEM](#copernicus-dem-glo-30-and-glo-90) GLO-30 / GLO-90 | land elevation in all terrain scenes; fallback city terrain | Copernicus DEM licence (free, attribution required) |
| [GEBCO 2026 Grid](#gebco-2026-grid) | sea-floor depths (`china` scene) | public domain, attribution required |
| [GEDTM30](#gedtm30) | bare-earth terrain of the city models | CC BY 4.0 |
| [Natural Earth](#natural-earth) | coastline, boundaries, South China Sea dashes, provinces, rivers, lakes, glaciers, places, label anchors | public domain |
| [Overture Maps](#overture-maps-and-openstreetmap) | building footprints, building parts, roads, water and land use in the city models | ODbL (buildings, transportation and base themes) |
| [Shi et al. 2023 East Asia buildings](#shi-et-al-2023-east-asia-buildings-via-overture) | machine-learned footprints (via Overture) | CC BY 4.0 |
| [GlobalBuildingAtlas LoD1](#globalbuildingatlas-lod1) | building heights; footprints in Chongqing | **CC BY-NC 4.0** |
| [Sentinel-2 quarterly mosaics](#sentinel-2-quarterly-cloudless-mosaics) | ground imagery of the city models | Copernicus Sentinel data legal notice (free, full and open) |
| [Mapterhorn](#mapterhorn) | web-map elevation (default) | open terrain data, see their attribution page; code BSD-3-Clause |
| [AWS Terrain Tiles](#aws-terrain-tiles) | web-map elevation (fallback, with bathymetry) | per source; attribution required |
| [Noto fonts and Protomaps glyphs](#fonts) | label type in renders and on the web map | SIL Open Font License 1.1 |

## Elevation

### Copernicus DEM (GLO-30 and GLO-90)

- **What:** the global 30 m (1″) and 90 m (3″) digital surface models, read as 1° × 1° Cloud-Optimized
  GeoTIFFs from the AWS Open Data buckets `copernicus-dem-30m` and `copernicus-dem-90m`
  ([registry](https://registry.opendata.aws/copernicus-dem/)). The AWS copy is the **2021 release**.
- **Used in:** every terrain scene. It is also the fallback for city terrain if GEDTM30 cannot be read.
  Through Mapterhorn, it also feeds the web map.
- **Licence:** the Copernicus DEM licence. Use is free of charge and worldwide, including commercial
  use, provided the source is credited.
- **Required credit:**

  > © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the
  > European Union and ESA; all rights reserved.

  The licence's full form is "produced using Copernicus WorldDEM-30" (or "WorldDEM-90"), followed by
  the notice above.

### GEBCO 2026 Grid

- **What:** the global 15″ terrain model for ocean and land. It is read from the Cloud-Optimized
  GeoTIFF mirror on Source Cooperative (`ausantarctic/gebco/GEBCO_2026.tif`), which the Australian
  Antarctic Division maintains.
- **Used in:** the sea-floor depths of the `china` scene.
- **Licence:** the GEBCO Grid is in the public domain and may be used free of charge. The terms of use
  ask users to:
  - acknowledge the source;
  - not suggest that GEBCO endorses the work;
  - not use the grid for navigation.
- **Required credit:**

  > GEBCO Compilation Group (2026) GEBCO 2026 Grid
  > (doi:[10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa](https://doi.org/10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa))

### GEDTM30

- **What:** the Global Ensemble Digital Terrain Model at 30 m. It is a bare-earth model, so buildings
  and canopy are removed. It is read from walkthru.earth's Cloud-Optimized GeoTIFF on Source Cooperative
  (`walkthru-earth/dem-terrain/GEDTM30/gedtm30.tif`).
- **Used in:** the terrain of both city models (Chongqing and Hong Kong).
- **Licence:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), by OpenGeoHub. The
  Source Cooperative copy is also CC BY 4.0, by walkthru.earth.
- **Credit:**

  > GEDTM30, OpenGeoHub, CC BY 4.0. Ho, Y.-F., Grohmann, C. H., Lindsay, J., Reuter, H. I., Parente, L.,
  > Witjes, M., & Hengl, T. (2025). GEDTM30: global ensemble digital terrain model at 30 m and derived
  > multiscale terrain variables. *PeerJ* 13, e19673,
  > doi:[10.7717/peerj.19673](https://doi.org/10.7717/peerj.19673). Dataset:
  > doi:[10.5281/zenodo.14900181](https://doi.org/10.5281/zenodo.14900181).

## Vectors

### Natural Earth

- **What:** Natural Earth v5.1.2, 1:10M. Sixteen GeoJSON layers are read from the `nvkelso/natural-earth-vector`
  repository. They include:
  - the **China point-of-view** admin-0 countries layer, with China, Hong Kong, Macau and Scarborough
    Shoal merged;
  - the China-view maritime indicator, which gives the South China Sea dashes;
  - land, coastline and minor islands;
  - admin-1 provinces;
  - rivers, lakes and glaciated areas;
  - geography regions, elevation points and marine areas;
  - populated places.
- **Used in:** coastlines and the land mask, all boundaries and the dash line, provinces, rivers (in the
  national render and on the web map), lakes and glaciers, places, and many label anchors in
  `config/labels.yaml`.
- **Licence:** public domain ([terms of use](https://www.naturalearthdata.com/about/terms-of-use/)).
  No credit is required. We credit it anyway: *Made with Natural Earth.*

### Overture Maps and OpenStreetMap

- **What:** the Overture Maps Foundation release `2026-10-21.0`. It is read as GeoParquet over HTTPS
  from the `overturemaps-us-west-2` bucket. Types used:
  - `buildings/building` and `buildings/building_part`;
  - `transportation/segment` (roads);
  - `base/water` and `base/land_use`.
- **Used in:** building footprints and parts, road and water painting on the ground texture, water
  masks, and land use (which picks a default building class). This data ends up in the city GLBs, in
  `buildings.geoparquet` and in the `*_buildings.pmtiles` web tiles.
- **Licence:** the buildings, transportation and base themes are licensed under the
  [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). Most of this data
  comes from OpenStreetMap. Overture lists its full per-theme sources on its
  [attribution page](https://docs.overturemaps.org/attribution/).
- **Required credit:**

  > © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, Overture Maps Foundation

### Shi et al. 2023 East Asia buildings (via Overture)

- **What:** machine-learned building footprints for East Asia. They arrive through Overture's buildings
  theme, and the data's `sources` column records them as dataset `doi:10.5281/zenodo.8174931`.
  - *Chongqing*: 19,986 of the 23,052 Overture buildings in the area come from this dataset, and 5,186
    remain in the final model.
  - *Hong Kong*: 1,517 of 12,998 come from it, and 860 remain.
- **Licence:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- **Required credit:**

  > Qian Shi, et al. A First High-quality Vector Data of Buildings in East Asian Countries Based on a
  > Comprehensive Large-scale Mapping Framework. Zenodo, 22 July 2023,
  > doi:[10.5281/zenodo.8174931](https://doi.org/10.5281/zenodo.8174931). CC BY 4.0.

### GlobalBuildingAtlas LoD1

- **What:** GlobalBuildingAtlas (GBA) LoD1 building polygons with heights, from the Technical University
  of Munich (TUM). They are read from TGE Labs' GeoParquet conversion on Source Cooperative
  ([`tge-labs/globalbuildingatlas-lod1`](https://source.coop/tge-labs/globalbuildingatlas-lod1)), using
  5° tiles `e105_n30_e110_n25` and `e110_n25_e115_n20`.
- **Used in:**
  - building heights in both cities (7,835 buildings in Chongqing, 4,780 in Hong Kong);
  - footprints in Chongqing, where they replace weaker machine-learned ones (5,280 from TUM's own
    "ours2" set and 223 from OSM).
- **Licence:** [**CC BY-NC 4.0**](https://creativecommons.org/licenses/by-nc/4.0/). This licence is
  non-commercial, and it is why the city outputs are non-commercial (see
  [LICENSE-OUTPUTS.md](LICENSE-OUTPUTS.md)). GBA itself compiles footprints from several sources,
  including OpenStreetMap, so the GBA "osm" footprints are also OSM-derived.
  The GBA authors publish the ODbL-derived polygons separately, and they warn that combining the parts
  may have licence implications.
- **Required credit:** the authors ask for the paper to be cited, and the dataset has its own DOI.

  > Zhu, X. X., Chen, S., Zhang, F., Shi, Y., & Wang, Y. (2025). GlobalBuildingAtlas: an open global and
  > complete dataset of building polygons, heights and LoD1 3D models. *Earth System Science Data* 17,
  > 6647–6668, doi:[10.5194/essd-17-6647-2025](https://doi.org/10.5194/essd-17-6647-2025). Dataset:
  > Technical University of Munich, doi:[10.14459/2025mp1782307](https://doi.org/10.14459/2025mp1782307).
  > CC BY-NC 4.0. Code and documentation: <https://github.com/zhu-xlab/GlobalBuildingAtlas>.

## Imagery

### Sentinel-2 quarterly cloudless mosaics

- **What:** the Copernicus Sentinel-2 Global Mosaics (product `S2MSI_L3__MCQ`). ESA produces them and
  the Copernicus Data Space Ecosystem publishes them. They are read from Taylor Geospatial Engine's
  mirror on Source Cooperative
  ([`tge-labs/sentinel-2-quarterly-cloudless-mosaics`](https://source.coop/tge-labs/sentinel-2-quarterly-cloudless-mosaics)).
  The quarters used are 2025 Q4 for Chongqing (tile 48RXT) and 2026 Q1 for Hong Kong (tile 50QKK).
- **Used in:** the ground texture of the city models, upsampled to 2 m.
- **Licence:** the
  [Legal Notice on the use of Copernicus Sentinel Data and Service Information](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice).
  It allows free, full and open use, including commercial use and redistribution.
- **Required credit:**

  > Contains modified Copernicus Sentinel data 2025 (Chongqing) and 2026 (Hong Kong)

## Web-map elevation (streamed by the browser, not redistributed)

### Mapterhorn

- **What:** Mapterhorn's global terrain tiles (`planet.pmtiles`, terrarium-encoded WebP). They are read
  from Source Cooperative (`mapterhorn/mapterhorn/planet.pmtiles`).
- **Licence:** the terrain is compiled from many open DEMs, with the Copernicus DEM among them. The full
  source list and the credits each source needs are at <https://mapterhorn.com/attribution>. The
  Mapterhorn code is BSD-3-Clause (<https://github.com/mapterhorn/mapterhorn>).
- **Credit:** the web map's attribution control credits "Mapterhorn (Copernicus DEM etc.)" and links to
  that page.

### AWS Terrain Tiles

- **What:** the Mapzen / Tilezen terrarium tiles on AWS Open Data
  ([registry](https://registry.opendata.aws/terrain-tiles/)). They are used when Mapterhorn fails, or with
  `?dem=aws`. They include bathymetry.
- **Credit:** use the
  [Tilezen joerd attribution](https://github.com/tilezen/joerd/blob/master/docs/attribution.md). For
  China and the surrounding seas, the relevant lines are:

  > Global ETOPO1 terrain data U.S. National Oceanic and Atmospheric Administration; global GMTED2010
  > and SRTM terrain data courtesy of the U.S. Geological Survey.

## Fonts

- **Noto Serif SC, Noto Sans SC and Noto Serif.** These are Google's Noto fonts, under the
  [SIL Open Font License 1.1](https://openfontlicense.org/). They are downloaded from fonts.gstatic.com at
  build time and used for the labels, titles and credits drawn into the renders. The fonts are not
  redistributed; only images set in them are.
- **WenQuanYi Zen Hei** (GPL v2 with a font-embedding exception). This is a local fallback, used only if
  the Noto download fails. None of the gallery images use it.
- **Protomaps glyphs.** Signed-distance-field glyph PBFs for Noto Sans Regular, Medium and Italic come
  from [protomaps/basemaps-assets](https://github.com/protomaps/basemaps-assets) under the SIL Open Font
  License. They are committed under `web/public/fonts/` and set the Latin labels on the web map. The
  browser draws the Chinese labels with local system fonts.

## Hosting

Several datasets are read from **Source Cooperative**, which Radiant Earth runs on AWS Open Data. The
mirrors are maintained by:
- Taylor Geospatial Engine (GlobalBuildingAtlas, Sentinel-2);
- walkthru.earth (GEDTM30);
- the Australian Antarctic Division (GEBCO);
- Mapterhorn.

The Copernicus DEM, Overture and Terrain Tiles buckets are part of the AWS Open Data Sponsorship Program.

## Software

| software | licence | role |
|---|---|---|
| [MapLibre GL JS](https://github.com/maplibre/maplibre-gl-js) 6.15.0 | BSD-3-Clause | web map |
| [PMTiles](https://github.com/protomaps/PMTiles) 4.5.0 | BSD-3-Clause | single-file vector tiles, read in the browser |
| [three.js](https://github.com/mrdoob/three.js) 0.186.1 | MIT | 3D city viewer |
| [Vite](https://vite.dev) 8.3.4 and [TypeScript](https://www.typescriptlang.org) 5.9.3 | MIT; Apache-2.0 | web build |
| [Playwright](https://playwright.dev) 1.56.1 | Apache-2.0 | screenshot test only |
| [Blender](https://www.blender.org) as the `bpy` 5.2.2 module | GNU GPL (the wheel declares GPL-3.0) | Cycles rendering. The scripts in `blender/` are MIT-licensed, which is GPL-compatible. |
| [GDAL](https://gdal.org) 3.12 via [rasterio](https://github.com/rasterio/rasterio) 1.5.2 | MIT; BSD-3-Clause | raster I/O |
| [tippecanoe](https://github.com/felt/tippecanoe) 2.72 | BSD-2-Clause | vector tiles |
| [gltfpack / meshoptimizer](https://github.com/zeux/meshoptimizer) 1.3.0 | MIT | web GLB compression |
| [texshade](https://github.com/fasiha/texshade) 2.0 | Unlicense | texture shading |
| numpy, scipy, matplotlib, shapely, pyproj, geopandas, pyogrio, pyarrow, trimesh, pygltflib, mapbox-earcut, pillow, pyyaml, requests | BSD / MIT / Apache-2.0 / ISC / PSF-style | Python stack |

The project's own code is MIT ([LICENSE](LICENSE)).
