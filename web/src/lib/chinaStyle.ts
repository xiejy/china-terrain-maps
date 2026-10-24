/** MapLibre style for the China terrain map: relief + hillshade + 3D terrain + vector overlays. */
import type {
  ExpressionSpecification,
  LayerSpecification,
  StyleSpecification,
  SymbolLayerSpecification,
} from 'maplibre-gl';
import { SKY, demSource, glyphsUrl, hillshadeLayer, reliefLayer, reliefStops, siteUrl, type DemKey, type PaletteKey } from './dem';

export interface MapState {
  dem: DemKey;
  palette: PaletteKey;
  terrain: boolean;
  exaggeration: number;
  english: boolean;
  groups: Record<GroupKey, boolean>;
}

export type GroupKey = 'relief' | 'hillshade' | 'water' | 'boundaries' | 'places' | 'physical' | 'provinces';

export const GROUP_LABELS: Record<GroupKey, string> = {
  relief: '分层设色 Colour relief',
  hillshade: '晕渲 Hillshade',
  water: '水系 Rivers & lakes',
  boundaries: '境界 Boundaries',
  provinces: '省区 Provinces',
  places: '城市 Places',
  physical: '自然地理 Physical names',
};

const REGULAR = ['Noto Sans Regular'];
const MEDIUM = ['Noto Sans Medium'];
const ITALIC = ['Noto Sans Italic'];

const BORDER = '#8c1d2c';
const WATER_LINE = '#3f86bd';
const WATER_TEXT = '#1b5684';
const HALO = 'rgba(255,252,246,0.86)';

/** Chinese name, optionally with the English name on a smaller second line. */
export function nameField(english: boolean, extra?: ExpressionSpecification): ExpressionSpecification {
  const parts: unknown[] = ['format', ['coalesce', ['get', 'zh_sp'], ['get', 'zh']], {}];
  if (extra) parts.push(extra, { 'font-scale': 0.8 });
  if (english) parts.push('\n', {}, ['coalesce', ['get', 'en'], ''], { 'font-scale': 0.7, 'text-font': ['literal', REGULAR] });
  return parts as ExpressionSpecification;
}

const ELEVATION: ExpressionSpecification = [
  'case',
  ['has', 'elevation'],
  ['concat', ' ', ['to-string', ['round', ['get', 'elevation']]]],
  '',
];

/** Text-field per label layer (re-applied when the English toggle changes). */
export function textFields(english: boolean): Record<string, ExpressionSpecification> {
  const physical: ExpressionSpecification = ['case', ['==', ['get', 'kind'], 'peak'], nameField(english, ELEVATION), nameField(english)];
  return {
    'province-label': ['get', 'zh'],
    'place-minor': nameField(english),
    'place-major': nameField(english),
    'place-capital': nameField(english),
    'label-city': nameField(english),
    'label-physical': physical,
    'label-physical-major': physical,
  };
}

export const GROUP_LAYERS: Record<GroupKey, string[]> = {
  relief: ['color-relief'],
  hillshade: ['hillshade'],
  water: ['glacier', 'lake-fill', 'lake-line', 'river'],
  boundaries: ['boundary-other', 'boundary-china-casing', 'boundary-china', 'scs-dash-casing', 'scs-dash'],
  provinces: ['province-line', 'province-label'],
  places: ['place-minor', 'place-major', 'place-capital', 'label-city'],
  physical: ['label-physical', 'label-physical-major'],
};

const zoomInterp = (...stops: (number | ExpressionSpecification)[]): ExpressionSpecification =>
  ['interpolate', ['linear'], ['zoom'], ...stops] as ExpressionSpecification;

/** Natural Earth 1:10M boundaries drift ~1 km off the ridge crests (e.g. south of the Everest summit):
 * fade them out where that becomes visible. */
const borderFade = (opacity: number): ExpressionSpecification => zoomInterp(7.5, opacity, 9, 0);

function symbol(id: string, sourceLayer: string, layout: SymbolLayerSpecification['layout'], paint: SymbolLayerSpecification['paint'], filter?: ExpressionSpecification, minzoom?: number, maxzoom?: number): SymbolLayerSpecification {
  const l: SymbolLayerSpecification = { id, type: 'symbol', source: 'ov', 'source-layer': sourceLayer, layout, paint };
  if (filter) l.filter = filter;
  if (minzoom !== undefined) l.minzoom = minzoom;
  if (maxzoom !== undefined) l.maxzoom = maxzoom;
  return l;
}

function vectorLayers(): LayerSpecification[] {
  const riverWidth: ExpressionSpecification = [
    'interpolate', ['exponential', 1.4], ['zoom'],
    2, ['max', 0.25, ['-', 1.5, ['*', 0.14, ['get', 'scalerank']]]],
    6, ['max', 0.6, ['-', 3.0, ['*', 0.24, ['get', 'scalerank']]]],
    10, ['max', 1.2, ['-', 5.0, ['*', 0.32, ['get', 'scalerank']]]],
  ];
  return [
    {
      id: 'glacier', type: 'fill', source: 'ov', 'source-layer': 'glacier', minzoom: 3,
      paint: { 'fill-color': '#ffffff', 'fill-opacity': zoomInterp(3, 0.25, 7, 0.45) },
    },
    { id: 'lake-fill', type: 'fill', source: 'ov', 'source-layer': 'lake', paint: { 'fill-color': '#8ebfdc', 'fill-opacity': 0.95 } },
    {
      id: 'lake-line', type: 'line', source: 'ov', 'source-layer': 'lake',
      paint: { 'line-color': WATER_LINE, 'line-width': zoomInterp(3, 0.3, 10, 1.0), 'line-opacity': 0.7 },
    },
    {
      id: 'river', type: 'line', source: 'ov', 'source-layer': 'river',
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': WATER_LINE, 'line-width': riverWidth, 'line-opacity': 0.92 },
    },
    {
      id: 'province-line', type: 'line', source: 'ov', 'source-layer': 'province', minzoom: 3,
      layout: { 'line-join': 'round' },
      paint: { 'line-color': '#5e4532', 'line-width': zoomInterp(3, 0.8, 6, 1.2, 10, 1.8), 'line-opacity': 0.8, 'line-dasharray': [3, 2] },
    },
    {
      id: 'boundary-other', type: 'line', source: 'ov', 'source-layer': 'boundary', filter: ['!', ['get', 'china']],
      layout: { 'line-join': 'round' },
      paint: { 'line-color': '#4d4f5c', 'line-width': zoomInterp(2, 0.6, 6, 1.2, 10, 2), 'line-opacity': borderFade(0.65), 'line-dasharray': [4, 1.5, 1, 1.5] },
    },
    {
      id: 'boundary-china-casing', type: 'line', source: 'ov', 'source-layer': 'boundary', filter: ['get', 'china'],
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': '#ffffff', 'line-width': zoomInterp(2, 3.5, 6, 6, 10, 7), 'line-opacity': borderFade(0.55), 'line-blur': 1 },
    },
    {
      id: 'boundary-china', type: 'line', source: 'ov', 'source-layer': 'boundary', filter: ['get', 'china'],
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': BORDER, 'line-width': zoomInterp(2, 1.5, 6, 2.6, 10, 2.8), 'line-opacity': borderFade(1) },
    },
    {
      id: 'scs-dash-casing', type: 'line', source: 'ov', 'source-layer': 'scs_dash',
      layout: { 'line-cap': 'round' },
      paint: { 'line-color': '#ffffff', 'line-width': zoomInterp(2, 3.5, 6, 6, 10, 9), 'line-opacity': 0.5, 'line-blur': 1 },
    },
    {
      id: 'scs-dash', type: 'line', source: 'ov', 'source-layer': 'scs_dash',
      layout: { 'line-cap': 'round' },
      paint: { 'line-color': BORDER, 'line-width': zoomInterp(2, 1.5, 6, 2.6, 10, 2.8) },
    },
  ];
}

const AREA_KINDS = ['range', 'plateau', 'basin', 'plain', 'desert', 'region'];
const WATER_KINDS = ['river', 'lake', 'sea'];

/** Physical-feature labels (layer 'label') with per-kind styling in one layer, so symbol-sort-key
 * (rank, then kind) decides collisions between ranges, basins, peaks, rivers and seas. */
function physicalLayer(id: string, english: boolean, filter: ExpressionSpecification): SymbolLayerSpecification {
  const kind = ['get', 'kind'] as ExpressionSpecification;
  const rank = ['get', 'rank'] as ExpressionSpecification;
  const isArea: ExpressionSpecification = ['in', kind, ['literal', AREA_KINDS]];
  const size = (r1: number, r2: number, r3: number): ExpressionSpecification => ['match', rank, 1, r1, 2, r2, r3];
  const sizeAt = (area: ExpressionSpecification, sea: ExpressionSpecification, other: ExpressionSpecification): ExpressionSpecification =>
    ['case', isArea, area, ['==', kind, 'sea'], sea, other];
  return symbol(id, 'label', {
    'text-field': textFields(english)[id],
    'text-font': ['case', ['in', kind, ['literal', WATER_KINDS]], ['literal', ITALIC], ['literal', MEDIUM]],
    'text-size': zoomInterp(
      2, sizeAt(size(14, 12, 11), size(15, 13, 12), size(13, 12, 11)),
      6, sizeAt(size(20, 16, 14), size(20, 17, 15), size(15, 13.5, 12)),
      10, sizeAt(size(24, 20, 17), size(22, 19, 16), size(17, 15, 13)),
    ),
    'text-letter-spacing': 0.04,
    'text-max-width': 30,
    'text-line-height': 1.15,
    'icon-image': ['case', ['!', ['get', 'marker']], '', ['==', kind, 'peak'], 'peak', 'dot-minor'],
    // unmarked island names (台湾岛, 海南岛) sit offshore to the east, leaving the island to its cities (台北)
    'text-anchor': ['case', ['all', ['get', 'marker'], ['==', kind, 'peak']], 'top', ['get', 'marker'], 'left', ['==', kind, 'island'], 'left', 'center'],
    'text-offset': ['case', ['all', ['get', 'marker'], ['==', kind, 'peak']], ['literal', [0, 0.7]], ['get', 'marker'], ['literal', [0.7, 0]],
      ['==', kind, 'island'], ['literal', [2.4, 0]], ['literal', [0, 0]]],
    'text-padding': 3,
    // within a rank, marked points (peaks: a fixed spot) win over area names, which have room to give
    'symbol-sort-key': ['+', ['*', 10, rank], ['case', ['get', 'marker'], -8, 0],
      ['match', kind, 'range', 0, 'plateau', 1, 'basin', 2, 'desert', 3, 'plain', 4, 'sea', 5, 'peak', 6, 'river', 7, 'lake', 8, 'island', 9, 5]],
  }, {
    'text-color': ['match', kind,
      'range', '#4a2c12', 'desert', '#7a4a1c', 'region', '#4d3a2a', 'peak', '#3b2512', 'island', '#2f3a44',
      'sea', '#1f5b8a', 'river', WATER_TEXT, 'lake', WATER_TEXT, '#5e3f1f'],
    'text-halo-color': ['match', kind, 'sea', 'rgba(235,245,252,0.72)', 'rgba(255,250,240,0.84)'],
    'text-halo-width': 1.6,
    'text-halo-blur': 0.5,
  }, filter);
}

function labelLayers(english: boolean): LayerSpecification[] {
  const tf = textFields(english);
  const halo = { 'text-halo-color': HALO, 'text-halo-width': 1.5, 'text-halo-blur': 0.4 };
  const isCapital: ExpressionSpecification = ['all', ['get', 'china'], ['==', ['get', 'capital'], 'country']];
  const isMajor: ExpressionSpecification = ['all', ['!', isCapital], ['any', ['!=', ['get', 'capital'], ''], ['all', ['get', 'china'], ['<=', ['get', 'rank'], 1]]]];
  const placeIcon: ExpressionSpecification = ['case',
    ['!', ['get', 'china']], ['match', ['get', 'capital'], 'country', 'capital-minor', 'dot-minor'],
    ['match', ['get', 'capital'], 'country', 'capital', 'province', 'province-capital', 'dot']];

  const placeLayout = (id: string, size: ExpressionSpecification): SymbolLayerSpecification['layout'] => ({
    'icon-image': placeIcon,
    'text-field': tf[id],
    'text-font': MEDIUM,
    'text-size': size,
    'text-variable-anchor': ['left', 'right', 'top', 'bottom'],
    'text-radial-offset': 0.75,
    'text-justify': 'auto',
    'text-line-height': 1.1,
    'symbol-sort-key': ['+', ['match', ['get', 'capital'], 'country', 0, 'province', 1, 3], ['get', 'rank'], ['case', ['get', 'china'], 0, 20]],
  });
  const placePaint = { 'text-color': ['case', ['get', 'china'], '#14181d', '#4f555d'] as ExpressionSpecification, ...halo };

  // Symbol layers are placed top-down: later layers in this list win label collisions.
  return [
    symbol('province-label', 'province_label', {
      'text-field': tf['province-label'],
      'text-font': MEDIUM,
      'text-size': zoomInterp(4, 12, 7, 16),
      'text-letter-spacing': 0.6,
      'text-padding': 6,
    }, { 'text-color': 'rgba(92,70,50,0.78)', 'text-halo-color': 'rgba(255,252,246,0.55)', 'text-halo-width': 1.2 }, undefined, 4, 8.5),
    symbol('place-minor', 'place', placeLayout('place-minor', zoomInterp(4, 11, 8, 14, 10, 15)), placePaint, ['!', ['any', isCapital, isMajor]]),
    symbol('label-city', 'label', {
      'text-field': tf['label-city'],
      'text-font': MEDIUM,
      'text-size': zoomInterp(5, 12, 10, 15),
      'icon-image': 'dot',
      'text-variable-anchor': ['left', 'right', 'top', 'bottom'],
      'text-radial-offset': 0.75,
      'symbol-sort-key': ['get', 'rank'],
    }, { 'text-color': '#20252c', ...halo }, ['==', ['get', 'kind'], 'city']),
    physicalLayer('label-physical', english, ['all', ['!=', ['get', 'kind'], 'city'], ['>', ['get', 'rank'], 1]]),
    symbol('place-major', 'place', placeLayout('place-major', zoomInterp(3, 12.5, 8, 17)), placePaint, isMajor),
    physicalLayer('label-physical-major', english, ['all', ['!=', ['get', 'kind'], 'city'], ['<=', ['get', 'rank'], 1]]),
    symbol('place-capital', 'place', { ...placeLayout('place-capital', zoomInterp(3, 15, 8, 20)), 'icon-allow-overlap': true }, placePaint, isCapital),
  ];
}

/** Layers kept in the 南海诸岛 inset: relief, China's boundary, the dashes and the island names. */
const INSET_LAYERS = ['background', 'color-relief', 'hillshade', 'boundary-other', 'boundary-china-casing', 'boundary-china',
  'scs-dash-casing', 'scs-dash'];

/** Static 2D style for the South China Sea inset (same sources, no terrain or sky). */
export function buildInsetStyle(s: MapState): StyleSpecification {
  const full = buildStyle({ ...s, terrain: false, groups: { ...s.groups, relief: true, boundaries: true } });
  const widths: Record<string, number> = { 'boundary-other': 0.6, 'boundary-china-casing': 3, 'boundary-china': 1.2, 'scs-dash-casing': 3.4, 'scs-dash': 1.5 };
  const layers = full.layers.filter((l) => INSET_LAYERS.includes(l.id)).map((l) =>
    l.type === 'line' ? ({ ...l, paint: { ...l.paint, 'line-width': widths[l.id] } } as LayerSpecification) : l);
  layers.push(symbol('inset-label', 'label', {
    'text-field': ['get', 'zh'],
    'text-font': MEDIUM,
    'text-size': 9.5,
    'text-padding': 1,
    'text-allow-overlap': false,
  }, { 'text-color': '#2f3a44', 'text-halo-color': 'rgba(255,255,255,0.85)', 'text-halo-width': 1.2 },
  ['all', ['==', ['get', 'kind'], 'island'], ['get', 'marker']]));
  delete full.terrain;
  delete full.sky;
  return { ...full, layers };
}

export function buildStyle(s: MapState): StyleSpecification {
  const layers: LayerSpecification[] = [
    { id: 'background', type: 'background', paint: { 'background-color': '#2b4560' } },
    reliefLayer(reliefStops(s.palette, s.dem)),
    hillshadeLayer(0.6),
    ...vectorLayers(),
    ...labelLayers(s.english),
  ];
  for (const [group, ids] of Object.entries(GROUP_LAYERS) as [GroupKey, string[]][]) {
    if (s.groups[group]) continue;
    for (const l of layers) if (ids.includes(l.id)) l.layout = { ...(l.layout ?? {}), visibility: 'none' } as never;
  }
  const style: StyleSpecification = {
    version: 8,
    glyphs: glyphsUrl(),
    sources: {
      'terrain-dem': demSource(s.dem),
      'relief-dem': demSource(s.dem),
      ov: { type: 'vector', url: `pmtiles://${siteUrl('data/china.pmtiles')}` },
    },
    sky: SKY,
    layers,
  };
  if (s.terrain) style.terrain = { source: 'terrain-dem', exaggeration: s.exaggeration };
  return style;
}
