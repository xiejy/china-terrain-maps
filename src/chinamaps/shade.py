"""Relief shading in float32: Horn gradients, multidirectional (MDOW) hillshade,
slope, and Leland Brown's texture shading (fractional Laplacian via texshade).

Conventions: p = dz/dx (east), q = dz/dy (north), rows run north to south.
"""

from __future__ import annotations

import math

import numpy as np

MDOW_AZIMUTHS = (225.0, 270.0, 315.0, 360.0)


def horn(z: np.ndarray, res: float, zf: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Horn (1981) 3x3 gradients (p east, q north) with edge replication."""
    zp = np.pad(z.astype(np.float32, copy=False), 1, mode="edge")
    a, b, c = zp[:-2, :-2], zp[:-2, 1:-1], zp[:-2, 2:]
    d, f = zp[1:-1, :-2], zp[1:-1, 2:]
    g, h, i = zp[2:, :-2], zp[2:, 1:-1], zp[2:, 2:]
    s = np.float32(zf / (8.0 * res))
    p = ((c + 2 * f + i) - (a + 2 * d + g)) * s
    q = ((a + 2 * b + c) - (g + 2 * h + i)) * s
    return p, q


def hillshade(p: np.ndarray, q: np.ndarray, azimuth: float, altitude: float) -> np.ndarray:
    """Lambertian shading (cos of incidence, clipped at 0) for light FROM azimuth."""
    az, alt = math.radians(azimuth), math.radians(altitude)
    lx = np.float32(math.sin(az) * math.cos(alt))
    ly = np.float32(math.cos(az) * math.cos(alt))
    lz = np.float32(math.sin(alt))
    num = lz - p * lx - q * ly
    return np.maximum(num, 0) / np.sqrt(1 + p * p + q * q)


def mdow_weights(p: np.ndarray, q: np.ndarray) -> tuple[np.ndarray, ...]:
    """gdaldem -multidirectional weights for azimuths 225/270/315/360, summing to 1.

    With GDAL's gradient x = -p, y = q: w225 = (x^2+y^2)/2 - xy, w270 = x^2,
    w315 = (x^2+y^2) - w225, w360 = y^2. Flat cells get equal weights.
    """
    x, y = -p, q
    r2 = x * x + y * y
    w225 = np.float32(0.5) * r2 - x * y
    w270 = x * x
    w315 = r2 - w225
    w360 = y * y
    total = np.float32(2.0) * r2
    flat = total <= np.float32(1e-12)
    inv = np.where(flat, np.float32(0), np.float32(1) / np.where(flat, np.float32(1), total))
    quarter = np.float32(0.25)
    return tuple(np.where(flat, quarter, w * inv) for w in (w225, w270, w315, w360))


def mdow(p: np.ndarray, q: np.ndarray, altitude: float = 45.0) -> np.ndarray:
    """Multidirectional oblique-weighted hillshade in [0, 1] (flat = sin(altitude))."""
    out = np.zeros(p.shape, dtype=np.float32)
    for az, w in zip(MDOW_AZIMUTHS, mdow_weights(p, q)):
        out += w * hillshade(p, q, az, altitude)
    return out


def slope_deg(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    return np.degrees(np.arctan(np.sqrt(p * p + q * q))).astype(np.float32)


def texture_shade(z: np.ndarray, alpha: float = 0.7, pct: tuple[float, float] = (1.0, 99.0),
                  mask: np.ndarray | None = None) -> np.ndarray:
    """Texture shading scaled to [-1, 1] by the given percentiles (0 stays 0).

    Percentiles are taken over mask (e.g. land) when given. The grid is
    mirror-padded before the FFT so opposite edges do not wrap.
    """
    from texshade import texshadeFFT

    z = z.astype(np.float32, copy=False)
    pad = int(min(512, max(16, min(z.shape) // 4)))
    zp = np.pad(z - np.float32(z.mean()), pad, mode="reflect")
    t = texshadeFFT(zp, alpha)[pad:-pad, pad:-pad].astype(np.float32)
    del zp
    step = (max(1, t.shape[0] // 1500), max(1, t.shape[1] // 1500))
    sub = t[:: step[0], :: step[1]]
    if mask is not None and mask[:: step[0], :: step[1]].any():
        sub = sub[mask[:: step[0], :: step[1]]]
    lo, hi = np.percentile(sub, pct)
    s = np.float32(max(abs(lo), abs(hi), 1e-9))
    np.clip(t / s, -1, 1, out=t)
    return t


def default_zfactor(res: float) -> float:
    """Hillshade vertical exaggeration for a pixel size: ~1 at 30 m, ~3.5 at 500 m."""
    return float(np.clip((res / 30.0) ** 0.45, 1.0, 6.0))
