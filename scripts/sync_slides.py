"""Крок Slide Sync: моменти перемикання слайдів з confidence.

Джерела за надійністю: screen_match (кадри відео ↔ PNG слайдів) > verbal_cue («наступний слайд»)
> text_alignment (монотонне вирівнювання тексту мови з текстом слайдів, детерміноване).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from scripts.common import ROOT, WORK, validate, write_json
from scripts.state import Context, StepError

SYNC = WORK / "sync"
THUMB = (64, 36)
NEXT_CUES = re.compile(r"\b(наступн\w*\s+слайд\w*|переходимо\s+до\s+наступн\w*|далі\s+слайд\w*|next\s+slide)",
                       re.IGNORECASE)
PREV_CUES = re.compile(r"\b(попередн\w*\s+слайд\w*|повернемос\w*\s+до\s+попередн\w*|previous\s+slide)",
                       re.IGNORECASE)
WORD = re.compile(r"[\w']+", re.UNICODE)


# ---------- screen match ----------

def _thumb(png: Path) -> np.ndarray:
    a = np.asarray(Image.open(png).convert("L").resize(THUMB, Image.BILINEAR), dtype=np.float32)
    return (a - a.mean()) / (a.std() + 1e-6)


def _video_thumbs(video: Path, fps: float = 2.0) -> np.ndarray:
    w, h = THUMB
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video), "-vf",
                        f"fps={fps},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=1800)
    frames = np.frombuffer(p.stdout, dtype=np.uint8).reshape(-1, h, w).astype(np.float32)
    mean = frames.mean(axis=(1, 2), keepdims=True)
    std = frames.std(axis=(1, 2), keepdims=True) + 1e-6
    return (frames - mean) / std


def screen_match(video: Path, slides: list[dict[str, Any]], fps: float = 2.0) -> list[dict[str, Any]] | None:
    thumbs = np.stack([_thumb(ROOT / s["image"]) for s in slides])
    frames = _video_thumbs(video, fps)
    if len(frames) == 0:
        return None
    corr = np.einsum("fhw,shw->fs", frames, thumbs) / (THUMB[0] * THUMB[1])
    best, score = corr.argmax(axis=1), corr.max(axis=1)
    if (score > 0.85).mean() < 0.7:  # слайдів у відео немає (камера, а не екран)
        return None
    # медіанне згладжування міток, перехід — лише якщо новий слайд тримається ≥ 1 с
    k = 5
    padded = np.pad(best, k // 2, mode="edge")
    smooth = np.array([np.bincount(padded[i:i + k]).argmax() for i in range(len(best))])
    out, current = [], None
    for i, label in enumerate(smooth):
        if label != current and (smooth[i:i + int(fps)] == label).all():
            out.append({"slide": slides[label]["slide"], "start": round(i / fps, 3),
                        "source": "screen_match", "confidence": round(float(score[i]), 3)})
            current = label
    if out:
        out[0]["start"] = 0.0
    return out


# ---------- verbal cues + text alignment ----------

def _stems(text: str) -> set[str]:
    return {w.lower()[:5] for w in WORD.findall(text or "") if len(w) >= 4 or w.isdigit()}


def _slide_text(slide: dict[str, Any]) -> str:
    parts = [slide.get("title") or "", slide.get("notes") or ""]
    for e in slide["elements"]:
        if e.get("text"):
            parts.append(e["text"])
        if e.get("chart"):
            parts += [str(c) for c in e["chart"].get("categories", [])]
            parts += [s["name"] for s in e["chart"].get("series", [])]
    return " ".join(parts)


def _units(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """Одиниці вирівнювання: речення (за пунктуацією) з часом першого слова."""
    units, cur = [], []
    for seg in transcript["segments"]:
        for w in seg["words"]:
            cur.append(w)
            if re.search(r"[.!?…]$", w["word"]):
                units.append(cur)
                cur = []
    if cur:
        units.append(cur)
    return [{"start": u[0]["start"], "end": u[-1]["end"], "text": " ".join(w["word"] for w in u)} for u in units]


def verbal_cues(units: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """[(індекс одиниці, +1/-1)] для фраз «наступний/попередній слайд»."""
    cues = []
    for i, u in enumerate(units):
        if NEXT_CUES.search(u["text"]):
            cues.append((i, +1))
        elif PREV_CUES.search(u["text"]):
            cues.append((i, -1))
    return cues


def align(units: list[dict[str, Any]], slides: list[dict[str, Any]], cue_units: set[int]) -> list[int]:
    """Монотонне DP: кожній одиниці — індекс слайда, неспадний, від 0 до K-1."""
    n, k = len(units), len(slides)
    if n == 0:
        return []
    if n < k:  # мови менше, ніж слайдів — рівномірно
        return [min(k - 1, i * k // n) for i in range(n)]
    sl = [_stems(_slide_text(s)) for s in slides]
    sim = np.zeros((n, k))
    for i, u in enumerate(units):
        us = _stems(u["text"])
        for j in range(k):
            sim[i, j] = len(us & sl[j]) / (len(us) ** 0.5 + 1e-6) if us else 0
    cue_bonus, stay_penalty = 2.0, 0.0
    neg = -1e9
    dp = np.full((n, k), neg)
    back = np.zeros((n, k), dtype=int)
    dp[0, 0] = sim[0, 0]
    for i in range(1, n):
        for j in range(k):
            stay = dp[i - 1, j] - stay_penalty
            move = dp[i - 1, j - 1] + (cue_bonus if i in cue_units else 0) if j > 0 else neg
            if i in cue_units and j > 0:
                stay -= cue_bonus  # маркер «наступний слайд» без переходу штрафується
            if move > stay:
                dp[i, j], back[i, j] = move + sim[i, j], j - 1
            else:
                dp[i, j], back[i, j] = stay + sim[i, j], j
    path = [k - 1]
    for i in range(n - 1, 0, -1):
        path.append(back[i, path[-1]])
    return path[::-1]


def text_sync(transcript: dict[str, Any], slides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units = _units(transcript)
    cues = verbal_cues(units)
    k = len(slides)
    nexts = [i for i, d in cues if d > 0]
    if cues and len(nexts) == k - 1 and all(d > 0 for _, d in cues):
        # повний набір маркерів — найнадійніший текстовий сигнал
        out = [{"slide": slides[0]["slide"], "start": 0.0, "source": "verbal_cue", "confidence": 0.9}]
        for n_idx, unit_idx in enumerate(nexts, start=1):
            out.append({"slide": slides[n_idx]["slide"], "start": _cue_time(units, unit_idx),
                        "source": "verbal_cue", "confidence": 0.85})
        return out
    path = align(units, slides, set(nexts))
    out, current = [], None
    for i, j in enumerate(path):
        if j != current:
            src = "verbal_cue" if i in nexts else "text_alignment"
            conf = 0.8 if src == "verbal_cue" else 0.5
            out.append({"slide": slides[j]["slide"], "start": 0.0 if not out else _cue_time(units, i),
                        "source": src, "confidence": conf})
            current = j
    shown = {t["slide"] for t in out}
    missing = [s["slide"] for s in slides if s["slide"] not in shown]
    if missing:  # DP пропустив слайди (n < k або нульова схожість) — не мовчимо
        for t in out:
            t["confidence"] = min(t["confidence"], 0.3)
    return out


def _cue_time(units: list[dict[str, Any]], i: int) -> float:
    """Перехід трохи раніше за першу фразу нового слайда, але не раніше кінця попередньої."""
    start = units[i]["start"]
    prev_end = units[i - 1]["end"] if i > 0 else 0.0
    return round(max(prev_end, start - 0.2), 3)


def run(job: dict[str, Any], ctx: Context) -> str:
    if job.get("mode") == "script":
        # таймкоди слайдів уже точно відомі з синтезу голосу
        if not (SYNC / "slide_timings.json").exists():
            raise StepError("режим script: немає slide_timings.json — спершу крок synthesize")
        ctx.log("  синхронізація: зі сценарію (точна)")
        return "SLIDES_SYNCED"
    manifest = json.loads((WORK / "slides" / "slide_manifest.json").read_text(encoding="utf-8"))
    transcript = json.loads((WORK / "transcript" / "transcript.json").read_text(encoding="utf-8"))
    media = json.loads((WORK / "media" / "media.json").read_text(encoding="utf-8"))
    slides = [s for s in manifest["slides"] if not s["hidden"]]
    if not slides:
        raise StepError("немає видимих слайдів")

    transitions = None
    sources = []
    if "screen_recording" in job["inputs"]:
        sources.append(ROOT / job["inputs"]["screen_recording"]["path"])
    if media.get("presenter"):
        sources.append(ROOT / media["presenter"])
    for video in sources:
        transitions = screen_match(video, slides)
        if transitions:
            ctx.log(f"  синхронізація: збіг кадрів ({video.name})")
            break
    if not transitions:
        transitions = text_sync(transcript, slides)
        ctx.log(f"  синхронізація: {transitions[1]['source'] if len(transitions) > 1 else 'один слайд'}")

    result = {"duration": media["master_duration"], "transitions": transitions}
    errors = validate("slide_timings", result)
    if errors:
        raise StepError("slide_timings: " + "; ".join(errors))
    SYNC.mkdir(parents=True, exist_ok=True)
    write_json(SYNC / "slide_timings.json", result)
    for t in transitions:
        ctx.log(f"    {t['start']:7.2f} с → слайд {t['slide']} ({t['source']}, {t['confidence']})")
    return "SLIDES_SYNCED"
