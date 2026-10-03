"""Режим «сценарій»: розбір, словник вимови, таймкоди слів, адаптер ElevenLabs (без мережі)."""

import base64
import io
import json

import pytest

from scripts import tts
from scripts.script_input import ScriptError, build_scripts, parse_script_file, split_parts
from scripts.synthesize import spoken_form, word_times
from scripts.tts import ElevenLabsTTS, Synth, TTSError, make_tts


def test_parse_script_headings_and_preamble(tmp_path):
    f = tmp_path / "script.md"
    f.write_text("Вступні нотатки (ігноруються)\n\n## Слайд 1\nВітаю!\n\nСлайд 3: Таблиця.\nДругий рядок.\n",
                 encoding="utf-8")
    assert parse_script_file(f) == {1: "Вітаю!", 3: "Таблиця.\nДругий рядок."}


def test_parse_script_duplicate_slide_fails(tmp_path):
    f = tmp_path / "script.md"
    f.write_text("## Слайд 1\nа\n## Слайд 1\nб\n", encoding="utf-8")
    with pytest.raises(ScriptError, match="двічі"):
        parse_script_file(f)


def test_pause_markers_and_markdown_cleanup():
    parts = split_parts("**Перше** речення. [пауза] - Друге [пауза 2,5] третє [пауза]", 1.0)
    assert [(p.text, p.pause_after) for p in parts] == [("Перше речення.", 1.0), ("Друге", 2.5), ("третє", 1.0)]


def test_build_scripts_notes_fallback_empty_slide_and_unknown():
    scripts = build_scripts([1, 2, 4], None, {1: "Привіт.", 2: None, 4: "Кінець."}, 1.0, 3.0)
    assert [s.slide for s in scripts] == [1, 2, 4]
    assert scripts[1].parts[0].text == "" and scripts[1].parts[0].pause_after == 3.0
    with pytest.raises(ScriptError, match="немає серед видимих"):
        build_scripts([1, 2], {1: "a", 3: "b"}, {}, 1.0, 3.0)


def test_spoken_form_keeps_script_words_and_spans():
    spoken, spans = spoken_form("Компанія АБК, 2026.", {"АБК": "а-бе-ка"})
    assert spoken == "Компанія а-бе-ка, 2026."
    assert [w for w, _, _ in spans] == ["Компанія", "АБК,", "2026."]
    assert [spoken[a:b] for _, a, b in spans] == ["Компанія", "а-бе-ка,", "2026."]


def test_word_times_from_alignment_and_proportional():
    spoken, spans = spoken_form("Тут АБК.", {"АБК": "а-бе-ка"})
    chars = list(spoken)
    starts = [i * 0.1 for i in range(len(chars))]
    ends = [s + 0.1 for s in starts]
    words = word_times(spans, Synth(None, chars, starts, ends), spoken, 2.0)
    assert words[0] == {"word": "Тут", "start": 0.0, "end": pytest.approx(0.3)}
    assert words[1]["word"] == "АБК." and words[1]["start"] == pytest.approx(0.4)
    prop = word_times(spans, Synth(None, None, None, None), spoken, 2.0)
    assert prop[0]["start"] == 0.0 and prop[-1]["end"] == pytest.approx(2.0)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_elevenlabs_request_alignment_and_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(tts, "LOGS", tmp_path / "logs")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k-test")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice123")
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req)
        body = json.loads(req.data)
        text = body["text"]
        return _Resp(json.dumps({
            "audio_base64": base64.b64encode(b"ID3fake").decode(),
            "alignment": {"characters": list(text),
                          "character_start_times_seconds": [i * 0.05 for i in range(len(text))],
                          "character_end_times_seconds": [i * 0.05 + 0.05 for i in range(len(text))]},
        }).encode())

    monkeypatch.setattr(tts.urllib.request, "urlopen", fake_urlopen)
    cfg = {"provider": "elevenlabs", "model": "eleven_multilingual_v2", "output_format": "mp3_44100_128",
           "voice_settings": {"stability": 0.5}, "language_code": None, "seed": 42}
    engine = make_tts(cfg, {"allow_tts_cloud": True})
    s1 = engine.synth("Привіт.", previous_text="", next_text="Далі.")
    req = calls[0]
    assert "/v1/text-to-speech/voice123/with-timestamps?output_format=mp3_44100_128" in req.full_url
    assert req.headers["Xi-api-key"] == "k-test"
    body = json.loads(req.data)
    assert body["model_id"] == "eleven_multilingual_v2" and body["next_text"] == "Далі." and body["seed"] == 42
    assert "language_code" not in body and "previous_text" not in body
    assert s1.chars == list("Привіт.") and s1.audio.read_bytes() == b"ID3fake" and not s1.cached
    s2 = engine.synth("Привіт.", previous_text="", next_text="Далі.")
    assert s2.cached and len(calls) == 1  # повтор — з кешу, без кредитів
    log = (tmp_path / "logs" / "tts_calls.jsonl").read_text().splitlines()
    assert len(log) == 1 and json.loads(log[0])["characters"] == 7


def test_elevenlabs_requires_key_and_policy(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    cfg = {"provider": "elevenlabs", "model": "m"}
    with pytest.raises(TTSError, match="allow_tts_cloud"):
        make_tts(cfg, {"allow_tts_cloud": False})
    with pytest.raises(TTSError, match="ELEVENLABS_API_KEY"):
        ElevenLabsTTS(cfg)


# ---------- регресії після верифікації ----------

def test_body_line_starting_with_slide_is_not_heading(tmp_path):
    f = tmp_path / "script.md"
    f.write_text("﻿## Слайд 1\nСлайд 1 показує графік.\nСлайд 3 теж важливий.\n<!-- нотатка автора -->\n"
                 "Слайд 2: Наступний.\n", encoding="utf-8")
    texts = parse_script_file(f)
    assert texts == {1: "Слайд 1 показує графік.\nСлайд 3 теж важливий.", 2: "Наступний."}


def test_long_text_split_under_limit_keeps_pause_on_last():
    from scripts.script_input import MAX_TTS_CHARS
    sentence = "Це досить довге речення про результати компанії за рік. "
    raw = sentence * 200 + "[пауза 2]"
    parts = split_parts(raw, 1.0)
    assert len(parts) > 1 and all(len(p.text) <= MAX_TTS_CHARS for p in parts)
    assert [p.pause_after for p in parts[:-1]] == [0.0] * (len(parts) - 1) and parts[-1].pause_after == 2.0
    # жодне слово не загубилося і порядок збережено
    assert " ".join(p.text for p in parts).split() == (sentence * 200).split()


def test_alignment_with_short_ends_falls_back_to_proportional():
    spoken, spans = spoken_form("Тут АБК.", {})
    chars = list(spoken)
    words = word_times(spans, Synth(None, chars, [0.1] * len(chars), [0.2] * 3), spoken, 2.0)
    assert words[0]["start"] == 0.0 and words[-1]["end"] == pytest.approx(2.0)


def test_elevenlabs_timeout_becomes_tts_error(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(tts, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    attempts = []

    def boom(req, timeout):
        attempts.append(1)
        raise TimeoutError("read timed out")
    monkeypatch.setattr(tts.urllib.request, "urlopen", boom)
    engine = ElevenLabsTTS({"model": "m", "output_format": "mp3_44100_128"})
    with pytest.raises(TTSError, match="TimeoutError"):
        engine.synth("Привіт.")
    assert len(attempts) == 4  # 1 + 3 повтори


def test_offline_only_blocks_cloud_tts():
    with pytest.raises(TTSError, match="offline_only"):
        make_tts({"provider": "elevenlabs", "model": "m"}, {"allow_tts_cloud": True, "offline_only": True})


def test_non_mp3_output_format_rejected(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    with pytest.raises(TTSError, match="mp3"):
        ElevenLabsTTS({"model": "m", "output_format": "pcm_44100"})


def test_lexicon_multiword_phrase_maps_all_words_to_one_span():
    lex = {"2 жовтня 2026 року": "друге жовтня дві тисячі двадцять шостого року", "xAI": "ікс-ей-ай"}
    spoken, spans = spoken_form("станом на 2 жовтня 2026 року. А xAI: так", lex)
    assert spoken == "станом на друге жовтня дві тисячі двадцять шостого року. А ікс-ей-ай: так"
    assert [w for w, _, _ in spans] == ["станом", "на", "2", "жовтня", "2026", "року.", "А", "xAI:", "так"]
    date_spans = {(a, b) for w, a, b in spans if w in ("2", "жовтня", "2026", "року.")}
    assert len(date_spans) == 1  # фраза має один спільний проміжок
    (a, b), = date_spans
    assert spoken[a:b] == "друге жовтня дві тисячі двадцять шостого року."


def test_lexicon_phrase_not_matched_across_punctuation():
    spoken, _ = spoken_form("на 2, жовтня 2026 року", {"2 жовтня 2026 року": "X"})
    assert spoken == "на 2, жовтня 2026 року"
