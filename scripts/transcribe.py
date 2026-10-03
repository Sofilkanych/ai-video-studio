"""Крок Transcribe: whisper.cpp + glossary + VAD + пост-корекція + субтитри."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from scripts.common import ROOT, WORK, expand, run_cmd, write_json
from scripts.glossary_fix import fix_words, load_glossary
from scripts.state import Context, StepError

TRANSCRIPT = WORK / "transcript"
DTW_PRESET = {"large-v3": "large.v3", "large-v3-turbo": "large.v3.turbo", "medium": "medium",
              "small": "small", "base": "base"}
SPECIAL = re.compile(r"^\[_|^<\|")
SENTENCE_END = re.compile(r"[.!?…]$")


def _whisper(stt_wav: Path, cfg: dict[str, Any], prompt: str) -> dict[str, Any]:
    stt = cfg["stt"]
    models = expand(stt["models_dir"])
    model = models / f"ggml-{stt['model']}.bin"
    if not model.exists():
        raise StepError(f"немає моделі {model}")
    out = TRANSCRIPT / "raw"
    args = [stt.get("binary", "whisper-cli"), "-m", str(model), "-f", str(stt_wav),
            "-l", cfg["project"]["language"], "-t", "8", "-ojf", "-of", str(out), "-np"]
    if stt.get("dtw") and stt["model"] in DTW_PRESET:
        args += ["--dtw", DTW_PRESET[stt["model"]], "-nfa"]  # DTW несумісний з flash attention
    if stt.get("vad"):
        vad = models / "ggml-silero-v5.1.2.bin"
        if vad.exists():
            args += ["--vad", "-vm", str(vad)]
    if prompt:
        args += ["--prompt", prompt]
        if stt.get("carry_initial_prompt"):
            args.append("--carry-initial-prompt")
    r = run_cmd(args, timeout=4 * 3600)
    raw = out.with_suffix(".json")
    if not r.ok or not raw.exists():
        raise StepError(f"whisper-cli: {(r.stderr or r.stdout).strip()[-500:]}")
    # whisper.cpp може розрізати багатобайтні символи між токенами
    return json.loads(raw.read_bytes().decode("utf-8", errors="replace"))


def _token_times(seg: dict[str, Any]) -> list[tuple[str, float]]:
    """(текст токена, час у секундах) для звичайних токенів; DTW, якщо є."""
    out = []
    for tok in seg.get("tokens", []):
        text = tok.get("text", "")
        if not text or SPECIAL.match(text):
            continue
        t = tok.get("t_dtw", -1)
        sec = t / 100 if isinstance(t, (int, float)) and t >= 0 else tok["offsets"]["from"] / 1000
        out.append((text, sec))
    return out


def words_from_segment(seg: dict[str, Any]) -> list[dict[str, Any]]:
    """Слова з тексту сегмента (коректний UTF-8), час — з токенів за позицією символу."""
    text = seg["text"].strip()
    start, end = seg["offsets"]["from"] / 1000, seg["offsets"]["to"] / 1000
    raw_words = text.split()
    if not raw_words:
        return []
    tokens = _token_times(seg)
    tok_chars = [len(t.strip()) or 1 for t, _ in tokens]
    total_tok = sum(tok_chars) or 1
    total_txt = sum(len(w) for w in raw_words) or 1
    cum_tok, acc = [], 0
    for c in tok_chars:
        cum_tok.append(acc)
        acc += c

    def time_at(char_pos: int) -> float:
        if not tokens:
            return start + (end - start) * char_pos / total_txt
        target = char_pos / total_txt * total_tok
        idx = max(i for i, c in enumerate(cum_tok) if c <= target) if cum_tok else 0
        return min(max(tokens[idx][1], start), end)

    words, pos = [], 0
    for w in raw_words:
        words.append({"word": w, "start": round(time_at(pos), 3)})
        pos += len(w)
    for i, w in enumerate(words):
        nxt = words[i + 1]["start"] if i + 1 < len(words) else end
        w["start"] = max(w["start"], words[i - 1]["start"] if i else start)  # монотонність
        w["end"] = round(max(nxt, w["start"] + 0.05), 3)
    return words


def _segment_prob(seg: dict[str, Any]) -> float:
    ps = [t.get("p", 1.0) for t in seg.get("tokens", []) if t.get("text") and not SPECIAL.match(t["text"])]
    return sum(ps) / len(ps) if ps else 0.0


def build_transcript(raw: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    segments, dropped, prev_text, repeats = [], [], None, 0
    for seg in raw.get("transcription", []):
        text = seg.get("text", "").strip()
        if not text:
            continue
        prob = _segment_prob(seg)
        repeats = repeats + 1 if text == prev_text else 0
        prev_text = text
        # проти галюцинацій: дуже низька впевненість або зациклений повтор
        if prob < 0.35 or repeats >= 2:
            dropped.append({"text": text, "start": seg["offsets"]["from"] / 1000, "prob": round(prob, 3)})
            continue
        segments.append({"start": seg["offsets"]["from"] / 1000, "end": seg["offsets"]["to"] / 1000,
                         "text": text, "prob": round(prob, 3), "words": words_from_segment(seg)})
    all_words = [w for s in segments for w in s["words"]]
    replacements = fix_words(all_words, terms)
    for s in segments:
        s["text"] = " ".join(w["word"] for w in s["words"])
    return {"language": raw.get("result", {}).get("language", "uk"), "segments": segments,
            "dropped": dropped, "glossary_replacements": replacements}


def build_captions(transcript: dict[str, Any], max_chars: int = 42, max_lines: int = 2,
                   max_dur: float = 6.0, pause: float = 0.6) -> list[dict[str, Any]]:
    """Субтитри: ≤2 рядки по ≤42 символи, ≤6 с; розрив на кінці речення або паузі."""
    words = [w for s in transcript["segments"] for w in s["words"]]
    caps, cur, lines, line = [], [], [], ""

    def flush():
        nonlocal cur, lines, line
        if cur:
            all_lines = lines + ([line] if line else [])
            caps.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": "\n".join(all_lines)})
        cur, lines, line = [], [], ""

    for i, w in enumerate(words):
        if cur and (w["start"] - cur[-1]["end"] > pause or w["end"] - cur[0]["start"] > max_dur):
            flush()
        candidate = f"{line} {w['word']}".strip()
        if len(candidate) > max_chars and line:
            if len(lines) + 1 >= max_lines:
                flush()
                candidate = w["word"]
            else:
                lines.append(line)
                candidate = w["word"]
        line = candidate
        cur.append(w)
        if SENTENCE_END.search(w["word"]) and len(" ".join(lines + [line])) > max_chars * 0.6:
            flush()
    flush()
    for a, b in zip(caps, caps[1:]):  # без накладання
        a["end"] = min(a["end"], b["start"])
    return caps


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(captions: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{c['text']}\n\n"
                            for i, c in enumerate(captions, 1)), encoding="utf-8")


def read_srt(path: Path) -> list[dict[str, Any]]:
    """Зворотне читання review.srt після ручних правок (checkpoint транскрипту)."""
    caps = []
    for block in path.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        a, b = (x.strip() for x in lines[1].split("-->"))

        def sec(s: str) -> float:
            h, m, rest = s.split(":")
            s2, ms = rest.split(",")
            return int(h) * 3600 + int(m) * 60 + int(s2) + int(ms) / 1000
        caps.append({"start": sec(a), "end": sec(b), "text": "\n".join(lines[2:])})
    return caps


def run(job: dict[str, Any], ctx: Context) -> str:
    cfg = ctx.cfg
    TRANSCRIPT.mkdir(parents=True, exist_ok=True)
    terms = load_glossary(ROOT / cfg["stt"]["glossary"])
    media = json.loads((WORK / "media" / "media.json").read_text(encoding="utf-8"))
    raw = _whisper(ROOT / media["stt_audio"], cfg, ", ".join(terms))
    transcript = build_transcript(raw, terms)
    if not transcript["segments"]:
        raise StepError("транскрипт порожній — перевірте аудіо і мову")
    write_json(TRANSCRIPT / "transcript.json", transcript)
    captions = build_captions(transcript)
    write_json(TRANSCRIPT / "captions.json", captions)
    write_srt(captions, TRANSCRIPT / "review.srt")
    n_words = sum(len(s["words"]) for s in transcript["segments"])
    ctx.log(f"  сегментів {len(transcript['segments'])}, слів {n_words}, субтитрів {len(captions)}, "
            f"замін за словником {len(transcript['glossary_replacements'])}, "
            f"відкинуто {len(transcript['dropped'])}")
    return "TRANSCRIBED"
