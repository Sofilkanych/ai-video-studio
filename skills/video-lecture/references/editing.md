# Editing a finished lecture

Every change creates a new timeline version (`work/timeline/timeline_vNNN.json`); unchanged
render chunks and all voice audio come from cache. `run.py status` shows the current version.

## Re-render with the same plan (no new Claude/ElevenLabs cost)

Use this for visual changes that don't need a new directing plan:

```python
import copy
from scripts.common import read_json, write_json, WORK
from scripts.state import load_job, save_job, transition
job = load_job(); v = job["timeline_version"]
tl = copy.deepcopy(read_json(WORK / "timeline" / f"timeline_{v}.json"))
tl["version"] = f"v{int(v[1:]) + 1:03d}"
# ... change tl here (e.g. tl["captions"]["enabled"] = False) ...
write_json(WORK / "timeline" / f"timeline_{tl['version']}.json", tl)
job["timeline_version"] = tl["version"]
transition(job, "DIRECTED", note="<what the user asked for>")
save_job(job)
```

Then run `run.py` (detached for long lectures). It rebuilds props, renders, runs QA, finalizes.

## Common requests

| Request | Change |
|---|---|
| No burned-in subtitles | `config.yaml → captions.burn_in: false` for future runs; for the current one set `tl["captions"]["enabled"] = False` in a new version. The slide grows into the freed space; `output/final.srt` is still produced. |
| Subtitles back | `burn_in: true` / `enabled: True`. |
| Highlight frame touches text / looks wrong | Styling lives in `remotion/src/components/SlideScene.tsx` (`Overlay`, `highlight`). Code changes invalidate the chunk cache automatically. |
| Too many highlights, or a different directing style | Edit `SYSTEM_PROMPT` limits in `scripts/direct.py`, then `run.py --redo direct` (new Claude call, new plan). Or remove actions from the timeline version directly. |
| Wrong pronunciation | `lexicon.txt`, then `run.py --redo transcribe` (re-synthesizes only changed parts). |
| Wrong slide timing (recording mode) | Inspect `work/sync/slide_timings.json`; fix and `run.py --redo direct`. |
| Different voice | `ELEVENLABS_VOICE_ID` in `.env`, then `--redo transcribe` (all parts are paid again). |
| Free draft | `--tts macos_say`. |

## QA failures

`work/qa/qa_<stage>_<version>.json` lists issues. Geometric overlaps (presenter/captions over
slide content) are repaired automatically up to `qa.max_repair_loops`, then a safe simple layout
is used. Technical issues (duration, loudness, black/silent segments) stop the job with
`job.error` — read the report, fix the cause, re-run.
