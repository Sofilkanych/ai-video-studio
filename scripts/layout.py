"""Геометрія кадру — єдине джерело правди для Remotion і геометричного QA.

Python обчислює прямокутники (у пікселях кадру) для кожної сцени й передає їх у props;
Remotion лише малює, QA перевіряє ті самі числа.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

Rect = tuple[float, float, float, float]  # x, y, w, h


@dataclass
class SceneGeometry:
    slide: Rect | None
    presenter: Rect | None
    presenter_shape: str | None
    caption: Rect

    def to_props(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()}


def _fit(aspect: float, box: Rect) -> Rect:
    x, y, w, h = box
    if w / h > aspect:
        nw = h * aspect
        return (x + (w - nw) / 2, y, nw, h)
    nh = w / aspect
    return (x, y + (h - nh) / 2, w, nh)


def _presenter_rect(position: str, shape: str, scale: float, W: int, H: int, m: float) -> Rect:
    h = scale * H
    w = h if shape == "circle" else h * 4 / 3
    x = W - m - w if "right" in position else m
    y = H - m - h if "bottom" in position else m
    return (x, y, w, h)


def scene_geometry(scene: dict[str, Any], W: int, H: int, slide_aspect: float,
                   has_presenter: bool, caption_band: float = 0.12) -> SceneGeometry:
    m = round(0.02 * H)
    layout = scene["layout"]
    pres = scene.get("presenter") if has_presenter else None
    shape = pres["shape"] if pres else None
    band = caption_band * H

    if layout == "presenter_full" and has_presenter:
        return SceneGeometry(None, (0, 0, W, H), "rect", (m, H - m - band, W - 2 * m, band))

    if layout == "side_by_side" and has_presenter:
        slide = _fit(slide_aspect, (m, m, 0.62 * W, H - 2 * m - band))
        px = slide[0] + slide[2] + m
        pw = W - px - m
        ph = pw * 3 / 4
        presenter = (px, slide[1] + (slide[3] - ph) / 2, pw, ph)
        return SceneGeometry(slide, presenter, "rect",
                             (m, slide[1] + slide[3] + m / 2, W - 2 * m, H - (slide[1] + slide[3]) - 1.5 * m))

    if layout == "slide_inset" and pres:
        p = _presenter_rect(pres["position"], pres["shape"], pres["scale"], W, H, m)
        max_w = W - 3 * m - p[2]
        hs = H - 2 * m - band
        ws = min(hs * slide_aspect, max_w)
        hs = ws / slide_aspect
        x = m if "right" in pres["position"] else W - m - ws
        slide = (x, m, ws, hs)
        cap = (x, m + hs + m / 2, ws, H - (m + hs + m / 2) - m)
        return SceneGeometry(slide, p, pres["shape"], cap)

    if layout == "slide_inset":  # без ведучого: слайд по центру, субтитри під ним
        slide = _fit(slide_aspect, (m, m, W - 2 * m, H - 2 * m - band))
        cap = (slide[0], slide[1] + slide[3] + m / 2, slide[2], H - (slide[1] + slide[3] + m / 2) - m)
        return SceneGeometry(slide, None, None, cap)

    # slide_full / free_zone (Фаза 2)
    slide = _fit(slide_aspect, (0, 0, W, H))
    p = _presenter_rect(pres["position"], pres["shape"], pres["scale"], W, H, m) if pres else None
    return SceneGeometry(slide, p, shape, (m, H - m - band, W - 2 * m, band))


def caption_band(tl: dict[str, Any]) -> float:
    """Смуга під субтитри (частка висоти кадру); без субтитрів у відео — 0, слайд більший."""
    return 0.12 if (tl.get("captions") or {}).get("enabled", True) else 0.0


def to_slide_norm(rect: Rect, slide: Rect) -> Rect:
    """Прямокутник кадру → координати 0..1 відносно слайда."""
    sx, sy, sw, sh = slide
    x, y, w, h = rect
    return ((x - sx) / sw, (y - sy) / sh, w / sw, h / sh)


def bbox_to_frame(bbox: list[float], slide: Rect, png_w: int, png_h: int) -> Rect:
    """bbox елемента (px PNG) → прямокутник кадру."""
    sx, sy, sw, sh = slide
    x, y, w, h = bbox
    return (sx + x / png_w * sw, sy + y / png_h * sh, w / png_w * sw, h / png_h * sh)


def intersects(a: Rect, b: Rect) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
