"""Пост-корекція транскрипту за glossary.txt.

Замінює лише слова/словосполучення, схожі на терміни словника; нічого не перефразовує.
Кожна заміна логується.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

WORD_RE = re.compile(r"^(\W*)(.*?)(\W*)$", re.UNICODE)
MIN_RATIO = 0.84

# Українська → латиниця (спрощено): STT часто пише абревіатури латиницею (АБК → ABK)
_UK_LAT = {"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh",
           "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
           "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
           "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia", "'": "", "’": ""}


def translit(text: str) -> str:
    return "".join(_UK_LAT.get(ch, ch) for ch in text.lower())


def _is_latin(text: str) -> bool:
    return any("a" <= ch <= "z" for ch in text.lower())


def similarity(a: str, b: str) -> float:
    """Схожість; транслітерація — лише коли написання в різних абетках (ABK ↔ АБК)."""
    a, b = a.lower(), b.lower()
    if _is_latin(a) != _is_latin(b):
        return difflib.SequenceMatcher(None, translit(a), translit(b)).ratio()
    return difflib.SequenceMatcher(None, a, b).ratio()


# Типові закінчення відмінків: різниця лише в них — це словоформа, а не помилка STT
_ENDINGS = {"", "а", "я", "у", "ю", "і", "ї", "и", "е", "є", "о", "ом", "ем", "єм", "ою", "ею", "єю",
            "ові", "еві", "єві", "ів", "їв", "ах", "ях", "ам", "ям", "ами", "ями", "ові", "ой"}


def is_inflection(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    if _is_latin(a) != _is_latin(b):
        return False
    k = 0
    while k < min(len(a), len(b)) and a[k] == b[k]:
        k += 1
    return k >= 3 and a[k:] in _ENDINGS and b[k:] in _ENDINGS


def _threshold(a: str, b: str) -> float:
    # в одній абетці відмінки («Лесі» ~ «Леся») не мають замінюватися — поріг вищий
    return MIN_RATIO if _is_latin(a) != _is_latin(b) else 0.9


def load_glossary(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _core(word: str) -> tuple[str, str, str]:
    m = WORD_RE.match(word)
    return (m.group(1), m.group(2), m.group(3)) if m else ("", word, "")


def fix_words(words: list[dict[str, Any]], terms: list[str]) -> list[dict[str, Any]]:
    """Змінює words на місці; повертає лог замін [{at, from, to, ratio}]."""
    log: list[dict[str, Any]] = []
    by_len: dict[int, list[str]] = {}
    for t in terms:
        by_len.setdefault(len(t.split()), []).append(t)
    i = 0
    while i < len(words):
        replaced = False
        for n in sorted(by_len, reverse=True):
            if i + n > len(words):
                continue
            chunk = words[i:i + n]
            pre, _, _ = _core(chunk[0]["word"])
            _, _, post = _core(chunk[-1]["word"])
            phrase = " ".join(_core(w["word"])[1] for w in chunk)
            if len(phrase) < 3:
                continue
            for term in by_len[n]:
                if phrase == term or is_inflection(phrase, term):
                    break
                ratio = similarity(phrase, term)
                if ratio >= _threshold(phrase, term):
                    term_words = term.split()
                    for k, w in enumerate(chunk):
                        w["word"] = (pre if k == 0 else "") + term_words[k] + (post if k == n - 1 else "")
                    log.append({"at": chunk[0]["start"], "from": phrase, "to": term, "ratio": round(ratio, 3)})
                    replaced = True
                    break
            if replaced:
                i += n
                break
        if not replaced:
            i += 1
    return log
