/** City page (city.html?c=<name>&view=map|3d): buildings on terrain in MapLibre, or the GLB in three.js. */
import * as maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import './styles.css';
import type { StyleSpecification } from 'maplibre-gl';
import {
  ATTRIBUTION, SKY, bearingFromAzimuth, demFromQuery, demSource, glyphsUrl, hillshadeLayer, initMapLibre,
  LOCAL_CJK_FONTS, reliefLayer, siteUrl, watchDemErrors, type DemKey, type ReliefStop,
} from './lib/dem';
import { registerIcons } from './lib/icons';
import { cityLinks, el, fetchJson, foldAttributionOnMobile, setupPanelToggle, type CityEntry } from './lib/ui';
import type { CityInfo, Viewer } from './lib/viewer3d';

declare global {
  interface Window {
    __viewReady?: boolean;
    __map?: maplibregl.Map;
  }
}

type View = 'map' | '3d';

const params = new URLSearchParams(location.search);
const name = params.get('c') ?? '';
let view: View = params.get('view') === '3d' ? '3d' : 'map';
let dem: DemKey = demFromQuery();

const HEIGHT_COLORS: [number, string][] = [
  [0, '#e9e3d6'], [15, '#ecdcbc'], [40, '#ecc48e'], [80, '#e6a064'], [150, '#d9744a'], [250, '#b84a3e'], [350, '#8e2c3c'],
];
const SOURCE_LABELS: Record<string, string> = {
  overture_height: 'Overture height',
  overture_floors: 'Overture floors',
  gba: 'GlobalBuildingAtlas',
  default: 'Default by class',
};

const notice = document.getElementById('notice')!;
function showNotice(title: string, body: string): void {
  notice.replaceChildren(el('h3', { textContent: title }), el('p', { innerHTML: body }));
  notice.classList.remove('hidden');
}
function hideNotice(): void {
  notice.classList.add('hidden');
}

/** Web Mercator zoom fitting a bbox in w x h pixels (512 px tiles). */
function fitZoom(b: [number, number, number, number], w: number, h: number): number {
  const y = (lat: number) => 0.5 - Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) / (2 * Math.PI);
  const dx = (b[2] - b[0]) / 360;
  const dy = y(b[1]) - y(b[3]);
  return Math.log2(Math.min(w / (512 * dx), h / (512 * dy)));
}

/** Colour relief stretched over the city's own elevation range (water below 0 m). Inland cities
 * also get a blue band at the bottom of the range, where the DEM's flattened rivers sit. */
function cityRelief(info: CityInfo | null): ReliefStop[] {
  const [lo, hi] = info?.terrain_z ?? [info?.z_min ?? 0, info?.z_max ?? 600];
  const base = Math.max(lo, 0.5);
  const span = Math.max(60, hi - base);
  const stops: ReliefStop[] = [[-60, '#5b8fb4'], [-1, '#8db8d2'], [0, '#a6c6d6']];
  let land = base;
  if (lo > 20) {
    const band = Math.max(6, 0.03 * span);
    stops.push([base, '#94bdd3'], [base + band * 0.6, '#a9c8d3']);
    land = base + band;
  }
  const s = Math.max(30, hi - land);
  stops.push(
    [land, '#bccfa8'], [land + 0.2 * s, '#cfd3a6'], [land + 0.45 * s, '#dccb9e'],
    [land + 0.7 * s, '#d8b891'], [land + s, '#efe4d1'],
  );
  return stops;
}

function cityStyle(entry: CityEntry, info: CityInfo | null): StyleSpecification {
  const style: StyleSpecification = {
    version: 8,
    glyphs: glyphsUrl(),
    sources: { 'terrain-dem': demSource(dem), 'relief-dem': demSource(dem) },
    sky: SKY,
    terrain: { source: 'terrain-dem', exaggeration: 1 },
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': '#d9d4c7' } },
      reliefLayer(cityRelief(info)),
      hillshadeLayer(0.35),
    ],
  };
  if (entry.buildings) {
    style.sources.buildings = {
      type: 'vector',
      url: `pmtiles://${siteUrl(`cities/${entry.name}_buildings.pmtiles`)}`,
    };
    style.layers.push({
      id: 'building', type: 'fill-extrusion', source: 'buildings', 'source-layer': 'building',
      paint: {
        // With terrain on, extrusions stand on the terrain surface, so base 0 and height = building height.
        'fill-extrusion-color': ['interpolate', ['linear'], ['get', 'height'], ...HEIGHT_COLORS.flat()] as never,
        'fill-extrusion-height': ['get', 'height'],
        'fill-extrusion-base': 0,
        'fill-extrusion-opacity': 0.94,
        'fill-extrusion-vertical-gradient': true,
      },
    });
  }
  if (entry.landmarks?.length) {
    style.sources.landmarks = {
      type: 'geojson',
      data: {
        type: 'FeatureCollection',
        features: entry.landmarks.map((l) => ({
          type: 'Feature', geometry: { type: 'Point', coordinates: [l.lon, l.lat] }, properties: { zh: l.zh ?? '', en: l.name },
        })),
      },
    };
    style.layers.push({
      id: 'landmark', type: 'symbol', source: 'landmarks',
      layout: {
        'icon-image': 'landmark',
        'text-field': ['format', ['get', 'zh'], {}, '\n', {}, ['get', 'en'], { 'font-scale': 0.75, 'text-font': ['literal', ['Noto Sans Regular']] }],
        'text-font': ['Noto Sans Medium'],
        'text-size': 14,
        'text-anchor': 'bottom',
        'text-offset': [0, -0.9],
        'icon-allow-overlap': true,
        'text-allow-overlap': true,
      },
      paint: { 'text-color': '#1c2128', 'text-halo-color': 'rgba(255,255,255,0.92)', 'text-halo-width': 1.6 },
    });
  }
  return style;
}

let map: maplibregl.Map | null = null;
let viewer: Viewer | null = null;

function initMap(entry: CityEntry, info: CityInfo | null): void {
  initMapLibre();
  const box = document.getElementById('map')!;
  const cam = entry.camera ?? {};
  const desktop = window.innerWidth > 640;
  const w = box.clientWidth - (desktop ? 320 : 0);
  map = new maplibregl.Map({
    container: box,
    style: cityStyle(entry, info),
    center: entry.center,
    zoom: fitZoom(entry.bbox, w, box.clientHeight) + 0.35,
    bearing: bearingFromAzimuth(cam.azimuth ?? 180),
    pitch: Math.min(85, Math.max(0, 90 - (cam.elevation ?? 35))),
    maxPitch: 85,
    localIdeographFontFamily: LOCAL_CJK_FONTS,
    attributionControl: { compact: true, customAttribution: ATTRIBUTION },
  });
  if (desktop) map.setPadding({ left: 320, top: 0, right: 0, bottom: 0 });
  window.__map = map;
  registerIcons(map);
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
  watchDemErrors(map, () => dem, () => {
    dem = 'aws';
    map?.setStyle(cityStyle(entry, info));
  });
  map.once('load', () => {
    foldAttributionOnMobile(box);
    map!.once('idle', () => { if (view === 'map') window.__viewReady = true; });
  });
}

async function init3d(entry: CityEntry, info: CityInfo | null): Promise<void> {
  const box = document.getElementById('three')!;
  showNotice('正在加载三维模型 Loading 3D model…', `cities/${entry.name}.glb`);
  try {
    const { createViewer } = await import('./lib/viewer3d');
    // cities/index.json carries the live config camera; cities/<name>.json the one at build time
    const viewInfo: CityInfo | null = info ? { ...info, camera: { ...info.camera, ...entry.camera } } : { camera: entry.camera };
    viewer = await createViewer(box, siteUrl(`cities/${entry.name}.glb`), viewInfo, (p) => {
      notice.querySelector('p')!.textContent = `cities/${entry.name}.glb · ${Math.round(p * 100)}%`;
    });
    hideNotice();
    viewer.start();
    await viewer.firstFrame();
  } catch (e) {
    console.error(e);
    showNotice('模型加载失败 Model failed to load', String(e));
  }
  if (view === '3d') window.__viewReady = true;
}

function setView(entry: CityEntry, info: CityInfo | null, v: View): void {
  view = v;
  window.__viewReady = false;
  for (const b of document.querySelectorAll<HTMLButtonElement>('#toolbar button')) b.classList.toggle('active', b.dataset.view === v);
  document.getElementById('map')!.classList.toggle('hidden', v !== 'map');
  document.getElementById('three')!.classList.toggle('hidden', v !== '3d');
  const url = new URL(location.href);
  url.searchParams.set('view', v);
  history.replaceState(null, '', url);
  hideNotice();
  if (v === 'map') {
    viewer?.stop();
    if (!entry.buildings) showNotice('建筑尚未生成 Buildings not built yet', `Run <code>chinamaps city ${entry.name}</code> then <code>chinamaps webdata</code>. Terrain is shown meanwhile.`);
    if (!map) initMap(entry, info);
    else { map.resize(); map.once('idle', () => { window.__viewReady = true; }); map.triggerRepaint(); }
    return;
  }
  if (!entry.glb) {
    showNotice('三维模型尚未生成 Model not built yet', `Run <code>chinamaps city ${entry.name}</code> then <code>chinamaps webdata</code> to create <code>cities/${entry.name}.glb</code>.`);
    window.__viewReady = true;
    return;
  }
  if (viewer) { viewer.start(); viewer.firstFrame().then(() => { window.__viewReady = true; }); }
  else void init3d(entry, info);
}

function renderInfo(info: CityInfo | null): void {
  const box = document.getElementById('info')!;
  if (!info) {
    box.replaceChildren(el('p', { className: 'muted small', textContent: 'city.json not built yet: building statistics will appear here.' }));
    return;
  }
  const counts = Object.entries(info.height_source_counts ?? {}).sort((a, b) => b[1] - a[1]);
  const total = counts.reduce((s, [, n]) => s + n, 0) || 1;
  const fmt = (n?: number) => (n === undefined ? '–' : n.toLocaleString('en-US', { maximumFractionDigits: 0 }));
  const dl = el('dl');
  const rows: [string, string][] = [
    ['建筑 Buildings', fmt(info.n_buildings)],
    ['最高 Tallest', info.height_max ? `${fmt(info.height_max)} m` : '–'],
    ['高程 Elevation', info.terrain_z ? `${fmt(info.terrain_z[0])}–${fmt(info.terrain_z[1])} m` : '–'],
    ['范围 Extent', info.extent_m ? `${(info.extent_m[0] / 1000).toFixed(1)} × ${(info.extent_m[1] / 1000).toFixed(1)} km` : '–'],
  ];
  for (const [k, v] of rows) dl.append(el('dt', { textContent: k }), el('dd', { textContent: v }));
  const bars = el('div', { className: 'bars' });
  for (const [k, n] of counts) {
    const bar = el('div', { className: 'bar' }, el('span', { textContent: SOURCE_LABELS[k] ?? k }), el('i'), el('em', { textContent: `${((100 * n) / total).toFixed(0)}%` }));
    (bar.querySelector('i') as HTMLElement).style.width = `${Math.max(2, (100 * n) / total)}%`;
    bar.title = `${k}: ${n.toLocaleString('en-US')}`;
    bars.append(bar);
  }
  const legend = el('div', { className: 'legend-bar' });
  legend.style.background = `linear-gradient(to right, ${HEIGHT_COLORS.map(([h, c]) => `${c} ${((h / 350) * 100).toFixed(0)}%`).join(', ')})`;
  const ticks = el('div', { className: 'legend-ticks' });
  for (const h of [0, 100, 200, 350]) {
    const s = el('span', { textContent: `${h} m` });
    s.style.left = `${Math.min(92, Math.max(6, (h / 350) * 100))}%`;
    ticks.append(s);
  }
  const credits = el('p', { className: 'credits', textContent: info.attribution ?? '' });
  box.replaceChildren(
    dl,
    el('h2', { innerHTML: '高度来源 <span>Height sources</span>' }),
    bars,
    el('h2', { innerHTML: '建筑高度 <span>Building height (map)</span>' }),
    legend, ticks, credits,
  );
}

async function main(): Promise<void> {
  setupPanelToggle();
  const cities = (await fetchJson<CityEntry[]>('cities/index.json')) ?? [];
  document.getElementById('cities')!.replaceChildren(...cityLinks(cities.filter((c) => c.name !== name)));
  const entry = cities.find((c) => c.name === name);
  if (!entry) {
    document.getElementById('info')!.replaceChildren();
    showNotice('未找到城市 City not found', name
      ? `No city <code>${name.replace(/[<>&]/g, '')}</code> in <code>cities/index.json</code>.`
      : 'Pick a city from the list.');
    window.__viewReady = true;
    return;
  }
  const info = entry.json ? await fetchJson<CityInfo>(`cities/${entry.name}.json`) : null;
  document.title = `${entry.title.zh} · ${entry.title.en}`;
  document.getElementById('title-zh')!.textContent = entry.title.zh;
  document.getElementById('title-en')!.textContent = entry.title.en;
  document.getElementById('subtitle')!.textContent = 'LoD1 buildings on terrain · 三维建筑与地形';
  renderInfo(info);
  for (const b of document.querySelectorAll<HTMLButtonElement>('#toolbar button')) b.onclick = () => setView(entry, info, b.dataset.view as View);
  setView(entry, info, view);
}

void main();
