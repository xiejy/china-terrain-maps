/** Small DOM helpers shared by both pages. */

export interface CityEntry {
  name: string;
  title: { zh: string; en: string };
  bbox: [number, number, number, number];
  center: [number, number];
  camera?: { azimuth?: number; elevation?: number; focal_mm?: number; distance?: number };
  landmarks?: { name: string; zh?: string; lon: number; lat: number }[];
  glb: boolean;
  json: boolean;
  buildings: boolean;
}

/** GET a JSON file next to the page; null when it is missing or not JSON. */
export async function fetchJson<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(new URL(path, document.baseURI));
    if (!r.ok || !(r.headers.get('content-type') ?? '').includes('json')) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

export function cityLinks(cities: CityEntry[] | null): HTMLElement[] {
  if (!cities?.length) {
    const s = document.createElement('span');
    s.className = 'muted';
    s.textContent = 'No city models listed yet.';
    return [s];
  }
  return cities.map((c) => {
    const a = document.createElement('a');
    a.href = `city.html?c=${encodeURIComponent(c.name)}`;
    const built = c.glb || c.buildings;
    if (!built) a.classList.add('disabled');
    a.innerHTML = '<b></b><small></small>';
    a.querySelector('b')!.textContent = c.title.zh;
    a.querySelector('small')!.textContent = built ? c.title.en : `${c.title.en} (not built yet)`;
    return a;
  });
}

export function setupPanelToggle(): void {
  const panel = document.getElementById('panel');
  const btn = document.getElementById('panel-toggle');
  if (!panel || !btn) return;
  btn.onclick = () => {
    const collapsed = panel.classList.toggle('collapsed');
    btn.setAttribute('aria-expanded', String(!collapsed));
  };
  if (window.innerWidth <= 640) panel.classList.add('collapsed');
}

export function el<K extends keyof HTMLElementTagNameMap>(tag: K, props: Partial<HTMLElementTagNameMap[K]> = {}, ...children: (Node | string)[]): HTMLElementTagNameMap[K] {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...children);
  return e;
}

/** On phones, start with the compact attribution folded (MapLibre opens it until the first drag). */
export function foldAttributionOnMobile(container: HTMLElement): void {
  if (window.innerWidth > 640) return;
  container.querySelector('.maplibregl-ctrl-attrib')?.classList.remove('maplibregl-compact-show');
}
