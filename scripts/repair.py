"""Детермінований repair геометричних помилок + safe fallback + finalize.

overlap → інший кут → менший масштаб → slide_inset → без ведучого;
субтитри на вмісті → slide_inset (субтитри під слайдом).
Кожна правка створює нову версію timeline; рендер перевикористовує незмінні чанки.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from scripts.assets import RENDERS, build_props, timeline_path, write_props
from scripts.common import OUTPUT, ROOT, WORK, read_json, write_json
from scripts.content_mask import load_mask, overlap_fraction
from scripts.direct import assemble_timeline, check_timeline, fallback_plan, load_inputs, save_timeline
from scripts.layout import caption_band, intersects, scene_geometry, to_slide_norm
from scripts.qa_video import OVERLAP_MAX, QA
from scripts.state import Context, StepError

CORNERS = ["bottom-right", "top-right", "bottom-left", "top-left"]


def next_version(v: str) -> str:
    return f"v{int(v[1:]) + 1:03d}"


def _scene_ok(scene: dict[str, Any], props: dict[str, Any], mask, aspect: float, has_presenter: bool) -> bool:
    tl = props["timeline"]
    g = scene_geometry(scene, tl["width"], tl["height"], aspect, has_presenter, caption_band(tl))
    if not g.slide:
        return True
    rects = []
    if g.presenter and scene["layout"] != "presenter_full":
        rects.append(g.presenter)
    rects.append(g.caption)
    return all(not intersects(r, g.slide) or overlap_fraction(mask, to_slide_norm(r, g.slide)) <= OVERLAP_MAX
               for r in rects)


def fix_scene(scene: dict[str, Any], props: dict[str, Any], mask, aspect: float, has_presenter: bool) -> str:
    """Змінює сцену на місці; повертає опис правки."""
    candidates = []
    pres = scene.get("presenter")
    if pres and scene["layout"] == "slide_full":
        for corner in CORNERS:
            for scale in (pres["scale"], 0.18, 0.15):
                candidates.append(({**scene, "presenter": {**pres, "position": corner, "scale": scale}},
                                   f"ведучий → {corner}, scale {scale}"))
    if has_presenter:
        candidates.append(({**scene, "layout": "slide_inset",
                            "presenter": {"shape": "circle", "position": "bottom-right", "scale": 0.22}},
                           "layout → slide_inset"))
    candidates.append(({**scene, "layout": "slide_inset", "presenter": None} if not has_presenter else
                       {**scene, "layout": "slide_full", "presenter": None}, "ведучого прибрано"))
    for cand, note in candidates:
        if _scene_ok(cand, props, mask, aspect, has_presenter):
            scene.clear()
            scene.update(cand)
            return note
    raise StepError(f"{scene['scene_id']}: не вдалося прибрати перекриття")


def run(job: dict[str, Any], ctx: Context) -> str:
    version = job["timeline_version"]
    qa_files = sorted(QA.glob(f"qa_*_{version}.json"), key=lambda p: p.stat().st_mtime)
    if not qa_files:
        raise StepError("немає QA-звіту для repair")
    report = read_json(qa_files[-1])
    geo_scenes = {i["scene_id"] for i in report["issues"] if i["check"] == "geometric" and i.get("scene_id")
                  and i["severity"] in ("high", "medium")}
    technical = [i for i in report["issues"] if i["check"] == "technical" and i["severity"] in ("high", "medium")]
    if not geo_scenes:
        raise StepError("QA знайшов лише технічні проблеми — автоматично не виправляються: "
                        + "; ".join(f"{i['type']}: {i['detail']}" for i in technical[:5]))

    tl = copy.deepcopy(read_json(timeline_path(version)))
    props = build_props(job, tl)
    manifest = read_json(WORK / "slides" / "slide_manifest.json")
    aspect = manifest["slide_width_px"] / manifest["slide_height_px"]
    masks = {s["slide"]: ROOT / s["content_mask"] for s in manifest["slides"] if s.get("content_mask")}
    has_presenter = props["presenter"] is not None
    notes = []
    for scene in tl["scenes"]:
        if scene["scene_id"] in geo_scenes:
            note = fix_scene(scene, props, load_mask(masks[scene["slide"]]), aspect, has_presenter)
            notes.append(f"{scene['scene_id']}: {note}")
    tl["version"] = next_version(version)
    errors = check_timeline(tl, load_inputs(), ctx.cfg["sync"]["low_confidence"])
    if errors:
        raise StepError("timeline після repair невалідний: " + "; ".join(errors[:5]))
    save_timeline(tl)
    job["timeline_version"] = tl["version"]
    write_props(job, tl["version"])
    ctx.log(f"  repair → {tl['version']}: " + "; ".join(notes))
    return "PATCHED"


def safe_fallback(job: dict[str, Any], ctx: Context) -> str:
    inp = load_inputs()
    tl = assemble_timeline(fallback_plan(inp), ctx.cfg, inp["timings"]["duration"],
                           next_version(job["timeline_version"]))
    errors = check_timeline(tl, inp, ctx.cfg["sync"]["low_confidence"])
    if errors:
        raise StepError("safe fallback невалідний: " + "; ".join(errors[:5]))
    save_timeline(tl)
    job["timeline_version"] = tl["version"]
    write_props(job, tl["version"])
    ctx.log(f"  safe fallback → {tl['version']} (проста режисура)")
    return "PATCHED"


def finalize(job: dict[str, Any], ctx: Context) -> str:
    from scripts.transcribe import write_srt

    version = job["timeline_version"]
    final = RENDERS / f"final_{version}.mp4"
    if not final.exists():
        raise StepError(f"немає {final.name}")
    OUTPUT.mkdir(exist_ok=True)
    shutil.copy2(final, OUTPUT / "final.mp4")
    captions = read_json(WORK / "transcript" / "captions.json")
    write_srt(captions, OUTPUT / "final.srt")
    calls = []
    log = WORK / "logs" / "llm_calls.jsonl"
    if log.exists():
        calls = [c for c in (json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip())
                 if c.get("job_id") == job["job_id"]]
    report = {
        "job_id": job["job_id"], "timeline_version": version, "flags": job.get("flags", {}),
        "repair_loops": job.get("repair_loops", 0),
        "qa_final": read_json(QA / f"qa_final_{version}.json"),
        "transcript": {k: v for k, v in read_json(WORK / "transcript" / "transcript.json").items()
                       if k in ("glossary_replacements", "dropped")},
        "slide_timings": read_json(WORK / "sync" / "slide_timings.json"),
        "llm_calls": calls,
    }
    write_json(OUTPUT / "report.json", report)
    ctx.log(f"  output/final.mp4, output/final.srt, output/report.json ({version})")
    return "DONE"
