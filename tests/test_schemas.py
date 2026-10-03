import json

import pytest

from scripts.common import SCHEMAS, validate
from jsonschema import Draft202012Validator

TIMELINE = {
    "version": "v001",
    "fps": 30,
    "width": 1920,
    "height": 1080,
    "duration": 196.8,
    "scenes": [
        {
            "scene_id": "s12",
            "start": 182.3,
            "end": 196.8,
            "slide": 7,
            "layout": "slide_inset",
            "presenter": {"shape": "circle", "position": "bottom-right", "scale": 0.22},
            "actions": [
                {"at": 184.0, "type": "zoom", "target": "slide7/shape12"},
                {"at": 188.5, "type": "highlight", "target": "slide7/table3/r2c4"},
            ],
            "generated_assets": [],
            "sync_confidence": 0.85,
        }
    ],
}


@pytest.mark.parametrize("path", sorted(SCHEMAS.glob("*.schema.json")), ids=lambda p: p.name)
def test_schema_is_valid(path):
    Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_guide_timeline_example_validates():
    assert validate("timeline", TIMELINE) == []


def test_free_form_target_rejected():
    bad = json.loads(json.dumps(TIMELINE))
    bad["scenes"][0]["actions"][1]["target"] = "revenue_column"
    assert validate("timeline", bad)


def test_qa_report_example_validates():
    report = {
        "status": "fail",
        "timeline_version": "v003",
        "stage": "preview",
        "issues": [{"scene_id": "s18", "time": 231.4, "type": "overlap", "check": "geometric",
                    "severity": "high", "fix": "move presenter to top-left"}],
    }
    assert validate("qa_report", report) == []


def test_manifest_hidden_slide_has_no_page():
    manifest = {
        "source": "input/presentation.pptx",
        "renderer": "powerpoint",
        "slide_width_px": 3840,
        "slide_height_px": 2160,
        "slides": [
            {"slide": 1, "hidden": False, "pdf_page": 1, "elements": [
                {"element_id": "slide1/shape2", "type": "title", "bbox": [10, 10, 100, 50], "text": "Тест"}]},
            {"slide": 2, "hidden": True, "pdf_page": None, "elements": []},
        ],
    }
    assert validate("slide_manifest", manifest) == []
