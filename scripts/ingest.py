"""Крок Ingest: медіа → SDR/CFR + master WAV; слайди → PNG + manifest."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from PIL import Image

from scripts.common import ROOT, WORK, run_cmd, validate, write_json
from scripts.content_mask import build_mask
from scripts.export_slides import ExportError, export_pdf, map_pages, rasterize, slide_visibility
from scripts.media import MediaError, ffmpeg, probe
from scripts.parse_pptx import build_manifest
from scripts.state import Context, StepError

MEDIA = WORK / "media"
SLIDES = WORK / "slides"


def _tonemap(src: Path, dst: Path, cfg: dict[str, Any], has_zscale: bool) -> str:
    mode = cfg["ingest"]["tonemapper"]
    use = "zscale" if (mode == "zscale" or (mode == "auto" and has_zscale)) else "avconvert"
    if use == "zscale":
        chain = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                 "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
        ffmpeg(["-i", str(src), "-map", "0:v:0", "-vf", chain, "-c:v", "libx264", "-crf", "14",
                "-preset", "fast", "-an", str(dst)])
    else:
        if not shutil.which("avconvert"):
            raise StepError("HDR-відео, але немає ні zscale, ні avconvert — tone mapping неможливий")
        r = run_cmd(["avconvert", "-s", str(src), "-p", "PresetHighestQuality", "-o", str(dst),
                     "--replace"], timeout=3600)
        if not dst.exists():
            raise StepError(f"avconvert не створив SDR-файл: {(r.stderr or r.stdout).strip()[-300:]}")
    after = probe(dst)
    if after.is_hdr:
        raise StepError(f"після {use} відео все ще HDR ({after.video.raw.get('color_transfer')})")
    return use


def _has_zscale() -> bool:
    r = run_cmd(["ffmpeg", "-hide_banner", "-filters"], timeout=30)
    return " zscale " in r.stdout


def ingest_media(media: Path, cfg: dict[str, Any], ctx: Context) -> dict[str, Any]:
    MEDIA.mkdir(parents=True, exist_ok=True)
    info = probe(media)
    if info.audio is None:
        raise StepError(f"{media.name}: немає аудіодоріжки")
    fps = cfg["project"]["fps"]
    report: dict[str, Any] = {"source": str(media.relative_to(ROOT)), "duration": info.duration,
                              "has_video": info.video is not None, "hdr": info.is_hdr,
                              "vfr": info.is_vfr, "source_fps": info.fps}

    # Відео ведучого → (tone mapping) → CFR без аудіо
    if info.video is not None:
        src = media
        if info.is_hdr:
            sdr = MEDIA / "presenter_sdr.mov"
            report["tonemapper"] = _tonemap(media, sdr, cfg, _has_zscale())
            ctx.log(f"  HDR → SDR через {report['tonemapper']}")
            src = sdr
        cfr = MEDIA / "presenter_cfr.mp4"
        ffmpeg(["-i", str(src), "-map", "0:v:0", "-vf", f"fps={fps},format=yuv420p",
                "-fps_mode", "cfr", "-c:v", "libx264", "-crf", "16", "-preset", "medium",
                "-movflags", "+faststart", "-an", str(cfr)])
        report["presenter"] = str(cfr.relative_to(ROOT))
        report["size"] = list(probe(cfr).size or (0, 0))

    # Master WAV з оригіналу; вирівнювання start_time відносно відео
    offset = 0.0
    if info.video is not None:
        offset = info.audio.start_time - info.video.start_time
    report["audio_offset"] = round(offset, 4)
    af = ["aresample=async=1"]
    if offset > 0.001:  # аудіо починається пізніше — доповнюємо тишею на початку
        af.append(f"adelay={int(offset * 1000)}:all=1")
    elif offset < -0.001:  # аудіо раніше — відрізаємо зайве
        af += [f"atrim=start={-offset}", "asetpts=PTS-STARTPTS"]
    denoise = cfg["ingest"].get("denoise", "none")
    if denoise == "afftdn":
        af.append("afftdn")
    master = MEDIA / "master.wav"
    ffmpeg(["-i", str(media), "-map", f"0:{info.audio.index}", "-af", ",".join(af),
            "-ar", str(cfg["ingest"]["master_audio_rate"]), "-ac", "1", "-c:a", "pcm_s24le", str(master)])
    stt = MEDIA / "stt.wav"
    ffmpeg(["-i", str(master), "-ar", str(cfg["ingest"]["stt_audio_rate"]), "-ac", "1",
            "-c:a", "pcm_s16le", str(stt)])
    report["master_audio"] = str(master.relative_to(ROOT))
    report["stt_audio"] = str(stt.relative_to(ROOT))
    report["master_duration"] = probe(master).duration

    if info.video is not None:
        face = _face_center(MEDIA / "presenter_cfr.mp4")
        report["face"] = face
        ctx.log("  обличчя: " + ("знайдено" if face.get("found") else "не знайдено — центральний кроп"))
    return report


def _face_center(video: Path) -> dict[str, Any]:
    r = run_cmd(["swift", str(ROOT / "scripts" / "face_center.swift"), str(video), "12"], timeout=300)
    for line in reversed(r.stdout.strip().splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return {"found": False, "error": (r.stderr or "no output").strip()[-200:]}


def ingest_slides(presentation: Path, job_id: str, cfg: dict[str, Any], ctx: Context) -> dict[str, Any]:
    SLIDES.mkdir(parents=True, exist_ok=True)
    width = cfg["slides"]["png_width"]
    flags: dict[str, Any] = {}
    if presentation.suffix.lower() == ".pptx":
        # унікальне ім'я: не сплутати з відкритими презентаціями користувача
        work_copy = SLIDES / f"{job_id}.pptx"
        shutil.copy2(presentation, work_copy)
        try:
            res = export_pdf(work_copy, SLIDES / f"{job_id}.pdf", cfg["slides"]["renderer"],
                             cfg["slides"].get("fallback"), cfg["slides"]["powerpoint_timeout_s"])
        except ExportError as e:
            raise StepError(f"експорт слайдів: {e}") from e
        if res.renderer != cfg["slides"]["renderer"]:
            flags["slides_renderer"] = res.renderer
            ctx.log(f"  ! слайди відрендерено через {res.renderer} (fallback)")
        visibility = slide_visibility(work_copy)
        try:
            pages = map_pages(visibility, res.pages)
        except ExportError as e:
            raise StepError(str(e)) from e
        pngs = rasterize(res.pdf, SLIDES, width)
        with Image.open(pngs[0]) as im:
            png_w, png_h = im.size
        manifest = build_manifest(work_copy, png_w, png_h)
        manifest["renderer"] = res.renderer
    else:
        pdf = SLIDES / f"{job_id}.pdf"
        shutil.copy2(presentation, pdf)
        pngs = rasterize(pdf, SLIDES, width)
        with Image.open(pngs[0]) as im:
            png_w, png_h = im.size
        pages = list(range(1, len(pngs) + 1))
        manifest = {"source": str(presentation), "renderer": "pdf", "slide_width_px": png_w,
                    "slide_height_px": png_h,
                    "slides": [{"slide": p, "hidden": False, "pdf_page": None, "image": None,
                                "content_mask": None, "title": None, "notes": None, "elements": [],
                                "builds": [], "advance_after_s": None} for p in pages]}

    manifest["source"] = str(Path(manifest["source"]).relative_to(ROOT)) if Path(manifest["source"]).is_absolute() else manifest["source"]
    for slide, page in zip(manifest["slides"], pages):
        slide["pdf_page"] = page
        if page is None:
            continue
        png = pngs[page - 1]
        mask = build_mask(png, SLIDES / "masks" / f"{png.stem}.png")
        slide["image"] = str(png.relative_to(ROOT))
        slide["content_mask"] = str(mask.path.relative_to(ROOT))
        slide["bg_color"] = mask.bg_color
        slide["dark_background"] = mask.dark_background
        slide["content_coverage"] = round(mask.coverage, 3)

    errors = validate("slide_manifest", manifest)
    if errors:
        raise StepError("slide_manifest не відповідає схемі: " + "; ".join(errors[:5]))
    write_json(SLIDES / "slide_manifest.json", manifest)
    visible = sum(1 for s in manifest["slides"] if not s["hidden"])
    ctx.log(f"  слайди: {len(manifest['slides'])} (видимих {visible}), рендер {manifest['renderer']}")
    return {"flags": flags, "visible": visible}


def run(job: dict[str, Any], ctx: Context) -> str:
    cfg = ctx.cfg
    if job.get("mode", "recording") == "recording":
        try:
            media_report = ingest_media(ROOT / job["inputs"]["media"]["path"], cfg, ctx)
        except MediaError as e:
            raise StepError(str(e)) from e
        write_json(MEDIA / "media.json", media_report)
    # режим «script»: аудіо створює крок synthesize
    slides = ingest_slides(ROOT / job["inputs"]["presentation"]["path"], job["job_id"], cfg, ctx)
    job.setdefault("flags", {}).update(slides["flags"])
    return "INGESTED"
