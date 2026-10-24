/** Map icons drawn on a canvas at runtime (no sprite sheet needed). */
import type { Map as MLMap } from 'maplibre-gl';

type Draw = (ctx: CanvasRenderingContext2D, s: number) => void;

const ICONS: Record<string, { size: number; draw: Draw }> = {
  dot: {
    size: 14,
    draw: (c, s) => {
      c.beginPath();
      c.arc(s / 2, s / 2, s * 0.24, 0, Math.PI * 2);
      c.fillStyle = '#ffffff';
      c.fill();
      c.lineWidth = s * 0.12;
      c.strokeStyle = '#2b2f36';
      c.stroke();
    },
  },
  'dot-minor': {
    size: 12,
    draw: (c, s) => {
      c.beginPath();
      c.arc(s / 2, s / 2, s * 0.2, 0, Math.PI * 2);
      c.fillStyle = '#5d636b';
      c.fill();
      c.lineWidth = s * 0.1;
      c.strokeStyle = 'rgba(255,255,255,0.9)';
      c.stroke();
    },
  },
  capital: {
    size: 22,
    draw: (c, s) => {
      // Star in a ring, after the usual Chinese map symbol for the national capital.
      c.beginPath();
      c.arc(s / 2, s / 2, s * 0.42, 0, Math.PI * 2);
      c.fillStyle = '#ffffff';
      c.fill();
      c.lineWidth = s * 0.08;
      c.strokeStyle = '#b3202c';
      c.stroke();
      c.beginPath();
      for (let i = 0; i < 10; i++) {
        const r = i % 2 === 0 ? s * 0.32 : s * 0.13;
        const a = -Math.PI / 2 + (i * Math.PI) / 5;
        c.lineTo(s / 2 + r * Math.cos(a), s / 2 + r * Math.sin(a));
      }
      c.closePath();
      c.fillStyle = '#c8202c';
      c.fill();
    },
  },
  'capital-minor': {
    size: 16,
    draw: (c, s) => {
      c.beginPath();
      c.arc(s / 2, s / 2, s * 0.36, 0, Math.PI * 2);
      c.fillStyle = '#ffffff';
      c.fill();
      c.lineWidth = s * 0.1;
      c.strokeStyle = '#5d636b';
      c.stroke();
      c.beginPath();
      c.arc(s / 2, s / 2, s * 0.17, 0, Math.PI * 2);
      c.fillStyle = '#5d636b';
      c.fill();
    },
  },
  'province-capital': {
    size: 16,
    draw: (c, s) => {
      c.beginPath();
      c.arc(s / 2, s / 2, s * 0.36, 0, Math.PI * 2);
      c.fillStyle = '#ffffff';
      c.fill();
      c.lineWidth = s * 0.1;
      c.strokeStyle = '#2b2f36';
      c.stroke();
      c.beginPath();
      c.arc(s / 2, s / 2, s * 0.15, 0, Math.PI * 2);
      c.fillStyle = '#2b2f36';
      c.fill();
    },
  },
  peak: {
    size: 16,
    draw: (c, s) => {
      c.beginPath();
      c.moveTo(s / 2, s * 0.14);
      c.lineTo(s * 0.88, s * 0.82);
      c.lineTo(s * 0.12, s * 0.82);
      c.closePath();
      c.fillStyle = '#5a3a1e';
      c.fill();
      c.lineWidth = s * 0.08;
      c.strokeStyle = 'rgba(255,250,240,0.95)';
      c.stroke();
    },
  },
  landmark: {
    size: 18,
    draw: (c, s) => {
      c.beginPath();
      c.arc(s / 2, s / 2, s * 0.34, 0, Math.PI * 2);
      c.fillStyle = '#d94f2b';
      c.fill();
      c.lineWidth = s * 0.1;
      c.strokeStyle = '#ffffff';
      c.stroke();
    },
  },
};

const RATIO = 2;

function render(id: string): ImageData | null {
  const icon = ICONS[id];
  if (!icon) return null;
  const s = icon.size * RATIO;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = s;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  icon.draw(ctx, s);
  return ctx.getImageData(0, 0, s, s);
}

/** Supply icons lazily whenever the style asks for one it does not have. */
export function registerIcons(map: MLMap): void {
  map.setMissingStyleImageResolver((id) => {
    if (map.hasImage(id)) return;
    const img = render(id);
    if (img) map.addImage(id, img, { pixelRatio: RATIO });
  });
}
