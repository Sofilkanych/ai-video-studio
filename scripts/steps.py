"""Кроки pipeline. Кожен крок: (job, ctx) → новий стан; вихідні файли — у work/.

Кроки імпортуються ліниво, щоб `run.py status` не тягнув важкі залежності.
"""

from __future__ import annotations

from typing import Any, Callable

from scripts.common import WORK
from scripts.state import Context, NotImplementedStep

Step = Callable[[dict[str, Any], Context], str]


def _todo(name: str, phase: int = 1) -> Step:
    def step(job: dict[str, Any], ctx: Context) -> str:
        raise NotImplementedStep(f"крок «{name}» реалізується у Фазі {phase}")
    return step


def review_transcript(job: dict[str, Any], ctx: Context) -> str:
    if ctx.approve_transcript:
        # субтитри беруться з виправленого людиною review.srt
        from scripts.common import write_json
        from scripts.transcribe import read_srt
        review = WORK / "transcript" / "review.srt"
        if review.exists():
            captions = read_srt(review)
            if captions:
                write_json(WORK / "transcript" / "captions.json", captions)
                ctx.log(f"  субтитри з review.srt: {len(captions)}")
        return "TRANSCRIPT_REVIEWED"
    review = WORK / "transcript" / "review.srt"
    ctx.log(
        f"Пауза для перевірки транскрипту: виправте {review.relative_to(WORK.parent)} "
        "і запустіть `python run.py --approve-transcript`."
    )
    return "TRANSCRIBED"


def _lazy(module: str, func: str = "run") -> Step:
    def step(job: dict[str, Any], ctx: Context) -> str:
        import importlib
        return getattr(importlib.import_module(f"scripts.{module}"), func)(job, ctx)
    return step


def _by_mode(recording: Step, script: Step) -> Step:
    def step(job: dict[str, Any], ctx: Context) -> str:
        return (script if job.get("mode") == "script" else recording)(job, ctx)
    return step


STEPS: dict[str, Step] = {
    "ingest": _lazy("ingest"),
    "transcribe": _by_mode(_lazy("transcribe"), _lazy("synthesize")),
    "review_transcript": review_transcript,
    "sync_slides": _lazy("sync_slides"),
    "direct": _lazy("direct"),
    "build_assets": _lazy("assets"),
    "render_preview": _lazy("render", "run_preview"),
    "qa_preview": _lazy("qa_video", "run_preview"),
    "repair": _lazy("repair"),
    "safe_fallback": _lazy("repair", "safe_fallback"),
    "render_final": _lazy("render", "run_final"),
    "qa_final": _lazy("qa_video", "run_final"),
    "finalize": _lazy("repair", "finalize"),
}
