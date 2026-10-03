"""State machine job-а. Стан веде код, а не пам'ять LLM."""

from __future__ import annotations

import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from scripts.common import INPUT, ROOT, WORK, read_json, sha256_file, validate, write_json

JOB_PATH = WORK / "job.json"

STATES = [
    "NEW", "INGESTED", "TRANSCRIBED", "TRANSCRIPT_REVIEWED", "SLIDES_SYNCED",
    "DIRECTED", "ASSETS_READY", "PREVIEW_RENDERED", "QA_FAILED", "PATCHED",
    "QA_PASSED", "FINAL_RENDERED", "FINAL_QA_PASSED", "DONE",
]

# Крок → стани, які він має право повернути. Повернення поточного стану = пауза.
ALLOWED: dict[str, set[str]] = {
    "ingest": {"INGESTED"},
    "transcribe": {"TRANSCRIBED"},
    "review_transcript": {"TRANSCRIPT_REVIEWED", "TRANSCRIBED"},
    "sync_slides": {"SLIDES_SYNCED"},
    "direct": {"DIRECTED"},
    "build_assets": {"ASSETS_READY"},
    "render_preview": {"PREVIEW_RENDERED"},
    "qa_preview": {"QA_PASSED", "QA_FAILED"},
    "repair": {"PATCHED"},
    "safe_fallback": {"PATCHED"},
    "render_final": {"FINAL_RENDERED"},
    "qa_final": {"FINAL_QA_PASSED", "QA_FAILED"},
    "finalize": {"DONE"},
}

# Стан, з якого стартує крок (для --redo STEP)
STEP_ENTRY = {
    "ingest": "NEW", "transcribe": "INGESTED", "sync_slides": "TRANSCRIBED", "direct": "SLIDES_SYNCED",
    "build_assets": "DIRECTED", "render_preview": "ASSETS_READY", "qa_preview": "PREVIEW_RENDERED",
    "render_final": "QA_PASSED", "qa_final": "FINAL_RENDERED", "finalize": "FINAL_QA_PASSED",
}

PRESENTATION_EXT = {".pptx", ".pdf"}
SCRIPT_NAMES = ["script.md", "script.txt", "сценарій.md", "сценарій.txt"]
TEXT_EXT = {".md", ".txt", ".docx"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
AUDIO_EXT = {".wav", ".m4a", ".mp3", ".aac", ".flac"}


class StepError(RuntimeError):
    """Крок не вдався; job зберігає помилку, повторний запуск продовжить з цього стану."""


class NotImplementedStep(StepError):
    """Крок ще не реалізовано (поточна фаза)."""


@dataclass
class Context:
    cfg: dict[str, Any]
    approve_transcript: bool = False
    log: Callable[[str], None] = print
    extra: dict[str, Any] = field(default_factory=dict)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def next_step(job: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    state = job["state"]
    if state == "TRANSCRIBED":
        # у режимі «script» текст відомий — перевірка розпізнавання не потрібна
        review = cfg.get("review", {}).get("transcript") and job.get("mode", "recording") != "script"
        return "review_transcript" if review else "sync_slides"
    if state == "QA_FAILED":
        max_loops = cfg.get("qa", {}).get("max_repair_loops", 3)
        if job.get("repair_loops", 0) < max_loops:
            return "repair"
        if not job.get("flags", {}).get("fallback_applied"):
            return "safe_fallback"
        return None
    return {
        "NEW": "ingest",
        "INGESTED": "transcribe",
        "TRANSCRIPT_REVIEWED": "sync_slides",
        "SLIDES_SYNCED": "direct",
        "DIRECTED": "build_assets",
        "ASSETS_READY": "render_preview",
        "PREVIEW_RENDERED": "qa_preview",
        "PATCHED": "render_preview",
        "QA_PASSED": "render_final",
        "FINAL_RENDERED": "qa_final",
        "FINAL_QA_PASSED": "finalize",
        "DONE": None,
    }[state]


def _pick(files: list[Path], preferred: list[str], exts: set[str], role: str,
          required: bool) -> Path | None:
    import unicodedata

    def norm(name: str) -> str:  # APFS/iCloud можуть зберігати кирилицю в NFD
        return unicodedata.normalize("NFC", name).lower()
    for name in preferred:
        for f in files:
            if norm(f.name) == name:
                return f
    candidates = [f for f in files if f.suffix.lower() in exts and f.name.lower() not in preferred]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise StepError(f"{role}: кілька кандидатів у input/: {', '.join(c.name for c in candidates)}. "
                        f"Перейменуйте потрібний на {preferred[0]}.")
    if required:
        raise StepError(f"{role}: не знайдено в input/ (очікується {' або '.join(preferred)})")
    return None


def discover_inputs(input_dir: Path | None = None) -> dict[str, Path]:
    input_dir = input_dir or INPUT
    # пропускаємо приховані файли й lock-файли Office (~$name.pptx)
    files = [f for f in input_dir.iterdir()
             if f.is_file() and not f.name.startswith((".", "~$"))]
    screen = _pick(files, ["screen_recording.mp4", "screen_recording.mov"], set(),
                   "Запис екрана", required=False)
    script = _pick(files, SCRIPT_NAMES, set(), "Сценарій", required=False)
    rest = [f for f in files if f not in (screen, script)]
    found: dict[str, Path] = {}
    presentation = _pick(rest, ["presentation.pptx", "presentation.pdf"], PRESENTATION_EXT,
                         "Презентація", required=False)
    if presentation is None:
        # лише текст лекції → презентацію складе крок compose (режим «текст → презентація»)
        if script:
            rest.append(script)
        lecture = _pick(rest, ["lecture.md", "lecture.txt", "lecture.docx"], TEXT_EXT,
                        "Текст лекції (або презентація)", required=True)
        found["lecture"] = lecture
        if screen:
            found["screen_recording"] = screen
        return found
    found["presentation"] = presentation
    media = _pick(rest, ["presenter.mp4", "presenter.mov", "voice.wav"], VIDEO_EXT | AUDIO_EXT,
                  "Відео/аудіо ведучого", required=False)
    if media and script:
        raise StepError("в input/ є і запис ведучого, і сценарій — залиште щось одне "
                        "(запис → монтаж виступу; сценарій → озвучення голосом)")
    if media:
        found["media"] = media
    elif script:
        found["script"] = script
    elif found["presentation"].suffix.lower() != ".pptx":
        raise StepError("немає ні запису ведучого, ні сценарію (script.md); для PDF нотаток доповідача немає")
    # без запису і без файлу сценарію — сценарій з нотаток доповідача PPTX
    if screen:
        found["screen_recording"] = screen
    return found


def job_mode(inputs: dict[str, Any]) -> str:
    return "recording" if "media" in inputs else "script"


def _input_entry(path: Path) -> dict[str, Any]:
    return {"path": str(path.relative_to(ROOT)), "sha256": sha256_file(path), "bytes": path.stat().st_size}


def create_job(inputs: dict[str, Path]) -> dict[str, Any]:
    ts = now_iso()
    return {
        "job_id": time.strftime("job_%Y%m%d_%H%M%S"),
        "created_at": ts,
        "state": "NEW",
        "mode": job_mode(inputs),
        "inputs": {role: _input_entry(p) for role, p in inputs.items()},
        "timeline_version": None,
        "repair_loops": 0,
        "flags": {},
        "chunks": [],
        "error": None,
        "history": [{"at": ts, "state": "NEW", "note": "job created"}],
    }


def inputs_changed(job: dict[str, Any], inputs: dict[str, Path]) -> list[str]:
    changed = []
    for role, path in inputs.items():
        old = job["inputs"].get(role)
        if not old or old["sha256"] != sha256_file(path):
            changed.append(role)
    changed += [r for r in job["inputs"] if r not in inputs]
    return changed


def save_job(job: dict[str, Any], path: Path | None = None) -> None:
    errors = validate("job", job)
    if errors:
        raise StepError("job.json не відповідає схемі: " + "; ".join(errors))
    write_json(path or JOB_PATH, job)


def load_job(path: Path | None = None) -> dict[str, Any] | None:
    path = path or JOB_PATH
    return read_json(path) if path.exists() else None


def transition(job: dict[str, Any], new_state: str, note: str = "") -> None:
    job["state"] = new_state
    job["error"] = None
    entry = {"at": now_iso(), "state": new_state}
    if note:
        entry["note"] = note
    job["history"].append(entry)
