"""Рендер: одна композиція → чанки --frames --muted з кешем → concat → аудіо одним проходом."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from scripts.assets import PUBLIC, RENDERS, props_path, write_props
from scripts.common import ROOT, WORK, read_json, run_cmd, write_json
from scripts.media import MediaError, count_packets, ffmpeg, probe
from scripts.state import Context, StepError

REMOTION = ROOT / "remotion"
# змінювати при зміні параметрів команди рендеру — інвалідовує кеш чанків
RENDER_VERSION = "h264-bt709-muted-1"
BUNDLE = RENDERS / "bundle"


def _src_hash() -> str:
    h = hashlib.sha256()
    for p in sorted((REMOTION / "src").rglob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _public_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(PUBLIC.rglob("*")):
        if p.is_file():
            st = p.stat()
            h.update(f"{p.relative_to(PUBLIC)}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


def ensure_bundle(ctx: Context) -> Path:
    key = f"{_src_hash()}-{_public_hash()}"
    stamp = BUNDLE / ".key"
    if stamp.exists() and stamp.read_text() == key:
        return BUNDLE
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    r = run_cmd(["npx", "remotion", "bundle", "src/index.ts", "--out-dir", str(BUNDLE),
                 "--public-dir", str(PUBLIC), "--log=error"], timeout=900, cwd=REMOTION)
    if not r.ok:
        raise StepError(f"remotion bundle: {(r.stderr or r.stdout).strip()[-500:]}")
    stamp.write_text(key)
    ctx.log("  bundle оновлено")
    return BUNDLE


def plan_chunks(tl: dict[str, Any], chunk_seconds: float) -> list[tuple[int, int]]:
    """Межі чанків — на межах сцен, ~chunk_seconds кожен. [(перший кадр, останній кадр)]."""
    fps = tl["fps"]
    total = round(tl["duration"] * fps)
    cuts = sorted({round(s["start"] * fps) for s in tl["scenes"]} | {total})
    chunks, start = [], 0
    for c in cuts:
        if c <= start:
            continue
        if (c - start) / fps >= chunk_seconds or c == total:
            chunks.append((start, c - 1))
            start = c
    return chunks


def chunk_hash(props: dict[str, Any], a: int, b: int, profile: dict[str, Any], src_hash: str,
               public_hash: str) -> str:
    """Хеш вмісту чанка: сцени й субтитри, що перетинають діапазон, + спільні дані + код + профіль."""
    tl = props["timeline"]
    fps = tl["fps"]
    t0, t1 = a / fps, (b + 1) / fps
    scenes = [s for s in tl["scenes"] if s["start"] < t1 and s["end"] > t0]
    ids = {s["scene_id"] for s in scenes}
    payload = {
        "range": [a, b], "fps": fps, "size": [tl["width"], tl["height"]], "duration": tl["duration"],
        "captions_cfg": tl.get("captions"), "render_version": RENDER_VERSION,
        "scenes": scenes, "geometry": {k: v for k, v in props["geometry"].items() if k in ids},
        "captions": [c for c in props["captions"] if c["start"] < t1 and c["end"] > t0],
        "slides": props["slides"], "elements": props["elements"], "presenter": props["presenter"],
        "style": props["style"], "profile": profile, "src": src_hash, "public": public_hash,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


def normalized_audio(cfg: dict[str, Any]) -> Path:
    """Master WAV → двопрохідний loudnorm → AAC (кешується за параметрами)."""
    master = WORK / "media" / "master.wav"
    target_i, target_tp = cfg["audio"]["loudness_lufs"], cfg["audio"]["true_peak_dbtp"]
    out = WORK / "media" / f"master_norm_{abs(target_i)}_{abs(target_tp)}_m.m4a"
    if out.exists() and out.stat().st_mtime >= master.stat().st_mtime:
        return out
    # запас 0.5 dB на true peak: AAC-кодування трохи піднімає піки, а QA вимагає ≤ target_tp
    tp = target_tp - 0.5
    r = run_cmd(["ffmpeg", "-hide_banner", "-nostats", "-i", str(master), "-af",
                 f"loudnorm=I={target_i}:TP={tp}:LRA=11:print_format=json", "-f", "null", "-"], timeout=1800)
    text = r.stderr
    m = json.loads(text[text.rfind("{"):text.rfind("}") + 1])
    filt = (f"loudnorm=I={target_i}:TP={tp}:LRA=11:measured_I={m['input_i']}:"
            f"measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
            f"offset={m['target_offset']}:linear=true")
    ffmpeg(["-i", str(master), "-af", filt, "-ar", "48000", "-c:a", "aac", "-b:a", "192k", str(out)])
    return out


def render(job: dict[str, Any], ctx: Context, stage: str) -> Path:
    cfg = ctx.cfg
    version = job["timeline_version"]
    ppath = props_path(version)
    if not ppath.exists():
        write_props(job, version)
    props = read_json(ppath)
    tl = props["timeline"]
    fps = tl["fps"]
    profile = cfg["profiles"]["preview" if stage == "preview" else "final"]
    scale = profile["width"] / tl["width"]

    bundle = ensure_bundle(ctx)
    src_hash, public_hash = _src_hash(), _public_hash()
    cache = RENDERS / "chunks" / stage
    cache.mkdir(parents=True, exist_ok=True)
    chunks = plan_chunks(tl, cfg["render"]["chunk_seconds"])
    files, state = [], []
    rendered = reused = 0
    for a, b in chunks:
        h = chunk_hash(props, a, b, profile, src_hash, public_hash)
        out = cache / f"{a:06d}-{b:06d}_{h}.mp4"
        if not out.exists():
            tmp = out.with_suffix(".part.mp4")
            r = run_cmd(["npx", "remotion", "render", str(bundle), "Main", str(tmp), f"--props={ppath}",
                         f"--frames={a}-{b}", "--muted", "--codec=h264", f"--crf={profile['crf']}",
                         f"--scale={scale}", "--color-space=bt709", "--log=error",
                         # запас на завантажену систему (iCloud/fileproviderd); на вигляд не впливає
                         "--timeout=120000"],
                        timeout=6 * 3600, cwd=REMOTION)
            if not r.ok or not tmp.exists():
                state.append({"chunk_id": f"{a}-{b}", "start_frame": a, "end_frame": b, "hash": h,
                              "status": "failed"})
                job["chunks"] = state
                raise StepError(f"рендер чанка {a}-{b}: {(r.stderr or r.stdout).strip()[-600:]}")
            got = count_packets(tmp)
            if got != b - a + 1:
                raise StepError(f"чанк {a}-{b}: {got} кадрів замість {b - a + 1}")
            tmp.replace(out)
            rendered += 1
        else:
            reused += 1
        files.append(out)
        state.append({"chunk_id": f"{a}-{b}", "start_frame": a, "end_frame": b, "hash": h,
                      "status": "rendered", "file": str(out.relative_to(ROOT))})
    job["chunks"] = state
    ctx.log(f"  {stage}: чанків {len(chunks)} (нових {rendered}, з кешу {reused})")

    listing = RENDERS / f"concat_{stage}.txt"
    listing.write_text("".join(f"file '{f}'\n" for f in files))
    video_only = RENDERS / f"{stage}_{version}_video.mp4"
    try:
        ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(video_only)])
        expected = round(tl["duration"] * fps)
        got = count_packets(video_only)
        if got != expected:
            raise StepError(f"після concat {got} кадрів, очікується {expected}")
        audio = normalized_audio(cfg)
        out = RENDERS / f"{stage}_{version}.mp4"
        ffmpeg(["-i", str(video_only), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "copy", "-t", f"{expected / fps:.3f}", "-movflags", "+faststart", str(out)])
    except MediaError as e:
        raise StepError(str(e)) from e
    video_only.unlink(missing_ok=True)
    ctx.log(f"  → {out.relative_to(ROOT)} ({probe(out).duration:.2f} с)")
    return out


def run_preview(job: dict[str, Any], ctx: Context) -> str:
    render(job, ctx, "preview")
    return "PREVIEW_RENDERED"


def run_final(job: dict[str, Any], ctx: Context) -> str:
    render(job, ctx, "final")
    return "FINAL_RENDERED"
