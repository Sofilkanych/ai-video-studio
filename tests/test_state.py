import pytest

from scripts import state
from scripts.common import validate
from scripts.state import StepError, create_job, discover_inputs, next_step

CFG = {"review": {"transcript": False}, "qa": {"max_repair_loops": 2}}


def make_inputs(tmp_path, names):
    for n in names:
        (tmp_path / n).write_bytes(b"x" * 10)
    return tmp_path


def test_happy_path_order():
    job = {"state": "NEW", "repair_loops": 0, "flags": {}}
    seen = []
    order = ["INGESTED", "TRANSCRIBED", "SLIDES_SYNCED", "DIRECTED", "ASSETS_READY",
             "PREVIEW_RENDERED", "QA_PASSED", "FINAL_RENDERED", "FINAL_QA_PASSED", "DONE"]
    for target in order:
        step = next_step(job, CFG)
        assert target in state.ALLOWED[step]
        seen.append(step)
        job["state"] = target
    assert next_step(job, CFG) is None
    assert "review_transcript" not in seen


def test_review_checkpoint_when_enabled():
    job = {"state": "TRANSCRIBED"}
    assert next_step(job, {"review": {"transcript": True}}) == "review_transcript"
    assert next_step(job, CFG) == "sync_slides"


def test_repair_loop_limit_then_fallback_then_stop():
    job = {"state": "QA_FAILED", "repair_loops": 0, "flags": {}}
    assert next_step(job, CFG) == "repair"
    job["repair_loops"] = 2
    assert next_step(job, CFG) == "safe_fallback"
    job["flags"]["fallback_applied"] = True
    assert next_step(job, CFG) is None


def test_every_state_has_a_route():
    for s in state.STATES:
        job = {"state": s, "repair_loops": 0, "flags": {}}
        next_step(job, CFG)  # не кидає KeyError


def test_discover_preferred_names(tmp_path):
    make_inputs(tmp_path, ["presentation.pptx", "presenter.mp4", "screen_recording.mp4"])
    found = discover_inputs(tmp_path)
    assert found["presentation"].name == "presentation.pptx"
    assert found["media"].name == "presenter.mp4"
    assert found["screen_recording"].name == "screen_recording.mp4"


def test_discover_single_arbitrary_names(tmp_path):
    make_inputs(tmp_path, ["Звіт Q3.pptx", "IMG_0042.MOV"])
    found = discover_inputs(tmp_path)
    assert found["presentation"].name == "Звіт Q3.pptx"
    assert found["media"].name == "IMG_0042.MOV"
    assert "screen_recording" not in found


def test_discover_ambiguous_fails(tmp_path):
    make_inputs(tmp_path, ["a.pptx", "b.pptx", "presenter.mp4"])
    with pytest.raises(StepError, match="кілька кандидатів"):
        discover_inputs(tmp_path)


def test_discover_pdf_without_media_or_script_fails(tmp_path):
    make_inputs(tmp_path, ["presentation.pdf"])
    with pytest.raises(StepError, match="немає ні запису ведучого, ні сценарію"):
        discover_inputs(tmp_path)


def test_discover_script_mode(tmp_path):
    make_inputs(tmp_path, ["presentation.pptx", "script.md"])
    found = discover_inputs(tmp_path)
    assert found["script"].name == "script.md" and "media" not in found
    assert state.job_mode(found) == "script"


def test_discover_pptx_alone_uses_notes(tmp_path):
    make_inputs(tmp_path, ["presentation.pptx"])
    found = discover_inputs(tmp_path)
    assert set(found) == {"presentation"} and state.job_mode(found) == "script"


def test_discover_media_and_script_is_ambiguous(tmp_path):
    make_inputs(tmp_path, ["presentation.pptx", "script.md", "presenter.mp4"])
    with pytest.raises(StepError, match="залиште щось одне"):
        discover_inputs(tmp_path)


def test_script_mode_skips_transcript_review():
    job = {"state": "TRANSCRIBED", "mode": "script"}
    assert next_step(job, {"review": {"transcript": True}}) == "sync_slides"


def test_created_job_matches_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "ROOT", tmp_path)
    make_inputs(tmp_path, ["presentation.pptx", "voice.wav"])
    job = create_job(discover_inputs(tmp_path))
    assert validate("job", job) == []
    assert job["inputs"]["media"]["path"] == "voice.wav"


def test_discover_ignores_office_lock_files(tmp_path):
    make_inputs(tmp_path, ["Звіт Q3.pptx", "~$Звіт Q3.pptx", "IMG_0042.MOV"])
    assert discover_inputs(tmp_path)["presentation"].name == "Звіт Q3.pptx"


def test_job_date_time_format_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "ROOT", tmp_path)
    make_inputs(tmp_path, ["presentation.pptx", "voice.wav"])
    job = create_job(discover_inputs(tmp_path))
    job["created_at"] = "garbage"
    assert any("created_at" in e for e in validate("job", job))
