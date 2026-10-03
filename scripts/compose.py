"""Крок Compose (режим «текст → презентація»): текст лекції → план слайдів з озвучкою → PPTX.

Claude (API, structured output) розбиває матеріал на слайди: короткий текст на екран і розмовну
озвучку для нотаток доповідача. План перевіряється (довжини, обов'язкові поля, ліміти озвучки);
при помилках — повтор з їх переліком. Результат кешується за хешем тексту й налаштувань:
повторний прогін не викликає Claude, а правки в work/compose/deck_plan.json + `--redo ingest`
перезбирають презентацію без нового запиту.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any

from scripts.common import ROOT, WORK, read_json, write_json
from scripts.deck_render import Deck, render_deck
from scripts.state import Context, StepError

COMPOSE = WORK / "compose"
LAYOUTS = list(Deck.LAYOUTS)

SYSTEM_PROMPT = """You are an instructional designer who turns lecture material into a narrated slide
video. You produce a slide plan: for every slide, short on-screen text and the full spoken narration
that will be voiced by a text-to-speech clone of the author's voice.

LANGUAGE: write everything in the language of the source text (usually Ukrainian).

NARRATION (the most important part)
- Cover the source faithfully and fully: keep its facts, examples, numbers, names and order.
  Do not invent facts, statistics, quotes or sources. You may lightly rephrase for speech.
- Spoken style: short clear sentences, natural transitions between slides, addressing the viewer.
- Write numbers, dates, percentages and amounts IN WORDS with the correct grammatical case
  (e.g. «друге жовтня дві тисячі двадцять шостого року», «шістдесят відсотків»), because a
  synthesizer reads digits badly. Keep product/company names and Latin abbreviations as written.
- Each slide: 60–200 words of narration. Do not start every slide with «На цьому слайді».
- Don't read the slide text aloud word for word — the narration explains, the slide summarizes.

SLIDES
- First slide: layout "title". Last slide: layout "closing" (takeaways + a short final line).
- Use a "statement" slide for a key idea or a chapter break, sparingly.
- Pick the layout that fits the content: "bullets" (3–6 short points), "cards" (2–4 parallel
  items, e.g. steps or components), "stats" (2–3 numbers that ARE in the source), "quote" (only a
  real quote from the source, with its source), "comparison" (before/after, do/don't).
- Vary layouts; avoid more than two "bullets" slides in a row.
- On-screen text is terse: kicker = 1–3 words (section label), title ≤ 60 characters, lead
  (optional subtitle) ≤ 110 characters, bullet ≤ 80 characters, card title ≤ 22, card text ≤ 70.
  On slides, numbers are written as digits.
- Fields that a layout doesn't use must be null or empty arrays.
"""

_TEXT_LIST = {"type": "array", "items": {"type": "string"}}
_COLUMN = {"anyOf": [{"type": "object", "properties": {"title": {"type": "string"}, "items": _TEXT_LIST},
                      "required": ["title", "items"], "additionalProperties": False}, {"type": "null"}]}
_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
SLIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "layout": {"type": "string", "enum": LAYOUTS},
        "kicker": {"type": "string"},
        "title": {"type": "string"},
        "lead": _NULLABLE_STR,
        "meta": _NULLABLE_STR,
        "statement": _NULLABLE_STR,
        "note": _NULLABLE_STR,
        "bullets": _TEXT_LIST,
        "cards": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "text": {"type": "string"}},
            "required": ["title", "text"], "additionalProperties": False}},
        "stats": {"type": "array", "items": {"type": "object", "properties": {
            "value": {"type": "string"}, "label": {"type": "string"}},
            "required": ["value", "label"], "additionalProperties": False}},
        "quote": {"anyOf": [{"type": "object", "properties": {
            "text": {"type": "string"}, "source": {"type": "string"}},
            "required": ["text", "source"], "additionalProperties": False}, {"type": "null"}]},
        "left": _COLUMN,
        "right": _COLUMN,
        "narration": {"type": "string"},
    },
    "required": ["layout", "kicker", "title", "lead", "meta", "statement", "note", "bullets", "cards",
                 "stats", "quote", "left", "right", "narration"],
    "additionalProperties": False,
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "footer": {"type": "string"},
                   "slides": {"type": "array", "items": SLIDE_SCHEMA}},
    "required": ["title", "footer", "slides"],
    "additionalProperties": False,
}
LIMITS = {"kicker": 40, "title": 70, "lead": 130, "meta": 140, "statement": 160, "note": 160}
WORD = re.compile(r"\w+", re.UNICODE)


# ---------- вхідний текст ----------

def read_lecture(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
        paras = ["".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
                 for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)]
        text = "\n\n".join(p for p in paras if p.strip())
    else:
        text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(WORD.findall(text)) < 80:
        raise StepError(f"{path.name}: замало тексту для лекції (менше 80 слів)")
    return text


def target_slides(words: int, cfg: dict[str, Any]) -> tuple[int, int]:
    per_slide = cfg.get("words_per_slide", 130)
    n = max(3, round(words / per_slide))
    return max(3, n - 2), n + 3


# ---------- перевірка плану ----------

def check_plan(plan: dict[str, Any], words_src: int, n_range: tuple[int, int]) -> list[str]:
    errors: list[str] = []
    slides = plan.get("slides") or []
    lo, hi = n_range
    if not lo <= len(slides) <= hi:
        errors.append(f"slides: {len(slides)} slides, expected {lo}–{hi} for this amount of text")
    if slides and slides[0]["layout"] != "title":
        errors.append("slide 1 must use layout 'title'")
    if slides and slides[-1]["layout"] != "closing":
        errors.append("the last slide must use layout 'closing'")
    narr_words = 0
    for i, s in enumerate(slides, start=1):
        p = f"slide {i} ({s['layout']})"
        for k, lim in LIMITS.items():
            if s.get(k) and len(s[k]) > lim:
                errors.append(f"{p}: {k} is {len(s[k])} chars, max {lim}")
        if not s["title"].strip():
            errors.append(f"{p}: empty title")
        w = len(WORD.findall(s["narration"]))
        narr_words += w
        if not 40 <= w <= 260:
            errors.append(f"{p}: narration has {w} words, keep 60–200")
        if len(s["narration"]) > 4500:
            errors.append(f"{p}: narration over 4500 characters")
        if re.search(r"\d", s["narration"]):
            errors.append(f"{p}: narration contains digits — write numbers in words")
        lay = s["layout"]
        if lay == "bullets" and not (3 <= len(s["bullets"]) <= 6):
            errors.append(f"{p}: needs 3–6 bullets")
        if lay == "bullets" and any(len(b) > 90 for b in s["bullets"]):
            errors.append(f"{p}: a bullet is longer than 90 chars")
        if lay == "cards":
            if not 2 <= len(s["cards"]) <= 4:
                errors.append(f"{p}: needs 2–4 cards")
            if any(len(c["title"]) > 26 or len(c["text"]) > 90 for c in s["cards"]):
                errors.append(f"{p}: card title ≤22 / text ≤70 chars")
        if lay == "stats":
            if not 2 <= len(s["stats"]) <= 3:
                errors.append(f"{p}: needs 2–3 stats")
            if any(len(x["value"]) > 8 or len(x["label"]) > 70 for x in s["stats"]):
                errors.append(f"{p}: stat value ≤8 / label ≤60 chars")
        if lay == "quote" and (not s["quote"] or len(s["quote"]["text"]) > 220):
            errors.append(f"{p}: needs a quote of ≤200 chars")
        if lay == "comparison":
            for side in ("left", "right"):
                col = s.get(side)
                if not col or not 2 <= len(col["items"]) <= 5 or any(len(x) > 70 for x in col["items"]):
                    errors.append(f"{p}: {side} needs a title and 2–5 items ≤60 chars")
        if lay == "statement" and not (s.get("statement") or s.get("lead")):
            errors.append(f"{p}: statement text is empty")
        if lay == "closing" and not s["bullets"]:
            errors.append(f"{p}: closing needs 2–4 takeaway bullets")
    if slides and narr_words < 0.6 * words_src:
        errors.append(f"narration covers {narr_words} words vs {words_src} in the source — "
                      "the source is being cut too much; keep all its content")
    return errors


# ---------- Claude ----------

def _call_claude(messages: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    import anthropic

    client = anthropic.Anthropic()
    params = dict(
        model=cfg["director"]["model"],
        max_tokens=64000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        messages=messages,
    )
    t0 = time.monotonic()
    # довга відповідь — потоково, щоб не впертися в HTTP-таймаут
    with client.beta.messages.stream(**params) as stream:
        response = stream.get_final_message()
    u = response.usage
    usage = {"provider": "anthropic_api", "step": "compose", "model": response.model,
             "stop_reason": response.stop_reason, "input_tokens": u.input_tokens,
             "output_tokens": u.output_tokens, "seconds": round(time.monotonic() - t0, 1)}
    if response.stop_reason == "refusal":
        raise StepError("Claude відмовився обробити текст (refusal)")
    if response.stop_reason == "max_tokens":
        raise StepError("план обрізано (max_tokens) — текст задовгий для одного виклику")
    return next(b.text for b in response.content if b.type == "text"), usage


def compose_plan(text: str, cfg: dict[str, Any], ctx: Context, job_id: str | None = None) -> dict[str, Any]:
    from scripts.direct import _CURRENT_JOB, _log_call

    if job_id:
        _CURRENT_JOB["id"] = job_id

    ccfg = cfg.get("compose", {})
    words = len(WORD.findall(text))
    n_range = target_slides(words, ccfg)
    user = (f"SOURCE LECTURE MATERIAL ({words} words). Target {n_range[0]}–{n_range[1]} slides.\n"
            f"Footer text for every slide: a short series/lecture label (≤ 40 chars).\n\n<source>\n{text}\n</source>")
    messages = [{"role": "user", "content": [{"type": "text", "text": user}]}]
    last_errors: list[str] = []
    for attempt in range(1, cfg["director"]["max_retries"] + 1):
        try:
            raw, usage = _call_claude(messages, cfg)
        except StepError:
            raise
        except Exception as e:  # помилки SDK/мережі
            if type(e).__module__.split(".")[0] == "anthropic":
                raise StepError(f"Claude API: {type(e).__name__}: {e}") from e
            raise
        _log_call({**usage, "attempt": attempt})
        try:
            plan = json.loads(raw)
        except json.JSONDecodeError as e:
            raise StepError(f"план не є JSON: {e}") from e
        last_errors = check_plan(plan, words, n_range)
        if not last_errors:
            ctx.log(f"  compose: {len(plan['slides'])} слайдів, спроба {attempt}")
            return plan
        ctx.log(f"  compose спроба {attempt}: {len(last_errors)} зауважень — повтор")
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": [{"type": "text", "text":
                      "The plan failed validation. Fix ALL of these and return the full corrected plan:\n- "
                      + "\n- ".join(last_errors[:40])}]}]
    raise StepError("план слайдів не пройшов перевірку: " + "; ".join(last_errors[:6]))


# ---------- крок ----------

def compose_deck(job: dict[str, Any], ctx: Context) -> Path:
    """Повертає шлях до згенерованого PPTX (з кешу, якщо текст і налаштування не змінились)."""
    cfg = ctx.cfg
    ccfg = cfg.get("compose", {})
    src = ROOT / job["inputs"]["lecture"]["path"]
    text = read_lecture(src)
    key = hashlib.sha256(json.dumps({"text": text, "model": cfg["director"]["model"],
                                     "words_per_slide": ccfg.get("words_per_slide", 130),
                                     "prompt": SYSTEM_PROMPT}, ensure_ascii=False).encode()).hexdigest()[:16]
    plan_path = COMPOSE / "deck_plan.json"
    meta_path = COMPOSE / "deck_plan.meta.json"
    if plan_path.exists() and meta_path.exists() and read_json(meta_path).get("key") == key:
        plan = read_json(plan_path)
        ctx.log(f"  compose: план з кешу ({len(plan['slides'])} слайдів; правки — у {plan_path.name})")
    else:
        if not cfg["data_policy"].get("allow_claude_api"):
            raise StepError("data_policy.allow_claude_api=false — скласти презентацію з тексту неможливо")
        plan = compose_plan(text, cfg, ctx, job["job_id"])
        write_json(plan_path, plan)
        write_json(meta_path, {"key": key, "source": job["inputs"]["lecture"]["path"]})
    if ccfg.get("footer"):
        plan = {**plan, "footer": ccfg["footer"]}
    out = COMPOSE / f"{job['job_id']}.pptx"
    render_deck(plan, out, ccfg.get("theme", "forest"))
    job.setdefault("flags", {})["composed_from_text"] = True
    return out
