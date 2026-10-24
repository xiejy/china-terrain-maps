/** China 3D terrain map (index.html). */
import * as maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import './styles.css';
import {
  ATTRIBUTION, DEM_LABELS, PALETTE_LABELS, PALETTES, demFromQuery, initMapLibre, LOCAL_CJK_FONTS, reliefLayer, reliefStops, watchDemErrors,
  type DemKey, type PaletteKey,
} from './lib/dem';
import { buildInsetStyle, buildStyle, GROUP_LABELS, GROUP_LAYERS, textFields, type GroupKey, type MapState } from './lib/chinaStyle';
import { registerIcons } from './lib/icons';
import { setupGallery } from './lib/gallery';
import { setupPanelToggle, fetchJson, foldAttributionOnMobile, type CityEntry, cityLinks } from './lib/ui';

declare global {
  interface Window {
    __mapIdle?: boolean;
    __map?: maplibregl.Map;
  }
}

interface Scene {
  name: string;
  title: { zh: string; en: string };
  center: [number, number];
  zoom: number;
  bearing: number;
  pitch: number;
  exaggeration?: number;
}

// The landing view frames the mainland and Hainan; the South China Sea islands (south to Zengmu Ansha,
// ~4 N) are shown complete in the 南海诸岛 inset, as on the PRC standard national map.
const CHINA_BOUNDS: maplibregl.LngLatBoundsLike = [[73.5, 15.5], [135.0, 53.6]];
const SCS_BOUNDS: maplibregl.LngLatBoundsLike = [[105.5, 2.5], [122.5, 23.5]];
const INSET_MAX_ZOOM = 5.2; // the inset hides once the main map is zoomed in past the national view
const REF_VIEWPORT = [1280, 800]; // scenes.json zooms are computed for this size

// Default palette: the Blender render tints (palettes.yaml land/ocean); 'web' is web_color_relief.
const DEFAULT_PALETTE: PaletteKey = 'render';
const qPalette = new URLSearchParams(location.search).get('palette');

const state: MapState = {
  dem: demFromQuery(),
  palette: qPalette === 'web' || qPalette === 'render' ? qPalette : DEFAULT_PALETTE,
  terrain: true,
  exaggeration: 1.5,
  english: true,
  groups: { relief: true, hillshade: true, water: true, boundaries: true, provinces: true, places: true, physical: true },
};

initMapLibre();
const desktop = window.innerWidth > 640;
const map = new maplibregl.Map({
  container: 'map',
  style: buildStyle(state),
  bounds: CHINA_BOUNDS,
  fitBoundsOptions: { padding: desktop ? { top: 30, bottom: 30, left: 330, right: 30 } : 10 },
  pitch: 40,
  maxPitch: 85,
  localIdeographFontFamily: LOCAL_CJK_FONTS,
  attributionControl: { compact: true, customAttribution: ATTRIBUTION },
});
window.__map = map;
registerIcons(map);
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
// 'load' fires once; code that runs later (e.g. after fetching scenes.json) awaits this instead
const mapLoaded = new Promise<void>((resolve) => map.once('load', () => resolve()));
void mapLoaded.then(() => foldAttributionOnMobile(map.getContainer()));

// ---- 南海诸岛 inset: a small static map of the South China Sea islands and the dash line ----
const insetBox = document.getElementById('scs-inset')!;
const inset = new maplibregl.Map({
  container: insetBox.querySelector<HTMLElement>('.scs-map')!,
  style: buildInsetStyle(state),
  bounds: SCS_BOUNDS,
  fitBoundsOptions: { padding: 2 },
  interactive: false,
  attributionControl: false,
  fadeDuration: 0,
  localIdeographFontFamily: LOCAL_CJK_FONTS,
});
registerIcons(inset);
(window as unknown as { __inset: maplibregl.Map }).__inset = inset;
const syncInset = () => insetBox.classList.toggle('hidden', map.getZoom() >= INSET_MAX_ZOOM);
map.on('zoomend', syncInset);
insetBox.onclick = () => map.fitBounds(SCS_BOUNDS, { pitch: 0, bearing: 0, padding: desktop ? { top: 30, bottom: 30, left: 330, right: 30 } : 10 });
// 'idle' can fire in the same frame as 'load', so listen for it synchronously inside the load handler
const mainIdle = new Promise<void>((resolve) => map.once('load', () => map.once('idle', () => resolve())));
const insetIdle = new Promise<void>((resolve) => inset.once('idle', () => resolve()));
void Promise.all([mainIdle, insetIdle]).then(() => { window.__mapIdle = true; });

// ---- DEM source (primary Mapterhorn PMTiles, fallback AWS terrarium) ----
const demStatus = document.getElementById('dem-status')!;
function setDem(key: DemKey, note = ''): void {
  state.dem = key;
  map.setStyle(buildStyle(state));
  inset.setStyle(buildInsetStyle(state));
  renderDemButtons();
  demStatus.textContent = note || (key === 'mapterhorn' ? '512 px WebP · terrarium · z0–12' : '256 px PNG · terrarium · z0–15');
}
function renderDemButtons(): void {
  const box = document.getElementById('dem')!;
  box.replaceChildren(...(Object.keys(DEM_LABELS) as DemKey[]).map((k) => {
    const b = document.createElement('button');
    b.textContent = DEM_LABELS[k];
    b.className = k === state.dem ? 'active' : '';
    b.onclick = () => k !== state.dem && setDem(k);
    return b;
  }));
}
renderDemButtons();
demStatus.textContent = state.dem === 'mapterhorn' ? '512 px WebP · terrarium · z0–12' : '256 px PNG · terrarium · z0–15';
watchDemErrors(map, () => state.dem, () => setDem('aws', 'Mapterhorn failed to load; switched to AWS Terrain Tiles.'));

// ---- relief palette ----
function renderPaletteButtons(): void {
  document.getElementById('palette')!.replaceChildren(...(Object.keys(PALETTE_LABELS) as PaletteKey[]).map((k) => {
    const b = document.createElement('button');
    b.textContent = PALETTE_LABELS[k];
    b.className = k === state.palette ? 'active' : '';
    b.onclick = () => {
      state.palette = k;
      const relief = reliefLayer(reliefStops(k, state.dem)).paint!['color-relief-color'];
      map.setPaintProperty('color-relief', 'color-relief-color', relief);
      inset.setPaintProperty('color-relief', 'color-relief-color', relief);
      renderPaletteButtons();
      renderLegend();
    };
    return b;
  }));
}
renderPaletteButtons();

// ---- terrain ----
const exag = document.getElementById('exag') as HTMLInputElement;
const exagOut = document.getElementById('exag-out')!;
const terrainBox = document.getElementById('terrain') as HTMLInputElement;
function applyTerrain(): void {
  exag.value = String(state.exaggeration);
  exagOut.textContent = `${state.exaggeration.toFixed(1)}×`;
  map.setTerrain(state.terrain ? { source: 'terrain-dem', exaggeration: state.exaggeration } : null);
}
exag.oninput = () => { state.exaggeration = Number(exag.value); applyTerrain(); };
terrainBox.onchange = () => { state.terrain = terrainBox.checked; applyTerrain(); };
exagOut.textContent = `${state.exaggeration.toFixed(1)}×`;

// ---- layer toggles ----
const layersBox = document.getElementById('layers')!;
for (const key of Object.keys(GROUP_LABELS) as GroupKey[]) {
  const label = document.createElement('label');
  label.className = 'check';
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.checked = state.groups[key];
  input.onchange = () => {
    state.groups[key] = input.checked;
    for (const id of GROUP_LAYERS[key]) if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', input.checked ? 'visible' : 'none');
  };
  const [zh, ...en] = GROUP_LABELS[key].split(' ');
  label.append(input, ` ${zh} `);
  const span = document.createElement('span');
  span.textContent = en.join(' ');
  label.append(span);
  layersBox.append(label);
}
const english = document.getElementById('english') as HTMLInputElement;
english.onchange = () => {
  state.english = english.checked;
  for (const [id, field] of Object.entries(textFields(state.english))) if (map.getLayer(id)) map.setLayoutProperty(id, 'text-field', field);
};

// ---- viewpoints ----
function viewportZoomOffset(): number {
  const w = map.getContainer().clientWidth - (desktop ? 320 : 0);
  return Math.log2(Math.max(0.25, Math.min(w / REF_VIEWPORT[0], map.getContainer().clientHeight / REF_VIEWPORT[1])));
}
/** Scene centres sit in the middle of the area right of the desktop panel. */
function scenePadding(): maplibregl.PaddingOptions | undefined {
  return desktop ? { left: 320, top: 0, right: 0, bottom: 0 } : undefined;
}
function flyToScene(s: Scene): void {
  if (s.exaggeration) { state.exaggeration = s.exaggeration; applyTerrain(); }
  map.flyTo({
    center: s.center, zoom: s.zoom + viewportZoomOffset(), bearing: s.bearing, pitch: s.pitch,
    padding: scenePadding(), duration: 5000, essential: true,
  });
}
const scenesReady = fetchJson<Scene[]>('scenes.json').then((scenes) => {
  const box = document.getElementById('scenes')!;
  if (!scenes?.length) { box.innerHTML = '<span class="muted">scenes.json not built</span>'; return; }
  box.replaceChildren(...scenes.map((s, i) => {
    const b = document.createElement('button');
    if (i === 0 && scenes.length % 2 === 1) b.className = 'wide';
    b.innerHTML = `<b></b><small></small>`;
    b.querySelector('b')!.textContent = s.title.zh;
    b.querySelector('small')!.textContent = s.title.en;
    b.title = `bearing ${s.bearing}°, pitch ${s.pitch}°`;
    b.onclick = () => flyToScene(s);
    return b;
  }));
  const q = new URLSearchParams(location.search).get('scene');
  const s = scenes.find((x) => x.name === q);
  // the map may have loaded already (warm cache, slow JSON): wait on the promise, not the event
  if (s) {
    void mapLoaded.then(() => {
      map.jumpTo({ center: s.center, zoom: s.zoom + viewportZoomOffset(), bearing: s.bearing, pitch: s.pitch, padding: scenePadding() });
      if (s.exaggeration) { state.exaggeration = s.exaggeration; applyTerrain(); }
      syncInset();
    });
  }
  return scenes;
});

// ---- gallery (Blender renders; the section stays hidden until gallery/index.json lists some) ----
void setupGallery(async (name) => {
  const s = (await scenesReady)?.find((x) => x.name === name);
  if (s) flyToScene(s);
});

// ---- cities ----
fetchJson<CityEntry[]>('cities/index.json').then((cities) => {
  document.getElementById('cities')!.replaceChildren(...cityLinks(cities));
});

// ---- legend ----
function renderLegend(): void {
  const land = PALETTES[state.palette].filter(([z]) => z >= -200);
  const lo = land[0][0], hi = land[land.length - 1][0];
  const pct = (z: number) => ((z - lo) / (hi - lo)) * 100;
  document.getElementById('legend-bar')!.style.background =
    `linear-gradient(to right, ${land.map(([z, c]) => `${c} ${pct(z).toFixed(1)}%`).join(', ')})`;
  document.getElementById('legend-ticks')!.replaceChildren(...[0, 2000, 4000, 6000].map((z) => {
    const s = document.createElement('span');
    s.style.left = `${pct(z)}%`;
    s.textContent = z === 0 ? '0 m' : `${z / 1000}k`;
    return s;
  }));
}
renderLegend();
setupPanelToggle();
