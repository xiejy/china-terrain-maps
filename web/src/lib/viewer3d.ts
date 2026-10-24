/** three.js viewer for cities/<name>.glb (meshopt-compressed, Y-up metres; see docs/ARCHITECTURE.md). */
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';

export interface CityInfo {
  name?: string;
  title?: { zh: string; en: string };
  n_buildings?: number;
  height_source_counts?: Record<string, number>;
  height_max?: number;
  extent_m?: [number, number];
  z_min?: number;
  z_max?: number;
  terrain_z?: [number, number];
  camera?: { azimuth?: number; elevation?: number; focal_mm?: number; distance?: number };
  sun?: { azimuth?: number; elevation?: number; strength?: number };
  attribution?: string;
}

export interface Viewer {
  start(): void;
  stop(): void;
  firstFrame(): Promise<void>;
}

const SKY_TOP = new THREE.Color('#4f86c6');
const SKY_HORIZON = new THREE.Color('#dce6ec');

/** Unit vector toward compass `azimuth` (deg from north, clockwise) at `elevation` above the horizon. */
export function direction(azimuth: number, elevation: number): THREE.Vector3 {
  const a = THREE.MathUtils.degToRad(azimuth);
  const e = THREE.MathUtils.degToRad(elevation);
  // Model frame: x = east, y = up, z = -north.
  return new THREE.Vector3(Math.cos(e) * Math.sin(a), Math.sin(e), -Math.cos(e) * Math.cos(a));
}

function skyDome(radius: number): THREE.Mesh {
  const mat = new THREE.ShaderMaterial({
    side: THREE.BackSide,
    depthWrite: false,
    fog: false,
    uniforms: { top: { value: SKY_TOP }, horizon: { value: SKY_HORIZON } },
    vertexShader: 'varying vec3 vDir; void main() { vDir = normalize(position); gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
    fragmentShader: `uniform vec3 top; uniform vec3 horizon; varying vec3 vDir;
      void main() { float t = pow(clamp(vDir.y, 0.0, 1.0), 0.55); gl_FragColor = vec4(mix(horizon, top, t), 1.0); }`,
  });
  const mesh = new THREE.Mesh(new THREE.SphereGeometry(radius, 32, 16), mat);
  mesh.renderOrder = -1;
  mesh.frustumCulled = false;
  return mesh;
}

/** Darken building walls toward the ground for a little depth (heights are absolute in this frame). */
function shadeBuildings(mat: THREE.MeshStandardMaterial, zRange: [number, number]): void {
  mat.onBeforeCompile = (shader) => {
    shader.uniforms.uZ = { value: new THREE.Vector2(zRange[0], zRange[1]) };
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\nvarying float vWorldY;')
      .replace('#include <project_vertex>', '#include <project_vertex>\nvWorldY = (modelMatrix * vec4(transformed, 1.0)).y;');
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', '#include <common>\nvarying float vWorldY;\nuniform vec2 uZ;')
      .replace('#include <color_fragment>', `#include <color_fragment>
        float hz = clamp((vWorldY - uZ.x) / max(1.0, uZ.y - uZ.x), 0.0, 1.0);
        diffuseColor.rgb *= mix(0.78, 1.08, smoothstep(0.0, 0.8, hz));`);
  };
  mat.needsUpdate = true;
}

/** Left panel width (px) covering the canvas on desktop; the view is centred in the rest. */
function panelInset(width: number): number {
  return width > 640 ? 320 : 0;
}

/** Shift the projection so the look-at point sits in the middle of the area right of the panel. */
function applyViewOffset(camera: THREE.PerspectiveCamera, w: number, h: number): void {
  camera.setViewOffset(w, h, -panelInset(w) / 2, 0, w, h);
}

function boxCorners(box: THREE.Box3): THREE.Vector3[] {
  return [0, 1, 2, 3, 4, 5, 6, 7].map((i) => new THREE.Vector3(
    i & 1 ? box.max.x : box.min.x, i & 2 ? box.max.y : box.min.y, i & 4 ? box.max.z : box.min.z,
  ));
}

/** Place the camera at `target + dir * d`; returns the NDC bounds of `pts` (null if one is behind it). */
function project(camera: THREE.PerspectiveCamera, target: THREE.Vector3, dir: THREE.Vector3, d: number, pts: THREE.Vector3[]) {
  camera.position.copy(target).addScaledVector(dir, d);
  camera.lookAt(target);
  camera.updateMatrixWorld();
  const b = { x0: Infinity, x1: -Infinity, y0: Infinity, y1: -Infinity };
  for (const c of pts) {
    const q = c.clone().project(camera);
    if (q.z >= 1) return null;
    b.x0 = Math.min(b.x0, q.x); b.x1 = Math.max(b.x1, q.x);
    b.y0 = Math.min(b.y0, q.y); b.y1 = Math.max(b.y1, q.y);
  }
  return b;
}

/**
 * Frame `box` in the part of the canvas right of the panel: alternately fit the distance (binary
 * search, the projected extent shrinks with distance) and pan `target` (in place, on its horizontal
 * plane) so the projected bounds are centred. Returns the camera distance.
 */
function frameBox(camera: THREE.PerspectiveCamera, target: THREE.Vector3, dir: THREE.Vector3, box: THREE.Box3, w: number, margin = 0.05): number {
  const pts = boxCorners(box);
  const size0 = box.getSize(new THREE.Vector3()).length();
  camera.far = size0 * 100; // fit without the far plane cutting the search short (the caller resets it)
  camera.updateProjectionMatrix();
  const xMin = -1 + (2 * panelInset(w)) / w + margin;
  const xMid = panelInset(w) / w;
  const fits = (d: number) => {
    const b = project(camera, target, dir, d, pts);
    return !!b && b.x0 >= xMin && b.x1 <= 1 - margin && b.y0 >= -1 + margin && b.y1 <= 1 - margin;
  };
  const size = box.getSize(new THREE.Vector3()).length();
  const fit = () => {
    let lo = size * 0.05, hi = size * 40; // a long lens on a phone-width canvas fits from far away
    for (let i = 0; i < 40; i++) {
      const mid = (lo + hi) / 2;
      if (fits(mid)) hi = mid; else lo = mid;
    }
    return hi;
  };
  const right = new THREE.Vector3();
  const forward = new THREE.Vector3(-dir.x, 0, -dir.z).normalize();
  const sinE = Math.max(0.2, dir.y / dir.length());
  let d = fit();
  for (let i = 0; i < 4; i++) {
    const b = project(camera, target, dir, d, pts);
    if (!b) break;
    const dx = xMid - (b.x0 + b.x1) / 2, dy = -(b.y0 + b.y1) / 2;
    if (Math.abs(dx) < 0.01 && Math.abs(dy) < 0.01) break;
    const hh = d * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2);
    right.setFromMatrixColumn(camera.matrixWorld, 0);
    // Moving the camera right moves the picture left; moving it forward moves the picture down.
    target.addScaledVector(right, -dx * hh * camera.aspect).addScaledVector(forward, (-dy * hh) / sinE);
    target.x = THREE.MathUtils.clamp(target.x, box.min.x, box.max.x);
    target.z = THREE.MathUtils.clamp(target.z, box.min.z, box.max.z);
    d = fit();
  }
  return d;
}

export async function createViewer(
  container: HTMLElement, url: string, info: CityInfo | null, onProgress?: (p: number) => void,
): Promise<Viewer> {
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.95;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  container.append(renderer.domElement);

  const loader = new GLTFLoader();
  loader.setMeshoptDecoder(MeshoptDecoder);
  const gltf = await loader.loadAsync(url, (e) => e.total && onProgress?.(e.loaded / e.total));
  const model = gltf.scene;

  const box = new THREE.Box3().setFromObject(model);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = 0.5 * Math.hypot(size.x, size.z);
  const zr: [number, number] = info?.terrain_z ?? [box.min.y, box.max.y];
  const zRange: [number, number] = [zr[0], zr[0] + Math.max(60, Math.min(250, (info?.height_max ?? 200) * 0.7))];

  model.traverse((o) => {
    const m = o as THREE.Mesh;
    if (!m.isMesh) return;
    const mat = m.material as THREE.MeshStandardMaterial;
    const isBuilding = mat.name === 'buildings' || m.name.includes('building');
    m.castShadow = isBuilding;
    m.receiveShadow = true;
    if (mat.map) mat.map.anisotropy = renderer.capabilities.getMaxAnisotropy();
    if (isBuilding) shadeBuildings(mat, zRange);
  });

  const scene = new THREE.Scene();
  scene.background = SKY_HORIZON.clone();
  scene.fog = new THREE.Fog(SKY_HORIZON, radius * 1.6, radius * 7);
  scene.add(model);
  scene.add(new THREE.HemisphereLight('#d6e6ff', '#7a6d5c', 0.85));

  const sunCfg = info?.sun ?? {};
  const sun = new THREE.DirectionalLight('#fff1dc', 3.2 * ((sunCfg.strength ?? 4) / 4));
  const sunDir = direction(sunCfg.azimuth ?? 240, sunCfg.elevation ?? 25);
  sun.position.copy(center).addScaledVector(sunDir, radius * 2);
  sun.target.position.copy(center);
  sun.castShadow = true;
  sun.shadow.mapSize.set(4096, 4096);
  const sc = sun.shadow.camera;
  sc.left = sc.bottom = -radius * 1.05;
  sc.right = sc.top = radius * 1.05;
  sc.near = radius * 0.2;
  sc.far = radius * 4;
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = (2 * radius) / 4096;
  scene.add(sun, sun.target);

  const sky = skyDome(radius * 9);
  scene.add(sky);

  const camCfg = info?.camera ?? {};
  const fov = THREE.MathUtils.radToDeg(2 * Math.atan(24 / (2 * (camCfg.focal_mm ?? 35))));
  const camera = new THREE.PerspectiveCamera(fov, container.clientWidth / container.clientHeight, radius / 2000, radius * 12);
  const target = new THREE.Vector3(center.x, zr[0] + 0.25 * (zr[1] - zr[0]), center.z);
  const viewDir = direction(camCfg.azimuth ?? 200, camCfg.elevation ?? 32);
  applyViewOffset(camera, container.clientWidth, container.clientHeight);
  // Whole model in view; config distances above 1 pull back further (below 1 would crop it here).
  const dist = frameBox(camera, target, viewDir, box, container.clientWidth) * Math.max(1, camCfg.distance ?? 1);
  camera.position.copy(target).addScaledVector(viewDir, dist);
  camera.lookAt(target);
  // fog, far plane and sky dome follow the fitted distance (on a phone it can exceed the 7-radius fog)
  scene.fog = new THREE.Fog(SKY_HORIZON, Math.max(radius * 1.6, dist * 0.75), Math.max(radius * 7, dist * 3.2));
  camera.far = Math.max(radius * 12, dist * 6);
  camera.updateProjectionMatrix();
  sky.scale.setScalar((camera.far * 0.75) / (radius * 9));

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.copy(target);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.maxPolarAngle = THREE.MathUtils.degToRad(86);
  controls.minDistance = radius * 0.03;
  // a narrow (phone) viewport fits the model from further away than 5 radii: never clamp the start view
  controls.maxDistance = Math.max(radius * 5, dist * 1.5);
  controls.screenSpacePanning = false;
  controls.update();

  const resize = () => {
    const w = container.clientWidth, h = container.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    applyViewOffset(camera, w, h);
    controls.maxDistance = Math.max(controls.maxDistance, camera.position.distanceTo(controls.target) * 1.5);
  };
  window.addEventListener('resize', resize);

  let frameWaiters: (() => void)[] = [];
  const frame = () => {
    controls.update();
    sky.position.copy(camera.position);
    renderer.render(scene, camera);
    if (frameWaiters.length) {
      const w = frameWaiters;
      frameWaiters = [];
      requestAnimationFrame(() => w.forEach((f) => f()));
    }
  };
  return {
    start: () => { resize(); renderer.setAnimationLoop(frame); },
    stop: () => renderer.setAnimationLoop(null),
    firstFrame: () => new Promise<void>((r) => frameWaiters.push(r)),
  };
}
