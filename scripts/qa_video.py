"""Автоматичний QA: технічний (відносно очікувань timeline) + геометричний.

Семантичний QA (мультимодальна LLM) — Фаза 4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.assets import RENDERS, props_path
from scripts.common import ROOT, WORK, read_json, validate, write_json
from scripts.content_mask import load_mask, overlap_fraction
from scripts.layout import intersects, to_slide_norm
from scripts.media import count_packets, detect_intervals, loudness, probe
from scripts.state import Context, StepError

QA = WORK / "qa"
OVERLAP_MAX = 0.02  # частка пікселів вмісту під ведучим/субтитрами
SEVERE = {"high", "medium"}


def _issue(type_: str, check: str, severity: str, detail: str, scene: str | None = None,
           time: float | None = None, fix: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": type_, "check": check, "severity": severity, "detail": detail}
    if scene:
        d["scene_id"] = scene
    if time is not None:
        d["time"] = round(max(0.0, time), 2)
    if fix:
        d["fix"] = fix
    return d


def _covered(interval: tuple[float, float], others: list[tuple[float, float]], tol: float) -> bool:
    a, b = interval
    return any(o0 - tol <= a and b <= o1 + tol for o0, o1 in others)


def technical(video: Path, props: dict[str, Any], cfg: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    tl = props["timeline"]
    fps = tl["fps"]
    prof = cfg["profiles"]["preview" if stage == "preview" else "final"]
    issues = []
    info = probe(video)
    v, a = info.video, info.audio
    if v is None or a is None:
        return [_issue("missing_media", "technical", "high", "немає відео- або аудіопотоку")]
    w, h = info.size
    if (w, h) != (prof["width"], prof["height"]):
        issues.append(_issue("format", "technical", "high", f"роздільність {w}x{h}, очікується {prof['width']}x{prof['height']}"))
    if v.codec_name != "h264" or v.raw.get("pix_fmt") != "yuv420p":
        issues.append(_issue("format", "technical", "medium", f"кодек {v.codec_name}/{v.raw.get('pix_fmt')}"))
    if v.raw.get("color_transfer") not in (None, "bt709") or v.raw.get("color_space") not in (None, "bt709"):
        issues.append(_issue("format", "technical", "medium",
                             f"колірний простір {v.raw.get('color_space')}/{v.raw.get('color_transfer')}, очікується bt709"))
    if abs((info.fps or 0) - fps) > 0.01:
        issues.append(_issue("format", "technical", "high", f"fps {info.fps}, очікується {fps}"))
    expected = round(tl["duration"] * fps)
    got = count_packets(video)
    if got != expected:
        issues.append(_issue("frame_count_mismatch", "technical", "high", f"{got} кадрів, очікується {expected}"))
    vdur, adur = v.duration or info.duration, a.duration or info.duration
    if abs(vdur - expected / fps) > 1.5 / fps or abs(adur - vdur) > 0.1:
        issues.append(_issue("duration_mismatch", "technical", "high",
                             f"відео {vdur:.3f} с, аудіо {adur:.3f} с, timeline {expected / fps:.3f} с"))

    lu = loudness(video)
    target_i, target_tp = cfg["audio"]["loudness_lufs"], cfg["audio"]["true_peak_dbtp"]
    if abs(lu["i"] - target_i) > 1.5:
        issues.append(_issue("loudness", "technical", "medium", f"{lu['i']:.1f} LUFS, ціль {target_i}"))
    if lu["tp"] > target_tp + 0.2:  # невеликий запас на AAC
        issues.append(_issue("clipping", "technical", "medium", f"true peak {lu['tp']:.1f} dBTP, ціль ≤ {target_tp}"))

    # Тиша: помилка лише там, де в джерелі була мова. Порівнюємо з нормалізованим master,
    # щоб поріг -45 dB стосувався однакового рівня гучності.
    from scripts.render import normalized_audio
    master = normalized_audio(cfg)
    out_sil = detect_intervals(video, "silencedetect=noise=-45dB:d=1.0", "silencedetect")
    src_sil = detect_intervals(master, "silencedetect=noise=-45dB:d=0.8", "silencedetect")
    for s in out_sil:
        if not _covered((s[0], min(s[1], vdur)), src_sil, tol=0.35):
            issues.append(_issue("unexpected_silence", "technical", "high",
                                 f"тиша {s[0]:.2f}–{s[1]:.2f} с, якої немає в джерелі", time=s[0]))

    # Чорні кадри: у timeline немає «чорних» сцен; темні слайди — норма
    scenes = tl["scenes"]
    slides_dark = {int(k) for k, s in props["slides"].items() if _is_dark(s["bg"])}
    for b0, b1 in detect_intervals(video, "blackdetect=d=0.5:pix_th=0.08", "blackdetect",
                                   stream="v", start_key="black_start", end_key="black_end"):
        mid = (b0 + min(b1, vdur)) / 2
        sc = next((s for s in scenes if s["start"] <= mid < s["end"]), None)
        if sc and sc.get("slide") in slides_dark:
            continue
        issues.append(_issue("black_frames", "technical", "high", f"чорні кадри {b0:.2f}–{b1:.2f} с",
                             scene=sc and sc["scene_id"], time=b0))

    # Застиглий ведучий: freezedetect лише в області ведучого і лише де він видимий
    by_rect: dict[tuple, list[dict[str, Any]]] = {}
    for s in scenes:
        g = props["geometry"][s["scene_id"]]
        if g["presenter"] and props.get("presenter") and s["end"] - s["start"] > 4:
            by_rect.setdefault(tuple(round(x) for x in g["presenter"]), []).append(s)
    sx = prof["width"] / tl["width"]
    pres_end = props["presenter"]["duration"] if props.get("presenter") else 0
    for rect, group in by_rect.items():
        x, y, rw, rh = (round(c * sx) for c in rect)
        crop = f"crop={rw - rw % 2}:{rh - rh % 2}:{x}:{y},freezedetect=n=0.002:d=3"
        frozen = detect_intervals(video, crop, "freezedetect", stream="v",
                                  start_key="freeze_start", end_key="freeze_end")
        for f0, f1 in frozen:
            if f0 >= pres_end - 0.5:
                continue  # відео ведучого закінчилось — очікувано
            for s in group:
                if f0 < s["end"] and min(f1, vdur) > s["start"]:
                    issues.append(_issue("frozen_presenter", "technical", "medium",
                                         f"ведучий застиг {f0:.2f}–{min(f1, vdur):.2f} с", scene=s["scene_id"], time=f0))
    return issues


def _is_dark(hex_color: str) -> bool:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.35


def geometric(props: dict[str, Any]) -> list[dict[str, Any]]:
    """Ведучий і субтитри не перекривають вміст слайда (маска з пікселів PNG)."""
    tl = props["timeline"]
    manifest = read_json(WORK / "slides" / "slide_manifest.json")
    masks = {s["slide"]: ROOT / s["content_mask"] for s in manifest["slides"] if s.get("content_mask")}
    cache: dict[int, Any] = {}
    issues = []
    for s in tl["scenes"]:
        g = props["geometry"][s["scene_id"]]
        slide_no = s.get("slide")
        if not g["slide"] or slide_no not in masks:
            continue
        mask = cache.setdefault(slide_no, load_mask(masks[slide_no]))
        slide_rect = tuple(g["slide"])
        if g["presenter"] and s["layout"] != "presenter_full" and intersects(tuple(g["presenter"]), slide_rect):
            frac = overlap_fraction(mask, to_slide_norm(tuple(g["presenter"]), slide_rect))
            if frac > OVERLAP_MAX:
                issues.append(_issue("overlap", "geometric", "high",
                                     f"ведучий перекриває {frac:.0%} вмісту слайда {slide_no}",
                                     scene=s["scene_id"], time=s["start"], fix="move presenter"))
        burn_in = (tl.get("captions") or {}).get("enabled", True)
        has_caps = burn_in and any(c["start"] < s["end"] and c["end"] > s["start"] for c in props["captions"])
        if has_caps and intersects(tuple(g["caption"]), slide_rect):
            frac = overlap_fraction(mask, to_slide_norm(tuple(g["caption"]), slide_rect))
            if frac > OVERLAP_MAX:
                issues.append(_issue("caption_safe_area", "geometric", "medium",
                                     f"субтитри перекривають {frac:.0%} вмісту слайда {slide_no}",
                                     scene=s["scene_id"], time=s["start"], fix="slide_inset"))
        for a in s.get("actions", []):
            if a["type"] == "callout" and len(a.get("text", "")) > 60:
                issues.append(_issue("callout_out_of_frame", "geometric", "low",
                                     "текст виноски довший за 60 символів", scene=s["scene_id"], time=a["at"]))
    return issues


def run_qa(job: dict[str, Any], ctx: Context, stage: str) -> str:
    version = job["timeline_version"]
    video = RENDERS / f"{stage}_{version}.mp4"
    if not video.exists():
        raise StepError(f"немає {video.name} для QA")
    props = read_json(props_path(version))
    issues = geometric(props) + technical(video, props, ctx.cfg, stage)
    status = "fail" if any(i["severity"] in SEVERE for i in issues) else "pass"
    report = {"status": status, "timeline_version": version, "stage": stage, "issues": issues}
    errors = validate("qa_report", report)
    if errors:
        raise StepError("qa_report: " + "; ".join(errors[:5]))
    write_json(QA / f"qa_{stage}_{version}.json", report)
    ctx.log(f"  QA {stage} {version}: {status}, проблем {len(issues)}"
            + "".join(f"\n    [{i['severity']}] {i['type']}: {i['detail']}" for i in issues))
    if stage == "preview":
        return "QA_PASSED" if status == "pass" else "QA_FAILED"
    return "FINAL_QA_PASSED" if status == "pass" else "QA_FAILED"


def run_preview(job: dict[str, Any], ctx: Context) -> str:
    return run_qa(job, ctx, "preview")


def run_final(job: dict[str, Any], ctx: Context) -> str:
    return run_qa(job, ctx, "final")
