"""Крок Synthesize (режим «script»): сценарій по слайдах → голос → master WAV + transcript + slide_timings.

Замінює transcribe і sync_slides: таймкоди слайдів відомі точно (confidence 1.0), час слів —
з вирівнювання символів ElevenLabs (або пропорційно для локального голосу).
Субтитри показують текст сценарію; у TTS іде текст зі словника вимови (lexicon.txt).
"""

from __future__ import annotations

import json
import re
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np

from scripts.common import ROOT, WORK, read_json, validate, write_json
from scripts.script_input import ScriptError, SlideScript, build_scripts, load_lexicon, parse_script_file
from scripts.state import Context, StepError
from scripts.transcribe import build_captions, write_srt
from scripts.tts import Synth, TTSError, make_tts

SR = 48000
WORD_CORE = re.compile(r"^(\W*)(.*?)(\W*)$", re.UNICODE)
SENTENCE_END = re.compile(r"[.!?…]$")


def decode(path: Path) -> np.ndarray:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "f32le",
                        "-ac", "1", "-ar", str(SR), "-"], capture_output=True, timeout=600)
    if p.returncode != 0:
        raise StepError(f"не вдалося декодувати {path.name}: {p.stderr.decode()[-300:]}")
    return np.frombuffer(p.stdout, dtype=np.float32)


def spoken_form(text: str, lexicon: dict[str, str]) -> tuple[str, list[tuple[str, int, int]]]:
    """Текст для TTS і відповідність: [(слово сценарію, початок, кінець у вимовленому тексті)].

    Ключі словника можуть бути фразами («2 жовтня 2026 року»); усі слова фрази отримують
    спільний проміжок вимовленого тексту.
    """
    words = text.split()
    cores = [WORD_CORE.match(w).groups() for w in words]
    entries = sorted(((k.split(), v) for k, v in lexicon.items()), key=lambda kv: -len(kv[0]))
    out: list[str] = []
    spans: list[tuple[str, int, int]] = []
    pos, i = 0, 0
    while i < len(words):
        match = None
        for key_words, repl in entries:
            n = len(key_words)
            if i + n > len(words):
                continue
            seq = [cores[i + k][1] for k in range(n)]
            # пунктуація всередині фрази заборонена: лише на початку першого і в кінці останнього слова
            inner_ok = all(not cores[i + k][2] for k in range(n - 1)) and all(not cores[i + k][0] for k in range(1, n))
            if inner_ok and [w.lower() for w in seq] == [w.lower() for w in key_words]:
                match = (n, repl)
                break
        n, repl = match if match else (1, None)
        token = f"{cores[i][0]}{repl}{cores[i + n - 1][2]}" if repl else words[i]
        start = pos + (1 if out else 0)
        out.append(token)
        pos = start + len(token)
        for k in range(n):
            spans.append((words[i + k], start, pos))
        i += n
    return " ".join(out), spans


def word_times(spans: list[tuple[str, int, int]], synth: Synth, spoken: str,
               duration: float) -> list[dict[str, Any]]:
    """Час кожного слова сценарію (відносно початку шматка)."""
    n = len(spoken)
    aligned = (synth.chars and synth.starts and synth.ends
               and len(synth.starts) == len(synth.chars) == len(synth.ends))
    words = []
    for word, a, b in spans:
        if aligned:
            m = len(synth.chars)
            # якщо API трохи змінив текст — масштабуємо індекси
            ia = min(m - 1, round(a * m / n)) if m != n else a
            ib = min(m - 1, max(ia, round((b - 1) * m / n))) if m != n else b - 1
            start, end = synth.starts[ia], synth.ends[ib]
        else:
            start, end = duration * a / n, duration * b / n
        words.append({"word": word, "start": float(start), "end": float(max(end, start + 0.05))})
    return words


def run(job: dict[str, Any], ctx: Context) -> str:
    cfg = ctx.cfg
    tcfg = cfg["tts"]
    pauses = tcfg["pauses"]
    manifest = read_json(WORK / "slides" / "slide_manifest.json")
    visible = [s["slide"] for s in manifest["slides"] if not s["hidden"]]
    notes = {s["slide"]: s.get("notes") for s in manifest["slides"]}
    try:
        file_texts = parse_script_file(ROOT / job["inputs"]["script"]["path"]) if "script" in job["inputs"] else None
        scripts: list[SlideScript] = build_scripts(visible, file_texts, notes, pauses["marker"], pauses["empty_slide"])
        tts = make_tts(tcfg, cfg["data_policy"])
    except (ScriptError, TTSError) as e:
        raise StepError(str(e)) from e
    lexicon = load_lexicon(ROOT / tcfg.get("lexicon", "lexicon.txt"))

    # плоский список шматків з текстом для previous_text / next_text (безперервність інтонації)
    flat = [(si, pi) for si, s in enumerate(scripts) for pi, p in enumerate(s.parts) if p.text]
    order = {key: k for k, key in enumerate(flat)}
    spoken_cache: dict[tuple[int, int], tuple[str, list]] = {
        (si, pi): spoken_form(scripts[si].parts[pi].text, lexicon) for si, pi in flat}

    buf: list[np.ndarray] = [np.zeros(round(pauses["lead_in"] * SR), dtype=np.float32)]
    t = pauses["lead_in"]
    all_words: list[dict[str, Any]] = []
    transitions = []
    chars = cached = 0
    for si, script in enumerate(scripts):
        if si > 0:
            gap = pauses["between_slides"]
            buf.append(np.zeros(round(gap * SR), dtype=np.float32))
            t += gap
        # новий слайд з'являється трохи раніше за голос
        lead = min(0.5, pauses["between_slides"]) if si > 0 else 0.0
        transitions.append({"slide": script.slide, "start": 0.0 if si == 0 else round(t - lead, 3),
                            "source": "script", "confidence": 1.0})
        for pi, part in enumerate(script.parts):
            if part.text:
                spoken, spans = spoken_cache[(si, pi)]
                k = order[(si, pi)]
                prev_t = spoken_cache[flat[k - 1]][0] if k > 0 else ""
                next_t = spoken_cache[flat[k + 1]][0] if k + 1 < len(flat) else ""
                try:
                    synth = tts.synth(spoken, prev_t, next_t)
                except TTSError as e:
                    raise StepError(f"слайд {script.slide}: {e}") from e
                audio = decode(synth.audio)
                dur = len(audio) / SR
                for w in word_times(spans, synth, spoken, dur):
                    all_words.append({**w, "start": round(t + w["start"], 3), "end": round(t + w["end"], 3)})
                buf.append(audio)
                t += dur
                chars += len(spoken)
                cached += synth.cached
            if part.pause_after:
                buf.append(np.zeros(round(part.pause_after * SR), dtype=np.float32))
                t += part.pause_after
    buf.append(np.zeros(round(pauses["tail"] * SR), dtype=np.float32))
    audio = np.concatenate(buf)
    duration = len(audio) / SR

    media_dir = WORK / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    master = media_dir / "master.wav"
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(master), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())

    # transcript: речення з часом слів (prob 1.0 — текст відомий)
    segments, cur = [], []
    for w in all_words:
        cur.append(w)
        if SENTENCE_END.search(w["word"]):
            segments.append(cur)
            cur = []
    if cur:
        segments.append(cur)
    transcript = {"language": cfg["project"]["language"], "source": f"script/{tts.name}",
                  "segments": [{"start": s[0]["start"], "end": s[-1]["end"], "text": " ".join(x["word"] for x in s),
                                "prob": 1.0, "words": s} for s in segments],
                  "dropped": [], "glossary_replacements": []}
    tr_dir = WORK / "transcript"
    write_json(tr_dir / "transcript.json", transcript)
    captions = build_captions(transcript)
    write_json(tr_dir / "captions.json", captions)
    write_srt(captions, tr_dir / "review.srt")

    timings = {"duration": round(duration, 3), "transitions": transitions}
    errors = validate("slide_timings", timings)
    if errors:
        raise StepError("slide_timings: " + "; ".join(errors))
    write_json(WORK / "sync" / "slide_timings.json", timings)
    write_json(media_dir / "media.json", {
        "source": f"tts:{tts.name}", "duration": round(duration, 3), "has_video": False, "hdr": False,
        "vfr": False, "presenter": None, "master_audio": str(master.relative_to(ROOT)),
        "master_duration": round(duration, 3), "mode": "script",
        "tts": {"provider": tts.name, "model": tcfg.get("model"), "characters": chars,
                "parts": len(flat), "cached_parts": cached, "has_alignment": tts.name == "elevenlabs"},
    })
    job.setdefault("flags", {})["tts_provider"] = tts.name
    ctx.log(f"  озвучено {len(scripts)} слайдів, {len(all_words)} слів, {chars} символів "
            f"({tts.name}, з кешу {cached}/{len(flat)}), тривалість {duration:.1f} с")
    return "TRANSCRIBED"
