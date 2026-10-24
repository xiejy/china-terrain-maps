"""Pure-numpy geometry shared by the Blender scripts (no bpy, so it is unit-testable).

Blender frame for scenes: x = east, y = north, z = up. Compass azimuths are
degrees clockwise from north.
"""

from __future__ import annotations

import math

import numpy as np

SENSOR_MM = 36.0
WORLD_UP = np.array([0.0, 0.0, 1.0])


def compass_vector(azimuth_deg: float, elevation_deg: float) -> np.ndarray:
    """Unit vector pointing toward compass azimuth at an elevation above the horizon."""
    a, e = math.radians(azimuth_deg), math.radians(elevation_deg)
    return np.array([math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), math.sin(e)])


def look_basis(forward: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(right, up, forward) unit vectors of a camera looking along forward with world Z up."""
    f = np.asarray(forward, float)
    f = f / np.linalg.norm(f)
    r = np.cross(f, WORLD_UP)
    if np.linalg.norm(r) < 1e-9:  # looking straight down: north is up in the image
        r = np.array([1.0, 0.0, 0.0])
    r = r / np.linalg.norm(r)
    return r, np.cross(r, f), f


def half_tans(focal_mm: float, aspect: float, sensor_mm: float = SENSOR_MM) -> tuple[float, float]:
    """tan(half FOV) horizontally and vertically for Blender's AUTO sensor fit."""
    t = sensor_mm / (2 * focal_mm)
    return (t, t / aspect) if aspect >= 1 else (t * aspect, t)


def project_tan(points: np.ndarray, cam: np.ndarray, basis) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Points -> (x/depth, y/depth, depth) in camera tangent space."""
    r, u, f = basis
    d = np.asarray(points, float) - cam
    depth = d @ f
    return (d @ r) / depth, (d @ u) / depth, depth


def fit_camera(points: np.ndarray, target: np.ndarray, view_dir: np.ndarray, focal_mm: float, aspect: float,
               distance: float = 1.0, margins: tuple[float, float, float, float] = (0.03, 0.03, 0.03, 0.03),
               sensor_mm: float = SENSOR_MM) -> dict:
    """Camera distance and lens shift so points fill the frame inside margins.

    view_dir points from target to camera. margins = (left, right, bottom, top) as fractions of
    the frame size. The fitted distance is multiplied by `distance` (>1 zooms out), then the lens
    shift centres the projected points in the margin box. Returns dict(location, distance,
    shift_x, shift_y, near, far, basis).
    """
    pts = np.asarray(points, float)
    target = np.asarray(target, float)
    d = np.asarray(view_dir, float) / np.linalg.norm(view_dir)
    basis = look_basis(-d)
    tx, ty = half_tans(focal_mm, aspect, sensor_mm)
    ml, mr, mb, mt = margins
    avail_w, avail_h = 2 * tx * (1 - ml - mr), 2 * ty * (1 - mb - mt)
    rel = pts - target
    lo = float((rel @ d).max()) + 1e-6 * float(np.ptp(rel, axis=0).max() + 1)
    lo = max(lo, 1e-6)

    def fits(dist: float) -> bool:
        x, y, _ = project_tan(pts, target + dist * d, basis)
        return np.ptp(x) <= avail_w and np.ptp(y) <= avail_h

    hi = lo * 2 + 1.0
    while not fits(hi):
        hi *= 2
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if fits(mid) else (mid, hi)
    dist = hi * distance
    cam = target + dist * d
    x, y, depth = project_tan(pts, cam, basis)
    cx, cy = (x.min() + x.max()) / 2, (y.min() + y.max()) / 2
    big = max(tx, ty)
    fx, fy = cx - (ml - mr) * tx, cy - (mb - mt) * ty
    return {"location": cam, "distance": dist, "shift_x": fx / (2 * big), "shift_y": fy / (2 * big),
            "near": float(depth.min()), "far": float(depth.max()), "basis": basis}


def to_pixels(points: np.ndarray, cam: np.ndarray, basis, focal_mm: float, width: int, height: int,
              shift_x: float = 0.0, shift_y: float = 0.0, sensor_mm: float = SENSOR_MM) -> np.ndarray:
    """Project points to pixel coordinates (x right, y down, origin top-left) plus depth.

    Mirrors bpy_extras.object_utils.world_to_camera_view for an AUTO-fit perspective camera.
    """
    tx, ty = half_tans(focal_mm, width / height, sensor_mm)
    big = max(tx, ty)
    x, y, depth = project_tan(points, cam, basis)
    u = (x - shift_x * 2 * big) / (2 * tx) + 0.5
    v = (y - shift_y * 2 * big) / (2 * ty) + 0.5
    return np.column_stack([u * width, (1 - v) * height, depth])


def in_frame(px: float, py: float, width: int, height: int, pad: float = 0.0) -> bool:
    return -pad <= px <= width + pad and -pad <= py <= height + pad


def in_polygon(x: np.ndarray, y: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Crossing-number point-in-polygon test (poly: (k, 2) vertices)."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    inside = np.zeros(x.shape, bool)
    for (xa, ya), (xb, yb) in zip(poly, np.roll(poly, -1, axis=0)):
        crosses = (ya > y) != (yb > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = xa + (y - ya) * (xb - xa) / (yb - ya)
        inside ^= crosses & (x < xi)
    return inside


def in_frame_region(fx: np.ndarray, fy: np.ndarray, frame) -> np.ndarray:
    """Mask of points (scene fractions from the SW corner) inside a camera `frame`:
    [x0, y0, x1, y1] or a polygon [[x, y], ...]."""
    poly = np.asarray(frame, float)
    if poly.ndim == 1:
        x0, y0, x1, y1 = poly
        poly = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
    return in_polygon(fx, fy, poly)


def block_mean(a: np.ndarray, f: int) -> np.ndarray:
    """Block-average by an integer factor (trailing rows/cols that do not fill a block are dropped)."""
    if f <= 1:
        return a.astype(np.float32, copy=False)
    h, w = (a.shape[0] // f) * f, (a.shape[1] // f) * f
    return a[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3), dtype=np.float64).astype(np.float32)


def gaussian_blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian blur with edge replication (numpy only: Blender's Python has no scipy)."""
    if sigma <= 0:
        return np.asarray(a, np.float32)
    r = max(1, math.ceil(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    out = np.asarray(a, np.float64)
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r, r)
        p = np.pad(out, pad, mode="edge")
        n = out.shape[axis]
        acc = np.zeros_like(out)
        for i, w in enumerate(k):
            acc += w * (p[i:i + n] if axis == 0 else p[:, i:i + n])
        out = acc
    return out.astype(np.float32)


def shape_relief(z: np.ndarray, smooth_px: float = 0.0, sharpen: dict | None = None) -> np.ndarray:
    """Mesh relief clean-up: Gaussian smoothing (removes the needle field of a coarse, strongly
    exaggerated mesh) and/or an unsharp mask {sigma, amount} (steepens karst towers)."""
    z = np.asarray(z, np.float32)
    if smooth_px:
        z = gaussian_blur(z, float(smooth_px))
    if sharpen:
        z = z + float(sharpen.get("amount", 0.8)) * (z - gaussian_blur(z, float(sharpen.get("sigma", 3.0))))
    return z.astype(np.float32)


def decimation_factor(shape: tuple[int, int], mesh_max: int) -> int:
    return max(1, math.ceil(max(shape) / max(2, mesh_max)))


def grid_faces(h: int, w: int) -> np.ndarray:
    """Quad vertex indices (CCW seen from +Z) for an h x w grid with row 0 = north."""
    i = np.arange(h * w, dtype=np.int32).reshape(h, w)
    return np.stack([i[:-1, :-1], i[1:, :-1], i[1:, 1:], i[:-1, 1:]], axis=-1).reshape(-1, 4)


def bilinear(grid: np.ndarray, col: np.ndarray, row: np.ndarray) -> np.ndarray:
    """Bilinear sample of grid at fractional (col, row) pixel-centre coordinates (edge clamped)."""
    h, w = grid.shape
    c = np.clip(np.asarray(col, float), 0, w - 1)
    r = np.clip(np.asarray(row, float), 0, h - 1)
    c0, r0 = np.minimum(c.astype(int), w - 2 if w > 1 else 0), np.minimum(r.astype(int), h - 2 if h > 1 else 0)
    c1, r1 = np.minimum(c0 + 1, w - 1), np.minimum(r0 + 1, h - 1)
    fc, fr = c - c0, r - r0
    top = grid[r0, c0] * (1 - fc) + grid[r0, c1] * fc
    bot = grid[r1, c0] * (1 - fc) + grid[r1, c1] * fc
    return top * (1 - fr) + bot * fr


def srgb_to_linear(c) -> np.ndarray:
    c = np.asarray(c, float)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def grade(rgb: np.ndarray, saturation: float = 1.0, contrast: float = 0.0) -> np.ndarray:
    """Saturation scale about Rec.709 luma, then an endpoint-preserving S-curve (0..255 floats)."""
    x = np.asarray(rgb, np.float32) / 255.0
    if saturation != 1.0:
        lum = x @ np.array([0.2126, 0.7152, 0.0722], np.float32)
        x = np.clip(lum[..., None] + (x - lum[..., None]) * saturation, 0.0, 1.0)
    if contrast:
        x = (1 - contrast) * x + contrast * (0.5 - 0.5 * np.cos(np.pi * x))
    return x * 255.0
