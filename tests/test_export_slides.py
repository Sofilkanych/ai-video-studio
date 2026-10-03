import pytest

from scripts.export_slides import ExportError, map_pages, slide_visibility
from scripts.make_fixture import build


def test_map_pages_skips_hidden():
    assert map_pages([True, True, False, True], 3) == [1, 2, None, 3]


def test_map_pages_count_mismatch_raises():
    with pytest.raises(ExportError, match="видимих слайдів 3"):
        map_pages([True, True, False, True], 4)


def test_fixture_has_one_hidden_slide(tmp_path):
    deck = build(tmp_path / "deck.pptx")
    assert slide_visibility(deck) == [True, True, True, False, True]
