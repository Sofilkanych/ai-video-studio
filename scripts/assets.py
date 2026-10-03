"""Крок Asset Builder: файли для Remotion + props з геометрією сцен."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from scripts.common import ROOT, WORK, read_json, write_json
from scripts.layout import caption_band, scene_geometry
from scripts.state import Context, StepError

RENDERS = WORK / "renders"
PUBLIC = RENDERS / "public"  # --public-dir для Remotion
STYLE = {"background": "#13202f", "accent": "#f2a900", "font": "Helvetica Neue, Arial, sans-serif"}


def _link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == src.stat().st_size and dst.stat().st_mtime >= src.stat().st_mtime:
        return
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)  # без копіювання великих файлів
    except OSError:
        shutil.copy2(src, dst)


def timeline_path(version: str) -> Path:
    return WORK / "timeline" / f"timeline_{version}.json"


def props_path(version: str) -> Path:
    return RENDERS / f"props_{version}.json"


def build_props(job: dict[str, Any], tl: dict[str, Any]) -> dict[str, Any]:
    manifest = read_json(WORK / "slides" / "slide_manifest.json")
    media = read_json(WORK / "media" / "media.json")
    captions = read_json(WORK / "transcript" / "captions.json")
    prefix = f"job/{job['job_id']}"

    slides: dict[str, Any] = {}
    elements: dict[str, Any] = {}
    for s in manifest["slides"]:
        if s["hidden"] or not s.get("image"):
            continue
        src = ROOT / s["image"]
        if not src.exists():
            raise StepError(f"немає зображення слайда {s['slide']}: {s['image']}")
        _link(src, PUBLIC / prefix / "slides" / src.name)
        slides[str(s["slide"])] = {"src": f"{prefix}/slides/{src.name}", "w": manifest["slide_width_px"],
                                   "h": manifest["slide_height_px"], "bg": s.get("bg_color", "#ffffff")}
        by_id = {e["element_id"]: e for e in s["elements"]}
        for e in s["elements"]:
            if not e.get("bbox") or e["type"] == "table_cell" or e["type"] == "paragraph":
                continue
            info: dict[str, Any] = {"slide": s["slide"], "type": e["type"], "bbox": e["bbox"]}
            if e["type"] == "table":
                info["rows"] = [[by_id[f"{e['element_id']}/r{r}c0"]["bbox"][1], by_id[f"{e['element_id']}/r{r}c0"]["bbox"][3]]
                                for r in range(e["table"]["rows"]) if f"{e['element_id']}/r{r}c0" in by_id]
            elements[e["element_id"]] = info
        for e in s["elements"]:
            if e["type"] == "table_cell" and e.get("bbox"):
                elements[e["element_id"]] = {"slide": s["slide"], "type": "table_cell", "bbox": e["bbox"]}

    presenter = None
    if media.get("presenter"):
        src = ROOT / media["presenter"]
        _link(src, PUBLIC / prefix / src.name)
        face = media.get("face") or {}
        focus = ({"x": face["x"] + face["w"] / 2, "y": face["y"] + face["h"] / 2} if face.get("found")
                 else {"x": 0.5, "y": 0.35})
        from scripts.media import probe
        presenter = {"src": f"{prefix}/{src.name}", "focus": focus, "duration": probe(src).duration}

    aspect = manifest["slide_width_px"] / manifest["slide_height_px"]
    band = caption_band(tl)
    geometry = {s["scene_id"]: scene_geometry(s, tl["width"], tl["height"], aspect, presenter is not None,
                                              band).to_props()
                for s in tl["scenes"]}
    return {"timeline": tl, "geometry": geometry, "slides": slides, "elements": elements,
            "presenter": presenter, "captions": captions, "style": STYLE}


def write_props(job: dict[str, Any], version: str) -> Path:
    tl = read_json(timeline_path(version))
    props = build_props(job, tl)
    path = props_path(version)
    write_json(path, props)
    return path


def run(job: dict[str, Any], ctx: Context) -> str:
    version = job.get("timeline_version")
    if not version or not timeline_path(version).exists():
        raise StepError("немає timeline — спершу крок direct")
    path = write_props(job, version)
    props = json.loads(path.read_text(encoding="utf-8"))
    ctx.log(f"  props {path.name}: сцен {len(props['timeline']['scenes'])}, слайдів {len(props['slides'])}, "
            f"елементів {len(props['elements'])}, субтитрів {len(props['captions'])}")
    return "ASSETS_READY"
