"""Крок AI Director: production_plan.json + timeline.json.

Провайдери: anthropic_api (Claude API, structured output, зображення слайдів, prompt caching),
claude_cli (`claude -p --json-schema`, лише текст; тільки для тестових фікстур за data_policy).
Кожна відповідь валідується схемою і семантичними перевірками; при помилці — повтор з текстом
помилки, далі — детермінована проста режисура.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
from pathlib import Path
from typing import Any

from PIL import Image

from scripts.common import ROOT, WORK, run_cmd, validate, write_json
from scripts.state import Context, StepError

TIMELINE = WORK / "timeline"
LOGS = WORK / "logs"
MVP_LAYOUTS = ["slide_inset", "slide_full", "presenter_full", "side_by_side"]
MVP_ACTIONS = ["zoom", "highlight", "callout", "table_reveal"]
TARGET_TYPES = {"title", "text", "table", "table_cell", "chart", "picture", "shape"}
LOW_CONFIDENCE = 0.6  # config sync.low_confidence

SYSTEM_PROMPT = """You are the AI director of an automated video studio. You turn a recorded talk
(transcript with timestamps) and its slide deck into a machine-executable edit plan for an
educational / corporate video. The speaker's language is Ukrainian; write all human-readable
text (titles, key points, callout text) in Ukrainian.

DIRECTING POLICY
1. Never ask questions; decide.
2. Slide change times are given; never invent them. A scene must lie inside one slide interval
   and show that interval's slide. You may split an interval into several scenes.
3. Action targets must be element_ids from the slide list that have a bbox (shapes, tables, table
   cells, charts, text, titles, pictures). Never invent ids. Only target elements of the scene's slide.
4. Prefer the presentation's own content; keep it calm and professional.
5. Presenter must not cover slide content. "slide_inset" (slide shrunk, presenter in the free strip)
   is the safe default when a presenter video exists. "slide_full" with the presenter bubble is only
   for slides with an empty corner. Use "presenter_full" sparingly (intro/outro, under 10 s,
   when nothing on the slide matters). Without presenter video, presenter must be null and the layout
   "slide_inset" (slide slightly smaller, captions below it — safe default) or "slide_full" (only when
   the bottom 15% of the slide is empty, because captions are drawn there).
6. Actions: "highlight" an element when the speaker talks about it; "zoom" into a dense element
   (table/chart) when details are discussed; "table_reveal" on a table element to reveal rows
   progressively while the speaker walks through them; "callout" adds a short Ukrainian note
   (max 8 words) next to the target. At most 3 actions per scene, at least 2 seconds apart.
   Time them to the words that mention the target.
7. If unsure, do less. A scene with no actions is fine.
8. Low sync confidence (< 0.6) on a slide interval → no word-precise actions there.
9. transition_in: "fade" between different slides, "cut" within the same slide.
"""

# Схема для structured output (без pattern/numeric constraints — повна перевірка після)
_ACTION = {
    "type": "object",
    "properties": {
        "at": {"type": "number"},
        "until": {"type": "number"},
        "type": {"type": "string", "enum": MVP_ACTIONS},
        "target": {"type": "string"},
        "text": {"type": "string"},
    },
    "required": ["at", "until", "type", "target", "text"],
    "additionalProperties": False,
}
_SCENE = {
    "type": "object",
    "properties": {
        "start": {"type": "number"},
        "end": {"type": "number"},
        "slide": {"type": "integer"},
        "layout": {"type": "string", "enum": MVP_LAYOUTS},
        "presenter": {"anyOf": [{
            "type": "object",
            "properties": {
                "shape": {"type": "string", "enum": ["circle", "rect"]},
                "position": {"type": "string", "enum": ["bottom-right", "bottom-left", "top-right", "top-left"]},
                "scale": {"type": "number"},
            },
            "required": ["shape", "position", "scale"],
            "additionalProperties": False,
        }, {"type": "null"}]},
        "transition_in": {"type": "string", "enum": ["cut", "fade"]},
        "actions": {"type": "array", "items": _ACTION},
    },
    "required": ["start", "end", "slide", "layout", "presenter", "transition_in", "actions"],
    "additionalProperties": False,
}
DIRECTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "chapters": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "number"},
                "end": {"type": "number"},
                "slides": {"type": "array", "items": {"type": "integer"}},
                "key_points": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "start", "end", "slides", "key_points"],
            "additionalProperties": False,
        }},
        "scenes": {"type": "array", "items": _SCENE},
    },
    "required": ["title", "chapters", "scenes"],
    "additionalProperties": False,
}


# ---------- вхідні дані ----------

def load_inputs() -> dict[str, Any]:
    def rd(p: str) -> Any:
        return json.loads((WORK / p).read_text(encoding="utf-8"))
    return {"manifest": rd("slides/slide_manifest.json"), "transcript": rd("transcript/transcript.json"),
            "timings": rd("sync/slide_timings.json"), "media": rd("media/media.json")}


def intervals(timings: dict[str, Any]) -> list[dict[str, Any]]:
    tr = timings["transitions"]
    out = []
    for i, t in enumerate(tr):
        end = tr[i + 1]["start"] if i + 1 < len(tr) else timings["duration"]
        out.append({"slide": t["slide"], "start": t["start"], "end": end, "confidence": t["confidence"]})
    return out


def _targetable(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for s in manifest["slides"]:
        for e in s["elements"]:
            if e.get("bbox") and e["type"] in TARGET_TYPES:
                out[e["element_id"]] = {"slide": s["slide"], "type": e["type"]}
    return out


def build_prompt_text(inp: dict[str, Any]) -> str:
    m = inp["manifest"]
    pw, ph = m["slide_width_px"], m["slide_height_px"]
    lines = ["SLIDES (bbox normalized x,y,w,h in 0..1):"]
    for s in m["slides"]:
        if s["hidden"]:
            continue
        lines.append(f"\n## Slide {s['slide']}: {s.get('title') or ''}")
        if s.get("notes"):
            lines.append(f"speaker notes: {s['notes']}")
        if s.get("content_coverage") is not None:
            lines.append(f"content coverage: {s['content_coverage']:.0%}")
        for e in s["elements"]:
            if not e.get("bbox") or e["type"] not in TARGET_TYPES:
                continue
            x, y, w, h = e["bbox"]
            text = (e.get("text") or "").replace("\n", " / ")[:80]
            extra = ""
            if e.get("table"):
                extra = f" table {e['table']['rows']}x{e['table']['cols']}"
            if e.get("chart"):
                extra = f" chart {e['chart'].get('categories')} {[s_['values'] for s_ in e['chart'].get('series', [])]}"
            lines.append(f"- {e['element_id']} [{e['type']}] bbox=({x/pw:.2f},{y/ph:.2f},{w/pw:.2f},{h/ph:.2f}){extra} {text}")
    lines.append("\nSLIDE INTERVALS (fixed):")
    for iv in intervals(inp["timings"]):
        lines.append(f"- slide {iv['slide']}: {iv['start']:.2f}–{iv['end']:.2f} s (sync confidence {iv['confidence']})")
    lines.append(f"\nPRESENTER VIDEO: {'yes' if inp['media'].get('presenter') else 'no (voice only)'}")
    lines.append(f"TOTAL DURATION: {inp['timings']['duration']:.2f} s")
    lines.append("\nTRANSCRIPT (sentence start–end s: text):")
    for seg in inp["transcript"]["segments"]:
        lines.append(f"{seg['start']:.2f}–{seg['end']:.2f}: {seg['text']}")
    lines.append("\nReturn the plan: chapters and scenes covering 0..duration without gaps or overlaps.")
    return "\n".join(lines)


def _slide_images(manifest: dict[str, Any], width: int) -> list[dict[str, Any]]:
    blocks = []
    for s in manifest["slides"]:
        if s["hidden"] or not s.get("image"):
            continue
        with Image.open(ROOT / s["image"]) as im:
            im = im.convert("RGB")
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
        blocks.append({"type": "text", "text": f"Slide {s['slide']}:"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                   "data": base64.standard_b64encode(buf.getvalue()).decode()}})
    if blocks:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


# ---------- перевірка ----------

def assemble_timeline(plan: dict[str, Any], cfg: dict[str, Any], duration: float, version: str) -> dict[str, Any]:
    fps = cfg["project"]["fps"]
    prof = cfg["profiles"]["final"]

    def snap(t: float) -> float:  # межі сцен — рівно на кадрах
        return round(round(t * fps) / fps, 4)

    ordered = sorted(plan["scenes"], key=lambda s: s["start"])
    # дрібні розриви/накладання (< 0.1 с) між сусідніми сценами закриваємо: start[i+1] = end[i]
    for a, b in zip(ordered, ordered[1:]):
        if abs(b["start"] - a["end"]) < 0.1:
            b["start"] = a["end"]
    if ordered and abs(ordered[-1]["end"] - duration) < 0.5:
        ordered[-1]["end"] = duration
    scenes = []
    for i, s in enumerate(ordered, start=1):
        scene = {"scene_id": f"s{i}", "start": snap(s["start"]), "end": snap(s["end"]),
                 "slide": s["slide"], "layout": s["layout"], "presenter": s["presenter"],
                 "transition_in": s.get("transition_in", "cut"), "actions": [], "generated_assets": []}
        for a in s.get("actions", []):
            act = {"at": round(a["at"], 3), "until": round(a["until"], 3), "type": a["type"], "target": a["target"]}
            if a.get("text"):
                act["text"] = a["text"]
            scene["actions"].append(act)
        scenes.append(scene)
    return {"version": version, "fps": fps, "width": prof["width"], "height": prof["height"],
            "duration": snap(duration), "captions": {"enabled": bool(cfg.get("captions", {}).get("burn_in", True)),
                         "position": "bottom", "scale": 1.0},
            "scenes": scenes}


def check_timeline(tl: dict[str, Any], inp: dict[str, Any], low_confidence: float = LOW_CONFIDENCE) -> list[str]:
    errors = validate("timeline", tl)
    if errors:
        return errors
    eps = 0.05
    ivs = intervals(inp["timings"])
    targets = _targetable(inp["manifest"])
    has_presenter = bool(inp["media"].get("presenter"))
    scenes = tl["scenes"]
    if abs(scenes[0]["start"]) > eps:
        errors.append(f"перша сцена має починатися з 0, а не {scenes[0]['start']}")
    if abs(scenes[-1]["end"] - tl["duration"]) > 0.5:
        errors.append(f"остання сцена закінчується {scenes[-1]['end']}, тривалість {tl['duration']}")
    for a, b in zip(scenes, scenes[1:]):
        if abs(a["end"] - b["start"]) > eps:
            errors.append(f"{a['scene_id']}→{b['scene_id']}: розрив/накладання {a['end']}→{b['start']}")
    for s in scenes:
        sid = s["scene_id"]
        if s["end"] - s["start"] < 1.0:
            errors.append(f"{sid}: сцена коротша за 1 с")
        mid = (s["start"] + s["end"]) / 2
        iv = next((iv for iv in ivs if iv["start"] - eps <= mid < iv["end"] + eps), None)
        if iv is None or iv["slide"] != s["slide"]:
            errors.append(f"{sid}: слайд {s['slide']} не відповідає інтервалу ({iv and iv['slide']})")
        elif s["start"] < iv["start"] - eps or s["end"] > iv["end"] + eps:
            errors.append(f"{sid}: сцена перетинає межу слайдового інтервалу {iv['start']}–{iv['end']}")
        if not has_presenter and (s["presenter"] or s["layout"] in ("presenter_full", "side_by_side")):
            errors.append(f"{sid}: немає відео ведучого — presenter має бути null, layout slide_inset або slide_full")
        if has_presenter and s["layout"] == "slide_inset" and not s["presenter"]:
            errors.append(f"{sid}: slide_inset потребує presenter")
        if s["layout"] == "presenter_full" and s["end"] - s["start"] > 10.0 + eps:
            errors.append(f"{sid}: presenter_full довше 10 с")
        if iv is not None and iv["confidence"] < low_confidence and any(
                a["type"] in ("highlight", "callout") for a in s["actions"]):
            errors.append(f"{sid}: низька впевненість синхронізації ({iv['confidence']}) — без highlight/callout")
        if s["presenter"] and not 0.12 <= s["presenter"]["scale"] <= 0.35:
            errors.append(f"{sid}: presenter.scale має бути 0.12–0.35")
        last = None
        for a in s["actions"]:
            t = targets.get(a["target"])
            if not t:
                errors.append(f"{sid}: ціль {a['target']} не існує або не має bbox")
            elif t["slide"] != s["slide"]:
                errors.append(f"{sid}: ціль {a['target']} з іншого слайда")
            elif a["type"] == "table_reveal" and t["type"] != "table":
                errors.append(f"{sid}: table_reveal лише для елемента-таблиці")
            if not (s["start"] <= a["at"] < a.get("until", a["at"] + 1) <= s["end"] + eps):
                errors.append(f"{sid}: дія {a['type']} поза сценою або until ≤ at")
            if last is not None and a["at"] - last < 2 - eps:
                errors.append(f"{sid}: дії ближче 2 с одна до одної")
            last = a["at"]
        if len(s["actions"]) > 3:
            errors.append(f"{sid}: більше 3 дій")
    return errors


# ---------- провайдери ----------

_CURRENT_JOB: dict[str, str] = {}


def _log_call(entry: dict[str, Any]) -> None:
    entry = {"job_id": _CURRENT_JOB.get("id"), "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / "llm_calls.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def call_anthropic(messages: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
    import anthropic

    client = anthropic.Anthropic()
    t0 = time.monotonic()
    response = client.beta.messages.create(
        model=cfg["director"]["model"],
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": DIRECTOR_SCHEMA}},
        messages=messages,
    )
    u = response.usage
    _log_call({"provider": "anthropic_api", "model": response.model, "stop_reason": response.stop_reason,
               "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
               "cache_read": getattr(u, "cache_read_input_tokens", None),
               "seconds": round(time.monotonic() - t0, 1)})
    if response.stop_reason == "refusal":
        raise StepError("Claude відмовився обробити запит (refusal)")
    if response.stop_reason == "max_tokens":
        raise StepError("відповідь обрізано (max_tokens)")
    return next(b.text for b in response.content if b.type == "text")


def call_claude_cli(prompt: str, cfg: dict[str, Any]) -> str:
    t0 = time.monotonic()
    r = run_cmd(["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(DIRECTOR_SCHEMA),
                 "--tools", "", "--no-session-persistence", "--model", cfg["director"].get("cli_model", "opus"),
                 "--system-prompt", SYSTEM_PROMPT, prompt], timeout=1800)
    if not r.ok:
        _log_call({"provider": "claude_cli", "error": (r.stderr or r.stdout).strip()[-300:],
                   "seconds": round(time.monotonic() - t0, 1)})
        raise StepError(f"claude CLI: {(r.stderr or r.stdout).strip()[-300:]}")
    data = json.loads(r.stdout)
    _log_call({"provider": "claude_cli", "is_error": data.get("is_error"),
               "permission_denials": data.get("permission_denials"), "usage": data.get("usage"),
               "cost_usd": data.get("total_cost_usd"), "seconds": round(time.monotonic() - t0, 1)})
    if data.get("is_error") or data.get("permission_denials"):
        raise StepError(f"claude CLI помилка: {data.get('result')}")
    out = data.get("structured_output")
    if out is None:
        raise StepError("claude CLI не повернув structured_output")
    return json.dumps(out, ensure_ascii=False)


def _api_key_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def choose_provider(cfg: dict[str, Any]) -> str | None:
    prov = cfg["director"]["provider"]
    dp = cfg["data_policy"]
    if prov == "anthropic_api" and dp.get("allow_claude_api") and _api_key_available():
        return "anthropic_api"
    if prov == "claude_cli" and dp.get("allow_consumer_subscription"):
        return "claude_cli"
    return None


# ---------- проста режисура (fallback) ----------

def fallback_plan(inp: dict[str, Any]) -> dict[str, Any]:
    has_presenter = bool(inp["media"].get("presenter"))
    by_slide = {s["slide"]: s for s in inp["manifest"]["slides"]}
    scenes, chapters, prev = [], [], None
    for iv in intervals(inp["timings"]):
        slide = by_slide[iv["slide"]]
        actions = []
        table = next((e for e in slide["elements"] if e["type"] == "table" and e.get("bbox")), None)
        dur = iv["end"] - iv["start"]
        if table and dur > 6 and iv["confidence"] >= 0.6:
            rows = table["table"]["rows"]
            at = iv["start"] + 1.0
            actions.append({"at": at, "until": min(at + max(3.0, rows * 1.5), iv["end"] - 0.5),
                            "type": "table_reveal", "target": table["element_id"], "text": ""})
        scenes.append({
            "start": iv["start"], "end": iv["end"], "slide": iv["slide"],
            # без ведучого — теж slide_inset: субтитри під слайдом, не на вмісті
            "layout": "slide_inset",
            "presenter": {"shape": "circle", "position": "bottom-right", "scale": 0.22} if has_presenter else None,
            "transition_in": "cut" if prev is None or prev == iv["slide"] else "fade",
            "actions": actions,
        })
        chapters.append({"title": slide.get("title") or f"Слайд {iv['slide']}", "start": iv["start"],
                         "end": iv["end"], "slides": [iv["slide"]], "key_points": []})
        prev = iv["slide"]
    title = next((s.get("title") for s in inp["manifest"]["slides"] if s.get("title")), "Відео")
    return {"title": title, "chapters": chapters, "scenes": scenes}


def to_production_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {"title": plan["title"], "pacing": "calm",
            "chapters": [{"chapter_id": f"c{i}", "title": c["title"], "start": round(c["start"], 3),
                          "end": round(c["end"], 3), "slides": c["slides"], "key_points": c["key_points"]}
                         for i, c in enumerate(plan["chapters"], start=1)],
            "external_assets": []}


# ---------- крок ----------

def direct(inp: dict[str, Any], cfg: dict[str, Any], ctx: Context, version: str = "v001") -> tuple[dict, dict, str]:
    duration = inp["timings"]["duration"]
    provider = choose_provider(cfg)
    if provider:
        prompt = build_prompt_text(inp)
        content: list[dict[str, Any]] = []
        if provider == "anthropic_api":
            content = _slide_images(inp["manifest"], cfg["director"]["slide_image_width"])
        messages = [{"role": "user", "content": [*content, {"type": "text", "text": prompt}]}]
        for attempt in range(1, cfg["director"]["max_retries"] + 1):
            try:
                raw = call_anthropic(messages, cfg) if provider == "anthropic_api" else call_claude_cli(
                    prompt if attempt == 1 else messages[-1]["content"][-1]["text"], cfg)
                plan = json.loads(raw)
                tl = assemble_timeline(plan, cfg, duration, version)
                errors = check_timeline(tl, inp, cfg["sync"]["low_confidence"])
            except (StepError, json.JSONDecodeError, KeyError) as e:
                ctx.log(f"  Director ({provider}) спроба {attempt}: {e}")
                break
            except Exception as e:  # anthropic.APIError (429/529/мережа) тощо — SDK вже робив retries
                if type(e).__module__.split(".")[0] != "anthropic":
                    raise
                _log_call({"provider": provider, "error": f"{type(e).__name__}: {e}"[:300]})
                ctx.log(f"  Director ({provider}): помилка API {type(e).__name__} — проста режисура")
                break
            if not errors:
                ctx.log(f"  Director: {provider}, спроба {attempt}, сцен {len(tl['scenes'])}")
                return to_production_plan(plan), tl, provider
            ctx.log(f"  Director спроба {attempt}: {len(errors)} помилок — повтор")
            feedback = ("Your plan failed validation. Fix ALL of these and return the full corrected plan:\n- "
                        + "\n- ".join(errors[:30]))
            if provider == "anthropic_api":
                messages += [{"role": "assistant", "content": raw},
                             {"role": "user", "content": [{"type": "text", "text": feedback}]}]
            else:
                messages = [{"role": "user", "content": [{"type": "text", "text":
                             prompt + "\n\nPREVIOUS ATTEMPT:\n" + raw + "\n\n" + feedback}]}]
    else:
        ctx.log("  Director: Claude недоступний (немає ключа API або заборонено data_policy) — проста режисура")
    plan = fallback_plan(inp)
    tl = assemble_timeline(plan, cfg, duration, version)
    errors = check_timeline(tl, inp, cfg["sync"]["low_confidence"])
    if errors:
        raise StepError("навіть проста режисура не пройшла перевірку: " + "; ".join(errors[:5]))
    return to_production_plan(plan), tl, "fallback"


def save_timeline(tl: dict[str, Any]) -> Path:
    path = TIMELINE / f"timeline_{tl['version']}.json"
    write_json(path, tl)
    return path


def run(job: dict[str, Any], ctx: Context) -> str:
    _CURRENT_JOB["id"] = job["job_id"]
    inp = load_inputs()
    plan, tl, provider = direct(inp, ctx.cfg, ctx)
    errors = validate("production_plan", plan)
    if errors:
        raise StepError("production_plan: " + "; ".join(errors[:5]))
    TIMELINE.mkdir(parents=True, exist_ok=True)
    write_json(TIMELINE / "production_plan.json", plan)
    save_timeline(tl)
    job["timeline_version"] = tl["version"]
    job.setdefault("flags", {})["director"] = provider
    return "DIRECTED"
