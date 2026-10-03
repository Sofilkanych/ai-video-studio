---
name: video-lecture
description: >-
  Turns a presentation (PPTX/PDF) into a finished narrated video lecture (MP4 + SRT) with the
  ai-video-studio pipeline: either from a recorded talk (presenter video/voice) or from a written
  per-slide script voiced by ElevenLabs (incl. the user's cloned voice), with automatic slide
  sync, Claude-directed highlights/zoom/callouts, Remotion rendering and automatic QA. Use this
  skill whenever the user wants to make a video, video lecture, video presentation, webinar
  recording, course video or narrated slides from a deck, script, speaker notes or a recording —
  in any language, e.g. «зроби відеолекцію з презентації», «озвуч слайди моїм голосом»,
  «змонтуй запис виступу зі слайдами», «перерендер без субтитрів», «виправ вимову в лекції» —
  even if they don't name the pipeline. Also use it to edit or re-render a lecture made earlier.
---

# Video lecture (ai-video-studio)

The pipeline lives in the repository that contains this skill. Find its root first — the skill
folder may be a symlink:

```bash
ROOT="$(cd "$(dirname "$(realpath "<this skill's base directory>/SKILL.md")")/../.." && pwd)"
cd "$ROOT" && .venv/bin/python run.py status
```

`run.py` is a resumable state machine (ingest → transcribe/synthesize → sync → direct →
assets → preview → QA → final → QA → finalize). Re-running continues from the current state;
every step caches its outputs, so repeated runs don't re-spend ElevenLabs credits or Claude tokens.
`CLAUDE.md` in the repo holds the full engineering rules — read it before changing code.

## 1. Preflight

```bash
.venv/bin/python run.py doctor            # 0 fail expected; warnings explain themselves
find . -path ./.git -prune -o -flags +dataless -print | head   # macOS iCloud eviction check
```

If `.venv`, `remotion/node_modules` or the whisper model are missing, follow
`references/setup.md`. If files show up as `dataless` (iCloud "Optimize Mac Storage"), they read as
empty without an error — restore them before running anything (see the same file).

## 2. Pick the mode from what the user has

| User has | Mode | Inputs |
|---|---|---|
| Presentation + recording of the talk (video or audio) | recording | `presentation.pptx` + `presenter.mp4` / `voice.wav` |
| Presentation + text for each slide | script | `presentation.pptx` + `script.md`, or text in PPTX speaker notes |

Don't copy the user's files around: point the run at their folder with `--input DIR`. The folder
must contain exactly one presentation and either one recording or a script (both at once is an
error by design). A Word script whose text matches the speaker notes needs no conversion — check
that first; otherwise convert it to `script.md` (`## Слайд N` headings, see
`references/script-and-voice.md`).

## 3. Script mode: protect the user's money and pronunciation

Voice synthesis is paid. Before the full run:

1. Count characters of the narration and check the ElevenLabs balance
   (`GET /v1/user/subscription` with `xi-api-key`; never print the key). Tell the user the cost
   share and get a clear yes.
2. Scan the text for numbers, dates, versions and Latin abbreviations — the things TTS most often
   misreads. Offer a short probe: synthesize only those sentences, transcribe them with whisper and
   let the user listen (`references/script-and-voice.md` has the procedure).
3. Put fixes into `lexicon.txt` (spoken form only; captions keep the script text). Dates and numbers
   are safest written out in words with the correct case.

`--tts macos_say` gives a free draft with the local voice when the user only wants to check
timing and layout.

## 4. Run

A 20-minute lecture takes ~20–40 min end to end, longer than a foreground tool call, so start it
detached and watch the log:

```bash
nohup .venv/bin/python -u run.py --new --input "<folder>" > work/logs/run.log 2>&1 < /dev/null &
```

Monitor `work/logs/run.log` for step lines (`→ step`), `QA …`, `✓ Готово`, `✗`, `■` and Python
tracebacks. Report progress at milestones, not every line.

## 5. Verify before announcing

- `output/final.mp4` exists; `ffprobe` shows 1920×1080 h264 + aac with matching durations.
- `output/report.json`: `qa_final.status == "pass"`, which director ran (`anthropic_api` or
  `fallback`), TTS provider, LLM token usage.
- Look at a few frames (`ffmpeg -ss T -frames:v 1`) — especially around highlights.
- Script mode: spot-check pronunciation of risky words by transcribing short audio windows around
  them (word times are in `work/transcript/transcript.json`).

Then open the video for the user (`open output/final.mp4`) and summarize: duration, QA, what the
director added, costs (characters, tokens), and anything that still needs their ears.

## 6. Edits after watching

Most feedback maps to a small re-render that reuses the voice and the director's plan — see
`references/editing.md` (captions on/off, highlight style, pronunciation fixes, re-directing a
lecture, versioned timelines). Prefer creating a new timeline version over re-running `direct`,
which calls Claude again and produces a different plan.

## Guardrails

- Never write into `input/` unless the user asks; never print or commit API keys (`.env`).
- Company material goes to external services only as `config.yaml → data_policy` allows
  (Claude API, ElevenLabs). Mention it when sending a new kind of material.
- Don't run `npm install`/`npm ci` inside `remotion/` — `node_modules` there is a symlink to a
  folder outside iCloud (`references/setup.md`).
