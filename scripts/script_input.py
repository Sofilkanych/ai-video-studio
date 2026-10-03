"""Сценарій лекції по слайдах (режим «script»): текст для озвучення кожного слайда.

Джерела (за пріоритетом):
1. Файл input/script.md (або .txt) з заголовками «## Слайд N» / «Слайд N:».
2. Нотатки доповідача у PPTX (slide_manifest.json → notes).

Позначки в тексті:
  [пауза]      — пауза за замовчуванням (config tts.pauses.marker)
  [пауза 2]    — пауза 2 секунди (дробові теж: [пауза 1.5])
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Заголовок: «## Слайд 3» (будь-що після) або «Слайд 3:» / «Слайд 3.» з роздільником.
# Рядок тексту «Слайд 3 показує графік» заголовком НЕ є.
HEADING = re.compile(r"^\s*(?:#{1,6}\s*(?:слайд|slide)\s*(\d+)\s*[:.\-–—]?\s*(.*)"
                     r"|(?:слайд|slide)\s*(\d+)\s*[:.\-–—]\s*(.*)|(?:слайд|slide)\s*(\d+)\s*)$", re.IGNORECASE)
MAX_TTS_CHARS = 4500  # з запасом від ліміту 10 000 символів eleven_multilingual_v2
PAUSE = re.compile(r"\[\s*пауза(?:\s+(\d+(?:[.,]\d+)?))?\s*\]", re.IGNORECASE)
LEXICON_LINE = re.compile(r"^\s*(.+?)\s*=\s*(.+?)\s*$")


class ScriptError(ValueError):
    pass


@dataclass
class Part:
    """Шматок тексту між паузами: text — для субтитрів, pause_after — секунд тиші після нього."""
    text: str
    pause_after: float = 0.0


@dataclass
class SlideScript:
    slide: int
    parts: list[Part] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(p.text for p in self.parts if p.text)


def parse_script_file(path: Path) -> dict[int, str]:
    """{номер слайда: сирий текст}. Текст до першого заголовка ігнорується."""
    texts: dict[int, list[str]] = {}
    current: int | None = None
    raw = path.read_text(encoding="utf-8-sig")  # BOM від Word/Блокнота
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)  # коментарі автора не озвучуються
    for line in raw.splitlines():
        m = HEADING.match(line)
        if m:
            num = next(g for g in (m.group(1), m.group(3), m.group(5)) if g)
            rest = (m.group(2) or m.group(4) or "").strip()
            current = int(num)
            if current in texts:
                raise ScriptError(f"слайд {current} описано двічі")
            texts[current] = [rest] if rest else []
            continue
        if current is not None:
            texts[current].append(line)
    if not texts:
        raise ScriptError(f"{path.name}: не знайдено жодного заголовка «Слайд N»")
    return {k: "\n".join(v).strip() for k, v in texts.items()}


def split_parts(raw: str, default_pause: float) -> list[Part]:
    parts: list[Part] = []
    pos = 0
    for m in PAUSE.finditer(raw):
        text = _clean(raw[pos:m.start()])
        dur = float(m.group(1).replace(",", ".")) if m.group(1) else default_pause
        if text:
            parts.append(Part(text, dur))
        elif parts:
            parts[-1].pause_after += dur
        else:
            parts.append(Part("", dur))  # пауза на початку слайда
        pos = m.end()
    tail = _clean(raw[pos:])
    if tail:
        parts.append(Part(tail))
    return [q for p in parts for q in _split_long(p)]


def _split_long(part: Part) -> list[Part]:
    """Довгий шматок → кілька за реченнями (≤ MAX_TTS_CHARS), без пауз між ними."""
    if len(part.text) <= MAX_TTS_CHARS:
        return [part]
    sentences = re.split(r"(?<=[.!?…])\s+", part.text)
    chunks, cur = [], ""
    for s in sentences:
        while len(s) > MAX_TTS_CHARS:  # надзвичайно довге речення — ріжемо по словах
            cut = s.rfind(" ", 0, MAX_TTS_CHARS)
            cut = cut if cut > 0 else MAX_TTS_CHARS
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(s[:cut].strip())
            s = s[cut:].strip()
        if cur and len(cur) + 1 + len(s) > MAX_TTS_CHARS:
            chunks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return [Part(c) for c in chunks[:-1]] + [Part(chunks[-1], part.pause_after)]


def _clean(text: str) -> str:
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)  # маркери списків markdown
    text = re.sub(r"[*_`]+", "", text)                              # розмітка markdown
    return re.sub(r"\s+", " ", text).strip()


def build_scripts(visible_slides: list[int], file_texts: dict[int, str] | None,
                  notes: dict[int, str | None], default_pause: float,
                  empty_slide_pause: float) -> list[SlideScript]:
    """Сценарій для кожного видимого слайда; порожній слайд — лише пауза."""
    if file_texts is not None:
        unknown = sorted(set(file_texts) - set(visible_slides))
        if unknown:
            raise ScriptError(f"у сценарії є слайди, яких немає серед видимих: {unknown} "
                              f"(видимі: {visible_slides})")
    out = []
    for n in visible_slides:
        raw = (file_texts.get(n) if file_texts is not None else notes.get(n)) or ""
        parts = split_parts(raw, default_pause)
        if not any(p.text for p in parts):
            parts = [Part("", max(empty_slide_pause, sum(p.pause_after for p in parts)))]
        out.append(SlideScript(n, parts))
    if not any(s.text for s in out):
        raise ScriptError("сценарій порожній: немає тексту для жодного слайда")
    return out


def load_lexicon(path: Path) -> dict[str, str]:
    """Словник вимови: «АБК = а-бе-ка». Ключ — слово як у сценарії (без розділових знаків)."""
    if not path.exists():
        return {}
    lex = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = LEXICON_LINE.match(line)
        if m:
            lex[m.group(1)] = m.group(2)
    return lex
