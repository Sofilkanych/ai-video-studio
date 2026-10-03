"""Життєвий цикл job через run.py у тимчасовій папці (input/ проєкту не чіпаємо)."""

import pytest

import run
from scripts import state, steps
from scripts.common import read_json


def _not_implemented(name):
    def step(job, ctx):
        raise state.NotImplementedStep(f"крок «{name}» реалізується у Фазі 1")
    return step


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    inp, work = tmp_path / "input", tmp_path / "work"
    inp.mkdir()
    work.mkdir()
    monkeypatch.setattr(state, "ROOT", tmp_path)
    monkeypatch.setattr(state, "INPUT", inp)
    monkeypatch.setattr(state, "JOB_PATH", work / "job.json")
    monkeypatch.setattr(steps, "WORK", work)
    # Справжні кроки пишуть у work/ і output/ проєкту — у тестах лише заглушки
    for name in list(run.STEPS):
        if name != "review_transcript":
            monkeypatch.setitem(run.STEPS, name, _not_implemented(name))
    (inp / "presentation.pptx").write_bytes(b"deck")
    (inp / "presenter.mov").write_bytes(b"video")
    return inp, work


def test_creates_job_and_stops_at_unimplemented_step(sandbox, capsys):
    _, work = sandbox
    assert run.cmd_run(new=False, approve_transcript=False) == 2
    job = read_json(work / "job.json")
    assert job["state"] == "NEW"
    assert job["error"]["step"] == "ingest"
    assert "Фазі 1" in capsys.readouterr().out


def test_rerun_resumes_same_job(sandbox):
    _, work = sandbox
    run.cmd_run(new=False, approve_transcript=False)
    first = read_json(work / "job.json")["job_id"]
    run.cmd_run(new=False, approve_transcript=False)
    assert read_json(work / "job.json")["job_id"] == first


def test_changed_input_requires_new(sandbox, capsys):
    inp, work = sandbox
    run.cmd_run(new=False, approve_transcript=False)
    (inp / "presenter.mov").write_bytes(b"other video")
    assert run.cmd_run(new=False, approve_transcript=False) == 1
    assert "змінилися (media)" in capsys.readouterr().out


def test_new_archives_previous_job(sandbox, monkeypatch):
    _, work = sandbox
    run.cmd_run(new=False, approve_transcript=False)
    old = read_json(work / "job.json")["job_id"]
    # job_id має секундну точність — гарантуємо інший id
    monkeypatch.setattr(state.time, "strftime",
                        lambda fmt, *a: "job_20991231_235959" if fmt.startswith("job_") else "2099-12-31T23:59:59+0000")
    run.cmd_run(new=True, approve_transcript=False)
    assert (work / "jobs_archive" / f"{old}.json").exists()
    assert read_json(work / "job.json")["job_id"] == "job_20991231_235959"


def test_transcript_checkpoint_pauses_then_continues(sandbox, monkeypatch):
    _, work = sandbox
    monkeypatch.setitem(run.STEPS, "ingest", lambda job, ctx: "INGESTED")
    monkeypatch.setitem(run.STEPS, "transcribe", lambda job, ctx: "TRANSCRIBED")
    cfg = run.load_config()
    cfg["review"]["transcript"] = True
    monkeypatch.setattr(run, "load_config", lambda: cfg)

    assert run.cmd_run(new=False, approve_transcript=False) == 0
    assert read_json(work / "job.json")["state"] == "TRANSCRIBED"

    assert run.cmd_run(new=False, approve_transcript=True) == 2  # далі — sync_slides (Фаза 1)
    job = read_json(work / "job.json")
    assert job["state"] == "TRANSCRIPT_REVIEWED"
    assert job["error"]["step"] == "sync_slides"


def test_qa_dead_end_records_error(sandbox, monkeypatch):
    _, work = sandbox
    for step, target in [("ingest", "INGESTED"), ("transcribe", "TRANSCRIBED"),
                         ("sync_slides", "SLIDES_SYNCED"), ("direct", "DIRECTED"),
                         ("build_assets", "ASSETS_READY"), ("render_preview", "PREVIEW_RENDERED"),
                         ("qa_preview", "QA_FAILED"), ("repair", "PATCHED"),
                         ("safe_fallback", "PATCHED")]:
        monkeypatch.setitem(run.STEPS, step, lambda job, ctx, t=target: t)
    assert run.cmd_run(new=False, approve_transcript=False) == 1
    job = read_json(work / "job.json")
    assert job["state"] == "QA_FAILED"
    assert job["flags"]["fallback_applied"] is True
    assert job["repair_loops"] == run.load_config()["qa"]["max_repair_loops"]
    assert job["error"]["step"] == "qa"


def test_failed_repair_goes_to_safe_fallback(sandbox, monkeypatch):
    _, work = sandbox
    for step, target in [("ingest", "INGESTED"), ("transcribe", "TRANSCRIBED"),
                         ("sync_slides", "SLIDES_SYNCED"), ("direct", "DIRECTED"),
                         ("build_assets", "ASSETS_READY"), ("render_preview", "PREVIEW_RENDERED"),
                         ("safe_fallback", "PATCHED"), ("render_final", "FINAL_RENDERED"),
                         ("qa_final", "FINAL_QA_PASSED"), ("finalize", "DONE")]:
        monkeypatch.setitem(run.STEPS, step, lambda job, ctx, t=target: t)
    qa_results = iter(["QA_FAILED", "QA_PASSED"])
    monkeypatch.setitem(run.STEPS, "qa_preview", lambda job, ctx: next(qa_results))

    def failing_repair(job, ctx):
        raise state.StepError("лише технічні проблеми")
    monkeypatch.setitem(run.STEPS, "repair", failing_repair)
    assert run.cmd_run(new=False, approve_transcript=False) == 0
    job = read_json(work / "job.json")
    assert job["state"] == "DONE"
    assert job["flags"]["fallback_applied"] is True
