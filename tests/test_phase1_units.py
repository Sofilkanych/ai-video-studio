"""Юніт-тести модулів Фази 1 (без PowerPoint, whisper і Remotion)."""

import numpy as np
import pytest

from scripts import direct, layout, render, repair
from scripts.common import load_config
from scripts.glossary_fix import fix_words, similarity
from scripts.sync_slides import align, text_sync
from scripts.transcribe import build_captions, read_srt, words_from_segment, write_srt

CFG = load_config()


# ---------- glossary ----------

def test_glossary_translit_and_punctuation():
    words = [{"word": "компанії", "start": 1.0}, {"word": "ABK,", "start": 2.0}]
    log = fix_words(words, ["АБК"])
    assert words[1]["word"] == "АБК,"
    assert log[0]["from"] == "ABK" and log[0]["to"] == "АБК"


def test_glossary_leaves_unrelated_words():
    words = [{"word": "Привіт", "start": 0.0}, {"word": "світ", "start": 0.5}]
    assert fix_words(words, ["АБК", "Київський хлібозавод"]) == []
    assert similarity("світ", "АБК") < 0.84


def test_glossary_multiword_term():
    words = [{"word": "Київський", "start": 0.0}, {"word": "хлібозавот.", "start": 0.4}]
    fix_words(words, ["Київський хлібозавод"])
    assert [w["word"] for w in words] == ["Київський", "хлібозавод."]


# ---------- transcribe ----------

def _seg(text, start_ms, end_ms, tokens):
    return {"text": text, "offsets": {"from": start_ms, "to": end_ms},
            "tokens": [{"text": t, "t_dtw": d, "offsets": {"from": start_ms, "to": end_ms}, "p": 0.9}
                       for t, d in tokens]}


def test_words_use_dtw_times_and_are_monotonic():
    seg = _seg(" Наступний слайд. Тут", 26000, 28500,
               [("[_BEG_]", -1), (" Нас", 2672), ("тупний", 2690), (" слайд", 2740), (".", 2770), (" Тут", 2800)])
    words = words_from_segment(seg)
    assert [w["word"] for w in words] == ["Наступний", "слайд.", "Тут"]
    assert words[0]["start"] == pytest.approx(26.72)
    assert all(a["start"] <= b["start"] for a, b in zip(words, words[1:]))
    assert all(w["end"] > w["start"] for w in words)


def test_captions_limits_and_srt_roundtrip(tmp_path):
    words = [{"word": f"слово{i}.", "start": i * 0.4, "end": i * 0.4 + 0.35} for i in range(40)]
    caps = build_captions({"segments": [{"words": words}]})
    for c in caps:
        lines = c["text"].split("\n")
        assert len(lines) <= 2 and all(len(line) <= 42 for line in lines)
        assert c["end"] - c["start"] <= 6.0 + 1e-6
    assert all(a["end"] <= b["start"] for a, b in zip(caps, caps[1:]))
    write_srt(caps, tmp_path / "x.srt")
    back = read_srt(tmp_path / "x.srt")
    assert [c["text"] for c in back] == [c["text"] for c in caps]
    assert back[0]["start"] == pytest.approx(caps[0]["start"], abs=0.001)


# ---------- sync ----------

SLIDES = [
    {"slide": 1, "title": "Вступ", "notes": None, "elements": [{"text": "Компанія АБК огляд"}]},
    {"slide": 2, "title": "Таблиця", "notes": None, "elements": [{"text": "Виручка маржа сценарій"}]},
    {"slide": 4, "title": "Висновки", "notes": None, "elements": [{"text": "Підсумки ризики план"}]},
]


def _transcript(sentences):
    segs, t = [], 0.0
    for s in sentences:
        ws = []
        for w in s.split():
            ws.append({"word": w, "start": t, "end": t + 0.3})
            t += 0.4
        t += 1.0
        segs.append({"words": ws})
    return {"segments": segs}


def test_sync_full_verbal_cues():
    tr = _transcript(["Вітаю всіх.", "Наступний слайд. Таблиця виручки.", "Наступний слайд. Висновки."])
    out = text_sync(tr, SLIDES)
    assert [t["slide"] for t in out] == [1, 2, 4]
    assert all(t["source"] == "verbal_cue" for t in out)
    assert out[0]["start"] == 0.0 and out[1]["start"] < out[2]["start"]


def test_sync_text_alignment_without_cues():
    tr = _transcript(["Компанія АБК огляд року.", "Виручка і маржа за сценарієм.",
                      "Сценарій базовий.", "Підсумки і план ризиків."])
    out = text_sync(tr, SLIDES)
    assert [t["slide"] for t in out] == [1, 2, 4]
    assert {t["source"] for t in out[1:]} == {"text_alignment"}
    assert all(t["confidence"] <= 0.6 for t in out[1:])


def test_align_is_monotonic_and_covers_all_slides():
    units = [{"text": x} for x in ["огляд", "виручка", "маржа", "ризики", "план"]]
    path = align(units, SLIDES, set())
    assert path[0] == 0 and path[-1] == len(SLIDES) - 1
    assert all(a <= b for a, b in zip(path, path[1:]))


# ---------- layout ----------

@pytest.mark.parametrize("position", ["bottom-right", "top-right", "bottom-left", "top-left"])
def test_slide_inset_never_overlaps_presenter_or_caption(position):
    scene = {"layout": "slide_inset", "presenter": {"shape": "circle", "position": position, "scale": 0.22}}
    g = layout.scene_geometry(scene, 1920, 1080, 16 / 9, True)
    assert not layout.intersects(g.presenter, g.slide)
    assert not layout.intersects(g.caption, g.slide)
    for r in (g.slide, g.presenter, g.caption):
        assert r[0] >= 0 and r[1] >= 0 and r[0] + r[2] <= 1920 and r[1] + r[3] <= 1080


def test_slide_full_presenter_inside_frame():
    scene = {"layout": "slide_full", "presenter": {"shape": "rect", "position": "top-left", "scale": 0.2}}
    g = layout.scene_geometry(scene, 1920, 1080, 4 / 3, True)
    assert g.slide[2] == pytest.approx(1440)  # 4:3 у 16:9 — по висоті
    assert g.presenter[0] >= 0 and g.presenter[1] >= 0


# ---------- director ----------

def _inputs(has_presenter=True):
    manifest = {"slide_width_px": 3840, "slide_height_px": 2160, "slides": [
        {"slide": 1, "hidden": False, "title": "Вступ", "elements": []},
        {"slide": 2, "hidden": False, "title": "Таблиця", "elements": [
            {"element_id": "slide2/shape3", "type": "table", "bbox": [100, 500, 3000, 1000],
             "table": {"rows": 4, "cols": 3, "cells": []}},
            {"element_id": "slide2/shape3/r1c1", "type": "table_cell", "bbox": [1100, 750, 1000, 250]}]},
        {"slide": 3, "hidden": True, "title": "x", "elements": []},
    ]}
    timings = {"duration": 30.0, "transitions": [
        {"slide": 1, "start": 0.0, "source": "verbal_cue", "confidence": 0.9},
        {"slide": 2, "start": 10.0, "source": "verbal_cue", "confidence": 0.85}]}
    return {"manifest": manifest, "transcript": {"segments": []}, "timings": timings,
            "media": {"presenter": "work/media/presenter_cfr.mp4" if has_presenter else None}}


def test_fallback_plan_is_valid_and_reveals_table():
    inp = _inputs()
    tl = direct.assemble_timeline(direct.fallback_plan(inp), CFG, 30.0, "v001")
    assert direct.check_timeline(tl, inp) == []
    s2 = tl["scenes"][1]
    assert s2["layout"] == "slide_inset" and s2["actions"][0]["type"] == "table_reveal"


def test_fallback_without_presenter_uses_safe_inset():
    inp = _inputs(has_presenter=False)
    tl = direct.assemble_timeline(direct.fallback_plan(inp), CFG, 30.0, "v001")
    assert direct.check_timeline(tl, inp) == []
    assert all(s["layout"] == "slide_inset" and s["presenter"] is None for s in tl["scenes"])


def test_voice_only_inset_keeps_captions_below_slide():
    g = layout.scene_geometry({"layout": "slide_inset", "presenter": None}, 1920, 1080, 16 / 9, False)
    assert g.presenter is None
    assert not layout.intersects(g.caption, g.slide)
    assert g.caption[1] + g.caption[3] <= 1080 and g.slide[0] > 0


def test_check_timeline_catches_director_mistakes():
    inp = _inputs()
    plan = direct.fallback_plan(inp)
    plan["scenes"][0]["end"] = 12.0          # сцена слайда 1 заходить в інтервал слайда 2
    plan["scenes"][1]["start"] = 12.0
    plan["scenes"][1]["actions"] = [{"at": 13, "until": 15, "type": "highlight", "target": "slide2/shape99", "text": ""},
                                    {"at": 14, "until": 16, "type": "table_reveal", "target": "slide2/shape3/r1c1", "text": ""}]
    errors = direct.check_timeline(direct.assemble_timeline(plan, CFG, 30.0, "v001"), inp)
    joined = " | ".join(errors)
    assert "перетинає межу" in joined
    assert "slide2/shape99" in joined
    assert "table_reveal лише для елемента-таблиці" in joined
    assert "ближче 2 с" in joined


def test_provider_respects_data_policy(monkeypatch):
    cfg = {"director": {"provider": "anthropic_api"},
           "data_policy": {"allow_claude_api": True, "allow_consumer_subscription": False}}
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert direct.choose_provider(cfg) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert direct.choose_provider(cfg) == "anthropic_api"
    cfg["director"]["provider"] = "claude_cli"
    assert direct.choose_provider(cfg) is None  # особиста підписка заборонена


# ---------- render ----------

def test_plan_chunks_cover_all_frames_on_scene_boundaries():
    tl = {"fps": 30, "duration": 200.0, "scenes": [{"start": float(s), "end": float(s + 25)} for s in range(0, 200, 25)]}
    chunks = render.plan_chunks(tl, 60)
    assert chunks[0][0] == 0 and chunks[-1][1] == 6000 - 1
    assert all(b + 1 == c for (_, b), (c, _) in zip(chunks, chunks[1:]))
    assert all((a % (25 * 30)) == 0 for a, _ in chunks)


def test_chunk_hash_changes_only_for_affected_range():
    def props(layout2):
        return {"timeline": {"fps": 30, "width": 1920, "height": 1080, "duration": 20.0, "scenes": [
            {"scene_id": "s1", "start": 0.0, "end": 10.0, "layout": "slide_inset"},
            {"scene_id": "s2", "start": 10.0, "end": 20.0, "layout": layout2}]},
            "geometry": {"s1": {}, "s2": {}}, "captions": [], "slides": {}, "elements": {},
            "presenter": None, "style": {}}
    a, b = props("slide_inset"), props("slide_full")
    p = {"crf": 18}
    assert render.chunk_hash(a, 0, 299, p, "x", "y") == render.chunk_hash(b, 0, 299, p, "x", "y")
    assert render.chunk_hash(a, 300, 599, p, "x", "y") != render.chunk_hash(b, 300, 599, p, "x", "y")


# ---------- repair ----------

def test_fix_scene_moves_presenter_off_content():
    mask = np.zeros((270, 480), dtype=bool)
    mask[150:270, 400:480] = True  # вміст у правому нижньому куті слайда
    scene = {"scene_id": "s1", "layout": "slide_full", "slide": 1,
             "presenter": {"shape": "circle", "position": "bottom-right", "scale": 0.22}}
    props = {"timeline": {"width": 1920, "height": 1080}}
    note = repair.fix_scene(scene, props, mask, 16 / 9, True)
    assert scene["presenter"]["position"] != "bottom-right" or scene["layout"] == "slide_inset"
    assert note
    assert repair._scene_ok(scene, props, mask, 16 / 9, True)


def test_next_version():
    assert repair.next_version("v009") == "v010"


# ---------- регресії після верифікації ----------

def test_glossary_does_not_touch_inflections():
    words = [{"word": "Лесі", "start": 0.0}, {"word": "Кропівницький", "start": 0.5}]
    fix_words(words, ["Леся", "Кропивницький"])
    assert words[0]["word"] == "Лесі"            # відмінок, не помилка
    assert words[1]["word"] == "Кропивницький"     # реальна помилка розпізнавання


def test_assemble_snaps_to_frames_and_closes_gaps():
    inp = _inputs()
    plan = direct.fallback_plan(inp)
    plan["scenes"][0]["end"] = 10.013
    plan["scenes"][1]["start"] = 10.047          # розрив 34 мс
    tl = direct.assemble_timeline(plan, CFG, 30.0, "v001")
    s1, s2 = tl["scenes"]
    assert s1["end"] == s2["start"]
    assert abs(s2["start"] * 30 - round(s2["start"] * 30)) < 1e-6


def test_check_timeline_low_confidence_and_presenter_full():
    inp = _inputs()
    inp["timings"]["transitions"][1]["confidence"] = 0.5
    plan = direct.fallback_plan(inp)
    plan["scenes"][1]["actions"] = [{"at": 12, "until": 14, "type": "highlight", "target": "slide2/shape3", "text": ""}]
    plan["scenes"][0]["layout"] = "presenter_full"   # 10 с — межа
    errors = direct.check_timeline(direct.assemble_timeline(plan, CFG, 30.0, "v001"), inp)
    assert any("низька впевненість" in e for e in errors)
    assert not any("presenter_full довше" in e for e in errors)
    plan["scenes"][1]["layout"] = "presenter_full"   # 20 с
    plan["scenes"][1]["actions"] = []
    errors = direct.check_timeline(direct.assemble_timeline(plan, CFG, 30.0, "v001"), inp)
    assert any("presenter_full довше 10 с" in e for e in errors)


def test_chunk_hash_includes_caption_settings():
    base = {"timeline": {"fps": 30, "width": 1920, "height": 1080, "duration": 10.0,
                         "captions": {"enabled": True}, "scenes": []},
            "geometry": {}, "captions": [], "slides": {}, "elements": {}, "presenter": None, "style": {}}
    other = {**base, "timeline": {**base["timeline"], "captions": {"enabled": False}}}
    assert render.chunk_hash(base, 0, 299, {}, "x", "y") != render.chunk_hash(other, 0, 299, {}, "x", "y")


def test_glossary_keeps_name_case_forms():
    words = [{"word": "Олександра", "start": 0.0}, {"word": "Олександрові", "start": 0.4}]
    fix_words(words, ["Олександр"])
    assert [w["word"] for w in words] == ["Олександра", "Олександрові"]


def test_no_burned_captions_gives_bigger_slide():
    scene = {"layout": "slide_inset", "presenter": None}
    with_caps = layout.scene_geometry(scene, 1920, 1080, 16 / 9, False, layout.caption_band({"captions": {"enabled": True}}))
    no_caps = layout.scene_geometry(scene, 1920, 1080, 16 / 9, False, layout.caption_band({"captions": {"enabled": False}}))
    assert no_caps.slide[2] > with_caps.slide[2]
    assert no_caps.slide[1] + no_caps.slide[3] <= 1080


def test_burn_in_setting_controls_timeline_captions():
    inp = _inputs()
    cfg = {**CFG, "captions": {"burn_in": False}}
    tl = direct.assemble_timeline(direct.fallback_plan(inp), cfg, 30.0, "v001")
    assert tl["captions"]["enabled"] is False
