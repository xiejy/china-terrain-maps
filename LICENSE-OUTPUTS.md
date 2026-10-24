# Licences for code and outputs

This repository holds two kinds of material, and they are licensed differently.

## Code: MIT

The source code is licensed under the MIT License; see [LICENSE](LICENSE). That covers:
- the `chinamaps` Python package (`src/`);
- the Blender scripts (`blender/`);
- the web application (`web/src/`, `web/scripts/`, `web/*.html`);
- the tests;
- the configuration files (`config/`).

You may reuse the code for any purpose, including commercial use.

## Outputs: CC BY-NC 4.0

The generated outputs are licensed under the
[Creative Commons Attribution-NonCommercial 4.0 International licence](https://creativecommons.org/licenses/by-nc/4.0/)
(CC BY-NC 4.0). They are:
- **rendered images:** `gallery/*.jpg`, `web/public/gallery/*.jpg`, and every render the pipeline
  writes to `out/renders/`;
- **3D models:** the city GLBs (`out/cities/<name>/city.glb`, `city_web.glb`,
  `web/public/cities/<name>.glb`) and the building table `buildings.geoparquet`;
- **generated tiles and web data:** `web/public/data/china.pmtiles`,
  `web/public/cities/*_buildings.pmtiles`, and the other files under `web/public/` that
  `chinamaps webdata` writes. The one exception is the glyph PBFs in `web/public/fonts/`, which stay
  under the SIL Open Font License (see [ATTRIBUTION.md](ATTRIBUTION.md#fonts)).

**Why non-commercial.** The building heights, and in Chongqing many of the footprints, come from
[GlobalBuildingAtlas](ATTRIBUTION.md#globalbuildingatlas-lod1). GlobalBuildingAtlas is licensed under
CC BY-NC 4.0, so every output that contains them must stay non-commercial too. That covers both city
models, their renders and their building tiles.

The terrain renders and `china.pmtiles` contain no GlobalBuildingAtlas data. Their inputs (Copernicus
DEM, GEBCO, Natural Earth) allow commercial use with attribution. The project still releases them under
the same CC BY-NC 4.0 terms, so that all outputs share one licence. You can rebuild them from the MIT
code and the upstream data under those sources' own terms.

**Attribution.** When you share an output, credit this project and keep the data credits shown on the
image or in the file. For city outputs, that means:
- © OpenStreetMap contributors (ODbL) via Overture Maps;
- Shi et al. 2023;
- GlobalBuildingAtlas (CC BY-NC 4.0);
- GEDTM30;
- Copernicus Sentinel data.

For terrain outputs, it means Copernicus DEM, GEBCO 2026 and Natural Earth. The full credit texts are in
[ATTRIBUTION.md](ATTRIBUTION.md).

## OpenStreetMap-derived geometry: ODbL as well

The building footprints, building parts, roads and water areas in the city outputs come largely from
OpenStreetMap, through Overture Maps. Overture distributes that data under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/), and those obligations
apply in addition to CC BY-NC 4.0:

- **Attribution.** Anything made from this data must show "© OpenStreetMap contributors". That covers
  images, models and maps, which the ODbL calls Produced Works.
- **Share-alike.** `buildings.geoparquet`, the `*_buildings.pmtiles` tiles and the building meshes in
  the city GLBs contain the geometry itself, so they are Derivative Databases. If you publicly use them,
  or a Produced Work made from them, you must offer the database itself under the ODbL.

The two licences pull in different directions. The ODbL requires the derived database to be shared
under the ODbL, while CC BY-NC 4.0 restricts the GlobalBuildingAtlas part to non-commercial use. In
practice, use the city data only for personal, non-commercial work, and keep both credits. If you need
clearer terms, rebuild the city models without GlobalBuildingAtlas. That needs a code change, because
there is no switch for it today. The result contains only data under ODbL, CC BY 4.0 and the Copernicus
terms.

## Boundaries

All outputs draw boundaries from the PRC point of view by choice. They are not official standard maps
and have no review number (审图号). See
[the disclaimer in the README](README.md#boundaries-and-disclaimer).

*This summary is not legal advice. The licence texts linked above govern.*
