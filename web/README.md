# Web map

Static Vite + TypeScript site: a 3D terrain map of China (`index.html`) and 3D city pages
(`city.html?c=<name>&view=map|3d`). Deployed to GitHub Pages by `.github/workflows/pages.yml`.

```sh
.venv/bin/chinamaps webdata      # (repo root) regenerate web/public/ and src/generated/
cd web
npm ci                           # PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 if no browser download is wanted
npm run dev                      # http://localhost:5173
npm run build                    # -> dist/
npm run preview
npm run screenshot               # needs a build; PNGs -> ../out/web/, non-zero exit on console errors
```

## Data

| path | made by | notes |
|---|---|---|
| `public/data/china.pmtiles` | `chinamaps webdata` | Natural Earth, cn worldview: `boundary` (`china`), `scs_dash`, `province`, `province_label`, `river`, `lake`, `glacier`, `place`, `label` |
| `public/fonts/<fontstack>/<range>.pbf` | `chinamaps webdata` | Protomaps Noto Sans glyphs for Latin ranges; CJK is drawn with local fonts (`localIdeographFontFamily`) |
| `public/scenes.json` | `chinamaps webdata` | viewpoints of `config/scenes.yaml` (bearing = azimuth + 180, pitch = 90 − elevation) |
| `public/cities/index.json` | `chinamaps webdata` | configured cities and which assets exist |
| `public/cities/<name>.glb`, `.json`, `_buildings.pmtiles` | `chinamaps webdata` from `out/cities/<name>/` | meshopt GLB, city.json, building extrusions (z12–16) |
| `src/generated/color_relief.json` | `chinamaps webdata` | `web` (`web_color_relief`) and `render` (Blender tints) stops |
| `public/gallery/<stem>_small.jpg`, `<stem>_2560.jpg`, `index.json` | `chinamaps webdata` from `../gallery/` | web copies of the Blender renders: thumbnails (`<stem>_small.jpg` as is, else `<stem>.jpg` downsized to 1600 px) and 2560 px lightbox copies; `index.json` is `[]` until renders exist, and the gallery section stays hidden |

The DEM is not hosted: the map reads Mapterhorn `planet.pmtiles` on source.coop and falls back to
AWS Terrain Tiles after repeated errors (or with `?dem=aws`). The national view frames the mainland;
the South China Sea islands and the dash line are shown complete in the 南海诸岛 inset (bottom right,
a second non-interactive map; click it to fly there), which hides once you zoom in. Other URL parameters on
`index.html`: `scene=<name>` (start at a viewpoint), `palette=render|web`. `index.html#gallery` opens the
gallery overlay.

## Screenshot test

`scripts/screenshot.mjs` serves `dist/` (with HTTP Range support, which PMTiles needs), opens each
page in headless Chromium with WebGL2 on SwiftShader, waits for `window.__mapIdle` /
`window.__viewReady`, and saves PNGs to `out/web/`. Remote tiles go through `$HTTPS_PROXY`;
Chromium trusts the proxy CA through the NSS store, and `SCREENSHOT_IGNORE_HTTPS_ERRORS=1` relaxes
TLS for the harness only. SwiftShader is slow: a pitched terrain view can take 1–2 minutes to idle.
`--base /china-terrain-maps/` serves `dist/` only under that path (as on GitHub project pages), so
any absolute-path request 404s and fails the page. On a shared machine, pin it to a few cores, e.g.
`nice taskset -c 2,3 npm run screenshot`.
