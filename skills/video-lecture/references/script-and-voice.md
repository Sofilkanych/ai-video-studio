# Script mode: text, voice and pronunciation

## Where the text comes from

1. `script.md` / `script.txt` in the input folder, or
2. PowerPoint speaker notes (used automatically when there is no script file and no recording).

`script.md` format:

```markdown
## Слайд 1
Вітаю! Сьогодні про результати компанії АБК.

## Слайд 2
Тут таблиця результатів. [пауза]
Базовий сценарій дає виручку сто двадцять мільйонів. [пауза 2] Далі — ризики.
```

- Slide numbers are PowerPoint numbers; hidden slides are skipped and must not be described.
- Headings: `## Слайд N` (anything after it) or `Слайд N:` / `Слайд N.` with a separator.
  A sentence starting "Слайд 3 показує…" is body text, not a heading.
- `[пауза]` = 1 s, `[пауза 2.5]` = 2.5 s; a slide without text is shown for 3 s.
- `<!-- comments -->` and markdown emphasis are not spoken. Long text is split under the TTS limit.

### Word documents

If the user gives a `.docx`, first compare it with the speaker notes (they are often identical —
then nothing needs converting). Read the docx without extra dependencies:

```python
import re, zipfile
xml = zipfile.ZipFile(path).read("word/document.xml").decode()
paras = ["".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
         for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)]
```

If conversion is needed, write `script.md` next to the user's files (ask before writing there),
dropping per-slide title lines that aren't meant to be spoken.

## Text → deck (only the lecture text is given)

- `compose` sends the text to Claude (structured output, validated: slide count ≈ words/130,
  first slide `title`, last `closing`, field lengths, narration 60–200 words per slide, numbers in
  words, narration covering ≥ 60 % of the source). Result: `work/compose/deck_plan.json` +
  `work/compose/<job_id>.pptx` with the narration in speaker notes.
- Before the paid voice step it is worth looking at the deck: run until `INGESTED`, open the PNGs in
  `work/slides/`, or the PPTX. Edit `deck_plan.json` by hand and `run.py --redo ingest` to rebuild
  without a new Claude call (the plan is cached by the text's hash).
- Theme: `config.yaml → compose.theme` (`forest`, `neutral`); footer: `compose.footer`.

## Voice

- Provider `elevenlabs` (default; model `eleven_multilingual_v2` — Ukrainian, character-level
  timestamps, `previous_text`/`next_text` continuity). `--tts macos_say` = free local draft.
- Each text part is cached in `work/tts_cache/` by text + voice + settings + neighbours' text.
  Editing one slide re-synthesizes it and its two neighbours; everything else is free.
- Usage log: `work/logs/tts_calls.jsonl` (characters per call).

## Pronunciation probe (before a full paid run)

1. Collect sentences from the narration containing digits, dates, versions (`GPT-5.6`) or Latin
   abbreviations (`xAI`, `HR`).
2. Synthesize them through `scripts.tts.make_tts(cfg["tts"], cfg["data_policy"]).synth(text)`
   (cached, logged), copy the MP3s to `work/review/tts_probe/`.
3. Transcribe each with `whisper-cli -m ~/.cache/ai-video-studio/whisper/ggml-large-v3.bin -l uk -nt -np`
   and compare with the text. Whisper writes numbers as digits, so it can't judge grammatical case —
   that needs the user's ears.
4. Concatenate the clips with 1 s gaps into `output/proba_golosu.mp3` and open it for the user.

Typical findings (Ukrainian, eleven_multilingual_v2): dates like «2 жовтня 2026 року» may come
out wrong → write them in words; `xAI` is unstable → «ікс-ей-ай»; `Claude` → «Клод».

## lexicon.txt

```
2 жовтня 2026 року = друге жовтня дві тисячі двадцять шостого року
xAI = ікс-ей-ай
Claude = Клод
```

Keys are words or phrases without inner punctuation, case-insensitive. Only the spoken text
changes; captions, transcript and highlights keep the script wording. After changing the lexicon,
re-run from synthesis: `run.py --redo transcribe` (only changed parts are paid for).
