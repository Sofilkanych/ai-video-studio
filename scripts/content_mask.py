"""Маска вмісту слайда з пікселів PNG.

Фон — медіанний колір рамки слайда; вміст — пікселі, що помітно відрізняються від фону,
розширені на кілька пікселів. Маска зберігається у зменшеному розмірі (ширина MASK_WIDTH).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

MASK_WIDTH = 480
DIFF_THRESHOLD = 28
DILATE = 3


@dataclass
class MaskInfo:
    path: Path
    bg_color: str
    dark_background: bool
    coverage: float


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
    return out


def build_mask(png: Path, out: Path) -> MaskInfo:
    img = Image.open(png).convert("RGB")
    small = img.resize((MASK_WIDTH, round(img.height * MASK_WIDTH / img.width)), Image.BILINEAR)
    a = np.asarray(small).astype(np.int16)
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    bg = np.median(border, axis=0)
    diff = np.abs(a - bg).max(axis=2)
    mask = _dilate(diff > DIFF_THRESHOLD, DILATE)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray((mask * 255).astype(np.uint8)).save(out)
    luminance = (0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]) / 255
    return MaskInfo(out, "#{:02x}{:02x}{:02x}".format(*(int(c) for c in bg)),
                    bool(luminance < 0.35), float(mask.mean()))


def load_mask(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path)) > 127


def overlap_fraction(mask: np.ndarray, rect_norm: tuple[float, float, float, float]) -> float:
    """Частка пікселів вмісту всередині прямокутника (координати 0..1 відносно слайда)."""
    h, w = mask.shape
    x, y, rw, rh = rect_norm
    x0, y0 = max(0, int(x * w)), max(0, int(y * h))
    x1, y1 = min(w, int((x + rw) * w + 0.999)), min(h, int((y + rh) * h + 0.999))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return float(mask[y0:y1, x0:x1].mean())
