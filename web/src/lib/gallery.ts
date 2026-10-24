/** Gallery of the Blender renders (gallery/index.json from `chinamaps webdata`); absent until renders exist. */
import { el, fetchJson } from './ui';

export interface GalleryItem {
  stem: string;
  kind: 'scene' | 'city';
  name: string;
  title: { zh: string; en: string };
  src: string;
  /** 2560 px copy for the lightbox, where the 1280 px thumbnail's labels are too small to read. */
  large?: string | null;
  width: number;
  height: number;
  full: string | null;
}

let items: GalleryItem[] = [];
let current = -1;

const overlay = () => document.getElementById('gallery')!;

/** "View on map" for scenes (fly there), "3D model" for cities. */
function actions(it: GalleryItem, onScene: (name: string) => void): HTMLElement {
  const box = el('div', { className: 'g-actions' });
  if (it.kind === 'scene') {
    box.append(el('button', { textContent: '在地图中查看 View on map', onclick: () => { close(); onScene(it.name); } }));
  } else {
    box.append(el('a', { textContent: '三维城市 3D city', href: `city.html?c=${encodeURIComponent(it.name)}&view=3d` }));
  }
  if (it.full) box.append(el('a', { textContent: '原图 Full size ↗', href: it.full, target: '_blank', rel: 'noopener' }));
  return box;
}

function caption(it: GalleryItem): HTMLElement {
  return el('div', { className: 'g-caption' }, el('b', { textContent: it.title.zh }), el('small', { textContent: it.title.en }));
}

/** Drop an item whose image fails to load (e.g. a stale index entry). */
function dropOnError(img: HTMLImageElement, it: GalleryItem, node: HTMLElement): void {
  img.onerror = () => {
    items = items.filter((x) => x !== it);
    node.remove();
    for (const t of document.querySelectorAll<HTMLElement>(`[data-stem="${it.stem}"]`)) t.remove();
    if (!items.length) document.getElementById('gallery-section')?.classList.add('hidden');
  };
}

function showGrid(onScene: (name: string) => void): void {
  current = -1;
  const grid = el('div', { className: 'g-grid' });
  items.forEach((it, i) => {
    const img = el('img', { src: it.src, alt: `${it.title.zh} ${it.title.en}`, loading: 'lazy', width: it.width, height: it.height });
    const card = el('figure', { className: 'g-card' }, el('button', { className: 'g-img', onclick: () => showOne(i, onScene) }, img), caption(it), actions(it, onScene));
    card.dataset.stem = it.stem;
    dropOnError(img, it, card);
    grid.append(card);
  });
  overlay().querySelector('.g-body')!.replaceChildren(grid);
}

function showOne(i: number, onScene: (name: string) => void): void {
  current = (i + items.length) % items.length;
  const it = items[current];
  const img = el('img', { src: it.large || it.src, alt: `${it.title.zh} ${it.title.en}` });
  const prev = el('button', { className: 'g-nav prev', textContent: '‹', title: 'Previous', onclick: () => showOne(current - 1, onScene) });
  const next = el('button', { className: 'g-nav next', textContent: '›', title: 'Next', onclick: () => showOne(current + 1, onScene) });
  const back = el('button', { className: 'g-back', textContent: '← 全部 All', onclick: () => showGrid(onScene) });
  const one = el('div', { className: 'g-one' }, el('div', { className: 'g-stage' }, img, prev, next),
    el('div', { className: 'g-bar' }, back, caption(it), actions(it, onScene)));
  dropOnError(img, it, one);
  overlay().querySelector('.g-body')!.replaceChildren(one);
}

function open(onScene: (name: string) => void, index = -1): void {
  if (!items.length) return;
  overlay().classList.remove('hidden');
  if (index >= 0) showOne(index, onScene);
  else showGrid(onScene);
  (overlay().querySelector('.g-close') as HTMLElement).focus();
}

function close(): void {
  overlay().classList.add('hidden');
  if (location.hash === '#gallery') history.replaceState(null, '', location.pathname + location.search);
}

/** Fill the panel section and wire the overlay; resolves to the number of images listed. */
export async function setupGallery(onScene: (name: string) => void): Promise<number> {
  items = ((await fetchJson<GalleryItem[]>('gallery/index.json')) ?? []).filter((x) => x?.src && x.title);
  const section = document.getElementById('gallery-section')!;
  if (!items.length) return 0;
  const thumbs = document.getElementById('gallery-thumbs')!;
  thumbs.replaceChildren(...items.map((it, i) => {
    const img = el('img', { src: it.src, alt: it.title.zh, loading: 'lazy' });
    const b = el('button', { title: `${it.title.zh} · ${it.title.en}`, onclick: () => open(onScene, i) }, img);
    b.dataset.stem = it.stem;
    dropOnError(img, it, b);
    return b;
  }));
  (document.getElementById('gallery-open') as HTMLButtonElement).onclick = () => open(onScene);
  overlay().querySelector<HTMLButtonElement>('.g-close')!.onclick = close;
  overlay().addEventListener('click', (e) => { if (e.target === overlay()) close(); });
  document.addEventListener('keydown', (e) => {
    if (overlay().classList.contains('hidden')) return;
    if (e.key === 'Escape') { if (current >= 0) showGrid(onScene); else close(); }
    if (current >= 0 && e.key === 'ArrowLeft') showOne(current - 1, onScene);
    if (current >= 0 && e.key === 'ArrowRight') showOne(current + 1, onScene);
  });
  section.classList.remove('hidden');
  document.getElementById('gallery-count')!.textContent = String(items.length);
  if (location.hash === '#gallery') open(onScene);
  return items.length;
}
