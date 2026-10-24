/** Shared terrain pieces: DEM sources, colour relief, hillshade, sky and the pmtiles protocol. */
import * as maplibregl from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import { Protocol } from 'pmtiles';
import type {
  ColorReliefLayerSpecification,
  HillshadeLayerSpecification,
  RasterDEMSourceSpecification,
  SkySpecification,
} from 'maplibre-gl';
import generatedStops from '../generated/color_relief.json';

export type DemKey = 'mapterhorn' | 'aws';

const MAPTERHORN =
  'pmtiles://https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop/mapterhorn/mapterhorn/planet.pmtiles';
const AWS_TERRARIUM = 'https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png';

export const DEM_LABELS: Record<DemKey, string> = {
  mapterhorn: 'Mapterhorn PMTiles',
  aws: 'AWS Terrain Tiles',
};

/** The bundled worker has to be pointed at explicitly once maplibre-gl.mjs is bundled. */
let initialised = false;
export function initMapLibre(): void {
  if (initialised) return;
  initialised = true;
  maplibregl.setWorkerUrl(workerUrl);
  const protocol = new Protocol();
  maplibregl.addProtocol('pmtiles', protocol.tile);
}

/** DEM sources carry no attribution of their own: ATTRIBUTION below lists every source. */
export function demSource(key: DemKey): RasterDEMSourceSpecification {
  if (key === 'mapterhorn') {
    return {
      type: 'raster-dem',
      url: MAPTERHORN,
      encoding: 'terrarium',
      tileSize: 512,
      maxzoom: 12,
    };
  }
  return {
    type: 'raster-dem',
    tiles: [AWS_TERRARIUM],
    encoding: 'terrarium',
    tileSize: 256,
    maxzoom: 15,
  };
}

export type ReliefStop = [number, string];
export type PaletteKey = 'render' | 'web';
/** Both ramps come from config/palettes.yaml via `chinamaps webdata`. */
export const PALETTES = generatedStops as Record<PaletteKey, ReliefStop[]>;
export const PALETTE_LABELS: Record<PaletteKey, string> = { render: '渲染 Render tints', web: '经典 Classic' };

/**
 * Stops for a palette on a DEM. Mapterhorn has no bathymetry: the sea is exactly 0 m, and negative
 * values are land below sea level (Turpan, -154 m). So with Mapterhorn, 0 m is drawn as water and
 * depressions as lowland; AWS terrarium has bathymetry (ETOPO1) and keeps the ocean ramp.
 */
export function reliefStops(palette: PaletteKey, dem: DemKey): ReliefStop[] {
  const stops = PALETTES[palette];
  if (dem !== 'mapterhorn') return stops;
  const land = stops.filter(([z]) => z >= 0);
  const sea = stops.filter(([z]) => z < 0);
  const water = sea.length ? sea[sea.length - 1][1] : '#afcdde';
  const low = land[0][1];
  return [[-500, low], [-0.5, low], [-0.05, water], [0.05, water], [1, low], ...land.filter(([z]) => z > 1)];
}

export function reliefLayer(stops: ReliefStop[], source = 'relief-dem'): ColorReliefLayerSpecification {
  return {
    id: 'color-relief',
    type: 'color-relief',
    source,
    paint: {
      'color-relief-color': ['interpolate', ['linear'], ['elevation'], ...stops.flat()] as never,
      'color-relief-opacity': 1,
    },
  };
}

export function hillshadeLayer(exaggeration = 0.6, source = 'relief-dem'): HillshadeLayerSpecification {
  return {
    id: 'hillshade',
    type: 'hillshade',
    source,
    paint: {
      'hillshade-method': 'multidirectional',
      'hillshade-exaggeration': exaggeration,
      // Main light from the north-west, softer fills from west, north and south-west.
      'hillshade-illumination-direction': [315, 270, 0, 225],
      'hillshade-illumination-altitude': [35, 30, 30, 25],
      'hillshade-shadow-color': ['rgba(38,30,60,0.55)', 'rgba(46,36,64,0.32)', 'rgba(40,44,70,0.28)', 'rgba(60,40,50,0.22)'],
      'hillshade-highlight-color': ['rgba(255,248,228,0.42)', 'rgba(255,244,220,0.18)', 'rgba(250,250,255,0.15)', 'rgba(255,236,210,0.12)'],
      'hillshade-accent-color': 'rgba(30,24,40,0.35)',
    },
  };
}

export const SKY: SkySpecification = {
  'sky-color': '#5f93c9',
  'horizon-color': '#dde8ef',
  'fog-color': '#e6edf1',
  'sky-horizon-blend': 0.6,
  'horizon-fog-blend': 0.7,
  'fog-ground-blend': ['interpolate', ['linear'], ['zoom'], 3, 0.92, 8, 0.75, 12, 0.6] as never,
  'atmosphere-blend': ['interpolate', ['linear'], ['zoom'], 0, 1, 6, 0.8, 10, 0],
};

export const ATTRIBUTION = [
  '<a href="https://mapterhorn.com/attribution" target="_blank">Mapterhorn</a> (Copernicus DEM etc.)',
  '<a href="https://registry.opendata.aws/terrain-tiles/" target="_blank">AWS Terrain Tiles</a> (SRTM, GMTED, ETOPO1)',
  '<a href="https://www.naturalearthdata.com/" target="_blank">Natural Earth</a>',
  '© <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors (ODbL) via <a href="https://overturemaps.org/" target="_blank">Overture Maps</a>',
  '<a href="https://doi.org/10.5281/zenodo.8174931" target="_blank">Shi et al. 2023</a> buildings (CC BY 4.0)',
  '<a href="https://github.com/zhu-xlab/GlobalBuildingAtlas" target="_blank">GlobalBuildingAtlas</a> (CC BY-NC 4.0)',
  'ESA Copernicus Sentinel-2',
];

/** Absolute URL of a file under the site root (Vite base is './'). */
export function siteUrl(path: string): string {
  return new URL(path, document.baseURI).href;
}

/** Glyph template; built by hand because URL() would escape the braces. */
export function glyphsUrl(): string {
  return `${new URL('.', document.baseURI).href}fonts/{fontstack}/{range}.pbf`;
}

export const LOCAL_CJK_FONTS = "'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif";

export function demFromQuery(): DemKey {
  const q = new URLSearchParams(location.search).get('dem');
  return q === 'aws' ? 'aws' : 'mapterhorn';
}

/**
 * Switch to the AWS tiles if the Mapterhorn archive keeps failing (CORS, network, outage).
 * Calls onFallback once after `threshold` DEM errors.
 */
export function watchDemErrors(map: maplibregl.Map, current: () => DemKey, onFallback: () => void, threshold = 3): void {
  let errors = 0;
  map.on('error', (e: maplibregl.ErrorEvent & { sourceId?: string }) => {
    if (!e.sourceId || !e.sourceId.endsWith('-dem') || current() !== 'mapterhorn') return;
    errors += 1;
    console.warn(`DEM error ${errors}/${threshold}:`, e.error?.message ?? e.error);
    if (errors === threshold) onFallback();
  });
}

/** Look direction for a camera placed at compass `azimuth` from the target. */
export function bearingFromAzimuth(azimuth: number): number {
  return (azimuth + 180) % 360;
}
