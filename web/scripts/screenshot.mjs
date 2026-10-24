#!/usr/bin/env node
/**
 * Screenshot test for the built site (run `npm run build` first).
 *
 * Serves web/dist with a small static server (HTTP Range support, which PMTiles needs), opens the
 * pages in headless Chromium with WebGL2 on SwiftShader, waits for the readiness flags
 * (window.__mapIdle on index.html, window.__viewReady on city.html), and writes PNGs to out/web/.
 * Exits non-zero if a page times out or logs a console error that is not on the BENIGN list.
 *
 * Remote DEM tiles go through $HTTPS_PROXY when it is set. Chromium on Linux trusts the proxy CA
 * when it is in the NSS store (~/.pki/nssdb); set SCREENSHOT_IGNORE_HTTPS_ERRORS=1 to skip TLS
 * checks in this test harness only (never in the site itself).
 *
 * --base /china-terrain-maps/ serves dist under that path only (like GitHub project pages), so a
 * request to an absolute site path 404s and fails the page.
 *
 * Usage: node scripts/screenshot.mjs [--only index,city-*] [--timeout 240] [--base /sub/] [--verbose]
 */
import { createReadStream, mkdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, normalize, resolve, sep } from 'node:path';

process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/opt/pw-browsers';
// Playwright otherwise forces loopback through the proxy, so the local server would be unreachable.
process.env.PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK = '1';
const { chromium } = await import('playwright');

const WEB = resolve(import.meta.dirname, '..');
const DIST = join(WEB, 'dist');
const OUT = resolve(WEB, '..', 'out', 'web');
const VIEWPORT = { width: 1600, height: 1000 };

const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : dflt;
};
const TIMEOUT = Number(opt('timeout', 240)) * 1000;
const ONLY = opt('only', '');
const VERBOSE = args.includes('--verbose');
const BASE = `/${opt('base', '/').replace(/^\/+|\/+$/g, '')}/`.replace('//', '/');

// Console errors that are expected and harmless:
//  - aborted tile requests while the camera moves (MapLibre cancels them),
//  - SwiftShader's GPU stall / performance notes,
//  - 404 for DEM tiles outside the AWS terrarium coverage (none expected, but harmless).
const BENIGN = [
  /net::ERR_ABORTED/,
  /AbortError/,
  /GPU stall due to ReadPixels/,
  /elevation-tiles-prod.*404/,
];

const PAGES = [
  { name: 'index', url: 'index.html', flag: '__mapIdle' },
  { name: 'index-everest', url: 'index.html?scene=himalaya-everest', flag: '__mapIdle' },
  { name: 'index-hengduan', url: 'index.html?scene=hengduan-three-rivers', flag: '__mapIdle' },
  { name: 'index-aws-classic', url: 'index.html?dem=aws&palette=web&scene=sichuan-basin-tibet-edge', flag: '__mapIdle' },
  { name: 'city-chongqing-yuzhong-map', url: 'city.html?c=chongqing-yuzhong&view=map', flag: '__viewReady' },
  { name: 'city-chongqing-yuzhong-3d', url: 'city.html?c=chongqing-yuzhong&view=3d', flag: '__viewReady' },
  { name: 'city-hongkong-victoria-harbour-map', url: 'city.html?c=hongkong-victoria-harbour&view=map', flag: '__viewReady' },
  { name: 'city-hongkong-victoria-harbour-3d', url: 'city.html?c=hongkong-victoria-harbour&view=3d', flag: '__viewReady' },
  { name: 'city-missing', url: 'city.html?c=no-such-city', flag: '__viewReady' },
  // A listed city whose model and tiles are not built yet (entry injected into cities/index.json).
  { name: 'city-not-built-map', url: 'city.html?c=demo-not-built&view=map', flag: '__viewReady', inject: true },
  { name: 'city-not-built-3d', url: 'city.html?c=demo-not-built&view=3d', flag: '__viewReady', inject: true },
  { name: 'index-mobile', url: 'index.html', flag: '__mapIdle', viewport: { width: 390, height: 844 } },
  // phone-width 3D viewer: the fitted camera distance must not be clamped by OrbitControls.maxDistance
  { name: 'city-hongkong-victoria-harbour-3d-mobile', url: 'city.html?c=hongkong-victoria-harbour&view=3d', flag: '__viewReady', viewport: { width: 390, height: 844 } },
  // Gallery overlay and lightbox (only shown when gallery/index.json lists renders).
  { name: 'index-gallery', url: 'index.html#gallery', flag: '__mapIdle' },
  { name: 'index-gallery-one', url: 'index.html#gallery', flag: '__mapIdle', click: '.g-img' },
];

const NOT_BUILT = {
  name: 'demo-not-built', title: { zh: '未生成示例', en: 'Not built yet (test)' }, bbox: [106.53, 29.53, 106.61, 29.59],
  center: [106.57, 29.56], camera: { azimuth: 225, elevation: 32 }, landmarks: [], glb: false, json: false, buildings: false,
};

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.pbf': 'application/x-protobuf', '.pmtiles': 'application/octet-stream',
  '.glb': 'model/gltf-binary', '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml',
};

const outsideBase = []; // requests that escaped BASE (absolute paths): each one fails the run

/** Static file server for dist/ under BASE, with single-range support. */
function serve(root) {
  const server = createServer((req, res) => {
    const full = decodeURIComponent(new URL(req.url, 'http://x').pathname);
    if (!full.startsWith(BASE)) { outsideBase.push(full); res.writeHead(404).end(`outside ${BASE}`); return; }
    const path = `/${full.slice(BASE.length)}`;
    let file = normalize(join(root, path.endsWith('/') ? `${path}index.html` : path));
    if (!file.startsWith(root + sep) && file !== root) { res.writeHead(403).end(); return; }
    let st;
    try { st = statSync(file); } catch { res.writeHead(404).end('not found'); return; }
    if (st.isDirectory()) { file = join(file, 'index.html'); st = statSync(file); }
    const headers = { 'Content-Type': MIME[extname(file)] ?? 'application/octet-stream', 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-cache' };
    const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range ?? '');
    if (m && (m[1] || m[2])) {
      let start = m[1] ? Number(m[1]) : Math.max(0, st.size - Number(m[2]));
      let end = m[1] && m[2] ? Math.min(Number(m[2]), st.size - 1) : st.size - 1;
      if (start >= st.size || start > end) { res.writeHead(416, { 'Content-Range': `bytes */${st.size}` }).end(); return; }
      res.writeHead(206, { ...headers, 'Content-Range': `bytes ${start}-${end}/${st.size}`, 'Content-Length': end - start + 1 });
      if (req.method === 'HEAD') { res.end(); return; }
      createReadStream(file, { start, end }).pipe(res);
      return;
    }
    res.writeHead(200, { ...headers, 'Content-Length': st.size });
    if (req.method === 'HEAD') { res.end(); return; }
    createReadStream(file).pipe(res);
  });
  return new Promise((ok) => server.listen(0, '127.0.0.1', () => ok(server)));
}

async function shoot(browser, base, page) {
  const ctx = await browser.newContext({
    viewport: page.viewport ?? VIEWPORT,
    deviceScaleFactor: 1,
    ignoreHTTPSErrors: process.env.SCREENSHOT_IGNORE_HTTPS_ERRORS === '1',
  });
  const p = await ctx.newPage();
  if (page.inject) {
    await p.route('**/cities/index.json', async (route) => {
      const res = await route.fetch();
      const cities = res.ok() ? await res.json() : [];
      await route.fulfill({ response: res, json: [...cities, NOT_BUILT] });
    });
  }
  const errors = [];
  const warnings = [];
  const failed = [];
  p.on('console', (m) => {
    if (m.type() === 'error' && !BENIGN.some((re) => re.test(m.text()))) errors.push(m.text());
    if (m.type() === 'warning') warnings.push(m.text());
  });
  p.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  p.on('requestfailed', (r) => {
    const f = r.failure()?.errorText ?? '';
    if (!/ERR_ABORTED/.test(f)) failed.push(`${f} ${r.url().slice(0, 140)}`);
  });
  const t0 = Date.now();
  let ok = true;
  await p.goto(`${base}/${page.url}`, { waitUntil: 'load' });
  try {
    await p.waitForFunction((flag) => window[flag] === true, page.flag, { timeout: TIMEOUT, polling: 500 });
    if (page.click && (await p.locator(page.click).count())) await p.locator(page.click).first().click();
    await p.waitForTimeout(1500);
  } catch {
    ok = false;
    errors.push(`timeout: window.${page.flag} not set after ${TIMEOUT / 1000}s`);
  }
  const tIdle = Date.now();
  const file = join(OUT, `${page.name}.png`);
  try {
    // SwiftShader can need minutes for one frame of a pitched terrain view on a busy machine.
    await p.screenshot({ path: file, timeout: TIMEOUT });
  } catch (e) {
    ok = false;
    errors.push(`screenshot failed: ${e.message.split('\n')[0]}`);
  }
  const secs = ((Date.now() - t0) / 1000).toFixed(1);
  if (VERBOSE) warnings.push(`timing: ready ${((tIdle - t0) / 1000).toFixed(1)}s, capture ${((Date.now() - tIdle) / 1000).toFixed(1)}s`);
  await ctx.close();
  return { ...page, file, ok: ok && errors.length === 0, secs, errors, warnings, failed };
}

async function main() {
  try { statSync(join(DIST, 'index.html')); } catch {
    console.error('web/dist/index.html missing: run `npm run build` first');
    process.exit(2);
  }
  mkdirSync(OUT, { recursive: true });
  const server = await serve(DIST);
  const base = `http://127.0.0.1:${server.address().port}${BASE.replace(/\/$/, '')}`;
  console.log(`serving ${DIST} at ${base}/`);
  const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
  const browser = await chromium.launch({
    args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
    ...(proxy ? { proxy: { server: proxy, bypass: '127.0.0.1,localhost' } } : {}),
  });
  const match = (name, o) => (o.endsWith('*') ? name.startsWith(o.slice(0, -1)) : name === o);
  const pages = PAGES.filter((p) => !ONLY || ONLY.split(',').some((o) => match(p.name, o)));
  let failures = 0;
  for (const page of pages) {
    const r = await shoot(browser, base, page);
    console.log(`${r.ok ? 'PASS' : 'FAIL'} ${r.name.padEnd(36)} ${r.secs.padStart(6)}s  ${r.file}`);
    for (const e of r.errors) console.log(`     console: ${e.slice(0, 300)}`);
    for (const f of r.failed.slice(0, 5)) console.log(`     request failed: ${f}`);
    if (VERBOSE) for (const w of r.warnings) console.log(`     warning: ${w.slice(0, 300)}`);
    if (!r.ok) failures += 1;
  }
  await browser.close();
  server.close();
  if (outsideBase.length) {
    console.log(`FAIL ${outsideBase.length} request(s) outside ${BASE}: ${[...new Set(outsideBase)].slice(0, 10).join(', ')}`);
    failures += 1;
  }
  process.exit(failures ? 1 : 0);
}

await main();
