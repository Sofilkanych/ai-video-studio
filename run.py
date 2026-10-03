#!/usr/bin/env python3
"""AI Video Studio — єдина точка входу.

    python run.py                       створити або продовжити job
    python run.py --new                 почати новий job (старий архівується)
    python run.py --approve-transcript  продовжити після перевірки транскрипту
    python run.py --input DIR           взяти вхідні файли з DIR замість input/
    python run.py --redo STEP           повторити з кроку STEP (напр. direct після правок)
    python run.py --tts macos_say       режим сценарію: чернетка локальним голосом (без ElevenLabs)
    python run.py status                стан поточного job
    python run.py doctor [--powerpoint] [--hdr] [--render] [--all]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts import state
from scripts.common import load_config, load_env
from scripts.state import (ALLOWED, STEP_ENTRY, Context, NotImplementedStep, StepError, create_job,
                           discover_inputs, inputs_changed, load_job, next_step, save_job,
                           transition)
from scripts.steps import STEPS


def cmd_status() -> int:
    job = load_job()
    if not job:
        print("Job ще не створено. Покладіть файли в input/ і запустіть `python run.py`.")
        return 0
    print(f"{job['job_id']}  стан: {job['state']}")
    for role, entry in job["inputs"].items():
        print(f"  {role}: {entry['path']}")
    if job.get("error"):
        print(f"  помилка на кроці «{job['error']['step']}»: {job['error']['message']}")
    nxt = next_step(job, load_config())
    print(f"  наступний крок: {nxt or '—'}")
    return 0


def cmd_run(new: bool, approve_transcript: bool, input_dir: Path | None = None,
            redo: str | None = None, tts: str | None = None) -> int:
    cfg = load_config()
    if tts:
        cfg["tts"]["provider"] = tts
    try:
        inputs = discover_inputs(input_dir)
    except StepError as e:
        print(f"✗ {e}")
        return 1

    job = load_job()
    if job and new:
        archive = state.JOB_PATH.parent / "jobs_archive" / f"{job['job_id']}.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        state.JOB_PATH.replace(archive)
        print(f"Попередній job заархівовано: {archive.name}")
        job = None
    if job:
        changed = inputs_changed(job, inputs)
        if changed:
            print(f"✗ Вхідні файли змінилися ({', '.join(changed)}). "
                  "Запустіть `python run.py --new`, щоб почати новий job.")
            return 1
    else:
        job = create_job(inputs)
        save_job(job)
        print(f"Створено {job['job_id']}")

    if redo:
        transition(job, STEP_ENTRY[redo], note=f"redo {redo}")
        job["repair_loops"] = 0
        job.setdefault("flags", {}).pop("fallback_applied", None)
        save_job(job)
        print(f"↺ повтор з кроку «{redo}»")

    ctx = Context(cfg=cfg, approve_transcript=approve_transcript)
    while step := next_step(job, cfg):
        print(f"→ {step}")
        try:
            new_state = STEPS[step](job, ctx)
        except NotImplementedStep as e:
            job["error"] = {"step": step, "message": str(e)}
            save_job(job)
            print(f"■ Зупинено: {e}")
            return 2
        except StepError as e:
            if step == "repair":
                # автоматично не виправляється — одразу до safe fallback (next_step)
                job["repair_loops"] = cfg.get("qa", {}).get("max_repair_loops", 3)
                job["error"] = {"step": step, "message": str(e)}
                save_job(job)
                print(f"  repair неможливий: {e} → safe fallback")
                continue
            job["error"] = {"step": step, "message": str(e)}
            save_job(job)
            print(f"✗ Крок «{step}» не вдався: {e}")
            return 1
        if new_state not in ALLOWED[step]:
            raise RuntimeError(f"крок {step} повернув недопустимий стан {new_state}")
        if new_state == job["state"]:  # пауза (checkpoint)
            save_job(job)
            return 0
        if step == "repair":
            job["repair_loops"] = job.get("repair_loops", 0) + 1
        if step == "safe_fallback":
            job.setdefault("flags", {})["fallback_applied"] = True
        transition(job, new_state, note=step)
        save_job(job)

    if job["state"] == "DONE":
        print("✓ Готово: output/final.mp4")
        return 0
    message = "QA не пройдено навіть після safe fallback — потрібне втручання (див. work/qa/)"
    job["error"] = {"step": "qa", "message": message}
    save_job(job)
    print(f"✗ Зупинено у стані {job['state']}: {message}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Video Studio")
    sub = parser.add_subparsers(dest="cmd")
    doc = sub.add_parser("doctor", help="перевірка середовища")
    doc.add_argument("--powerpoint", action="store_true", help="тест експорту слайдів (відкриє PowerPoint)")
    doc.add_argument("--hdr", action="store_true", help="тест HDR → SDR")
    doc.add_argument("--render", action="store_true", help="тест рендеру чанками Remotion")
    doc.add_argument("--all", action="store_true", help="усі тести")
    sub.add_parser("status", help="стан поточного job")
    parser.add_argument("--new", action="store_true", help="почати новий job")
    parser.add_argument("--input", type=Path, default=None,
                        help="каталог з вхідними файлами замість input/ (напр. тестова фікстура)")
    parser.add_argument("--tts", choices=["elevenlabs", "macos_say"], default=None,
                        help="режим сценарію: провайдер голосу на цей запуск (macos_say — безкоштовна чернетка)")
    parser.add_argument("--redo", choices=sorted(STEP_ENTRY), default=None,
                        help="повторити job, починаючи з кроку (попередні результати зберігаються)")
    parser.add_argument("--approve-transcript", action="store_true",
                        help="підтвердити перевірений транскрипт")
    args = parser.parse_args(argv)
    load_env()

    if args.cmd == "doctor":
        from scripts import check_env
        return check_env.run(powerpoint=args.powerpoint or args.all,
                             hdr=args.hdr or args.all,
                             render=args.render or args.all)
    if args.cmd == "status":
        return cmd_status()
    return cmd_run(new=args.new, approve_transcript=args.approve_transcript,
                   input_dir=args.input.resolve() if args.input else None, redo=args.redo,
                   tts=args.tts)


if __name__ == "__main__":
    sys.exit(main())
