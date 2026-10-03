# Setup and environment repair (macOS, Apple Silicon)

## First install

```bash
brew install ffmpeg poppler whisper-cpp          # LibreOffice optional (fallback slide export)
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
mkdir -p ~/.cache/ai-video-studio/whisper
curl -L -o ~/.cache/ai-video-studio/whisper/ggml-large-v3.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin   # ~3 GB, recording mode only
mkdir -p ~/Library/Caches/ai-video-studio/remotion-deps
cp remotion/package.json remotion/package-lock.json ~/Library/Caches/ai-video-studio/remotion-deps/
(cd ~/Library/Caches/ai-video-studio/remotion-deps && npm ci)
ln -sfn ~/Library/Caches/ai-video-studio/remotion-deps/node_modules remotion/node_modules
cp .env.example .env && chmod 600 .env           # fill in keys (user does this, not you)
cp glossary.example.txt glossary.txt; cp lexicon.example.txt lexicon.txt
.venv/bin/python run.py doctor --all             # opens PowerPoint once for the export test
```

Microsoft PowerPoint renders slides most faithfully; the first run asks macOS for Automation
permission. Without PowerPoint the pipeline falls back to LibreOffice.

## Keys (`.env`)

| Key | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | Claude director (highlights, zoom, callouts). Without it: simple directing. |
| `ELEVENLABS_API_KEY` | Script mode. Key permissions: **Text to Speech** + **Voices → Read**. |
| `ELEVENLABS_VOICE_ID` | The voice to use. With `voices_read` you can list voices (`GET /v1/voices`) and pick the user's clone (`category` = `cloned` / `professional`); a professional clone is usable only after fine-tuning finished. |

Never echo key values; check presence and length only.

## iCloud (Desktop/Documents synced)

If the project sits in an iCloud-synced folder and the disk gets full, macOS evicts files
("dataless"). Sandboxed processes then read them as **empty without an error** — Remotion fails
with odd errors, Python imports empty modules.

- Detect: `find . -path ./.git -prune -o -flags +dataless -print`
- Text files: reading them with the Read tool re-downloads them.
- `.venv`: `uv pip install --reinstall --python .venv/bin/python -r requirements.txt`
- Remotion deps: `npm ci` in `~/Library/Caches/ai-video-studio/remotion-deps` (never in `remotion/`).
- `mv` of a folder containing dataless files out of iCloud hangs — reinstall instead of moving.

Layout that avoids the problem: `.venv` → `.venv.nosync/`, `work` → `work.nosync/` (symlinks;
iCloud skips `*.nosync`), `remotion/node_modules` → outside iCloud (webpack breaks on a path
segment `node_modules.nosync`). `doctor` warns if `node_modules` ends up inside iCloud.

## Known environment facts

- whisper.cpp 1.9.4: `--vad` leaves token/DTW timestamps on a silence-free timeline (up to 1.5 s
  off) — VAD stays off; hallucinations are filtered by token probability and repeats.
- Homebrew ffmpeg has no `zscale`; HDR (iPhone HLG) is tone-mapped with `avconvert`.
- PowerPoint for Mac: save to PDF only into a `POSIX file` object (a string path silently writes
  nothing); hidden slides are excluded from the PDF; the PowerPoint sandbox container is not
  accessible to other processes and not needed.
- Under heavy system load Remotion frames can time out; the render already uses a 120 s per-frame
  timeout. A failed chunk is retried by simply re-running `run.py`.
