# AI Video Studio

Презентація + запис ведучого **або** текст для кожного слайда → готова відеолекція (слайди, голос, акценти, QA).

Статус: **Фаза 1 (MVP)** — повний цикл input → `output/final.mp4` + `final.srt` + `report.json` працює на тестовій фікстурі.

## iCloud

Папка проєкту лежить на синхронізованому з iCloud «Робочому столі». Щоб iCloud не синхронізував і не вивантажував важкі файли:

| У проєкті | Насправді лежить | Чому так |
|---|---|---|
| `.venv` → | `.venv.nosync/` | суфікс `.nosync` iCloud не синхронізує |
| `work` → | `work.nosync/` | те саме (кеш, рендери) |
| `remotion/node_modules` → | `~/Library/Caches/ai-video-studio/remotion-deps/node_modules` | webpack не працює зі шляхом `node_modules.nosync`, тому — поза iCloud |

**Не запускайте `npm ci` / `npm install` у `remotion/`** — це замінить посилання на справжню папку в iCloud. Оновлення залежностей:

```bash
cp remotion/package.json remotion/package-lock.json ~/Library/Caches/ai-video-studio/remotion-deps/
(cd ~/Library/Caches/ai-video-studio/remotion-deps && npm ci)
```

`python run.py doctor` попереджає, якщо `node_modules` опинився в iCloud.

## Встановлення (macOS, Apple Silicon)

Системні залежності: `ffmpeg`, `poppler`, `whisper-cpp` (Homebrew), Node.js, Microsoft PowerPoint (LibreOffice — fallback).

```bash
brew install ffmpeg poppler whisper-cpp
# модель STT (~3 ГБ)
mkdir -p ~/.cache/ai-video-studio/whisper && curl -L -o ~/.cache/ai-video-studio/whisper/ggml-large-v3.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt
mkdir -p ~/Library/Caches/ai-video-studio/remotion-deps && cp remotion/package*.json ~/Library/Caches/ai-video-studio/remotion-deps/
(cd ~/Library/Caches/ai-video-studio/remotion-deps && npm ci)
ln -sfn ~/Library/Caches/ai-video-studio/remotion-deps/node_modules remotion/node_modules
cp .env.example .env   # вписати ключі (див. нижче)
cp glossary.example.txt glossary.txt; cp lexicon.example.txt lexicon.txt
```

## Навичка для Claude Code

У `skills/video-lecture/` — навичка, з якою Claude Code сам веде весь процес: перевіряє середовище, обирає режим, оцінює вартість озвучки, перевіряє вимову, запускає рендер і перевіряє результат.

```bash
mkdir -p ~/.claude/skills && ln -sfn "$(pwd)/skills/video-lecture" ~/.claude/skills/video-lecture
```

Далі в Claude Code достатньо сказати, наприклад: «зроби відеолекцію з презентації в папці ~/Lectures/intro моїм голосом».

## Два режими

| Режим | Що покласти в `input/` | Звідки голос |
|---|---|---|
| **Запис** | `presentation.pptx` + `presenter.mp4` (або `voice.wav`) | ваш запис; розпізнавання whisper.cpp |
| **Сценарій** | `presentation.pptx` + `script.md` (або лише PPTX з текстом у нотатках доповідача) | ElevenLabs (ваш клонований голос) |

Формат `script.md`:

```markdown
## Слайд 1
Вітаю! Сьогодні про результати компанії АБК.

## Слайд 2
Тут таблиця результатів. [пауза]
Базовий сценарій дає виручку сто двадцять мільйонів. [пауза 2] Далі — ризики.
```

- Номери — як у PowerPoint (приховані слайди пропускаються, їх не описують).
- `[пауза]` = 1 с, `[пауза 2]` = 2 с. Слайд без тексту показується 3 с.
- Числа краще писати словами («сто двадцять мільйонів») — так синтезатор прочитає їх у правильному відмінку.
- `lexicon.txt` — як вимовляти абревіатури (`АБК = а-бе-ка`); субтитри лишаються як у сценарії.
- Озвучення кешується: правка одного шматка перегенерує його і двох сусідів (вони отримують новий контекст інтонації), решта — з кешу.
- Слайд, якого немає в `script.md`, показується 3 с без голосу; заголовок рядком — `## Слайд N` або `Слайд N:`.
- `lexicon.txt` працює для окремих слів (без розділових знаків у ключі); `<!-- коментарі -->` не озвучуються.
- Перевірка транскрипту (`--approve-transcript`) у режимі сценарію не потрібна і пропускається.
- Чернетка без витрат кредитів: `python run.py --tts macos_say` (голос Lesya).

Для ElevenLabs у `.env`: `ELEVENLABS_API_KEY` і `ELEVENLABS_VOICE_ID` (ID вашого клонованого голосу).

## Перевірка середовища

```bash
python run.py doctor
python run.py doctor --powerpoint   # відкриє PowerPoint і експортує тестову презентацію
```

Звіт зберігається у `work/doctor/env_report.json`.

## Запуск

```bash
# покласти файли в input/: presentation.pptx (або .pdf) + presenter.mp4 (або voice.wav)
python run.py           # створює/продовжує job
python run.py status
python run.py --input tests/fixtures/generated/job_input   # тестова фікстура
python run.py --redo direct                                # переробити режисуру й рендер
```

## Етапи

| Стан | Крок | Що робить |
|---|---|---|
| NEW → INGESTED | ingest | HDR→SDR (avconvert/zscale), CFR, master WAV з вирівнюванням, PowerPoint→PDF→PNG, manifest, маски вмісту, пошук обличчя |
| → TRANSCRIBED | transcribe | запис: whisper.cpp large-v3 + DTW, словник, субтитри; сценарій: синтез голосу ElevenLabs по слайдах з таймкодами слів |
| → SLIDES_SYNCED | sync_slides | запис: збіг кадрів > «наступний слайд» > текстове вирівнювання; сценарій: точно з синтезу |
| → DIRECTED | direct | Claude (structured output + перевірки) або проста режисура |
| → ASSETS_READY | build_assets | props для Remotion з геометрією сцен |
| → PREVIEW_RENDERED | render_preview | 720p, чанки з кешем, аудіо −16 LUFS |
| → QA_PASSED / QA_FAILED | qa_preview | технічний (відносно timeline) + геометричний QA |
| QA_FAILED → PATCHED | repair / safe_fallback | детерміноване виправлення перекриттів, нова версія timeline |
| → FINAL_RENDERED → FINAL_QA_PASSED → DONE | render_final, qa_final, finalize | 1080p, QA, `output/` |

## Структура

| Шлях | Призначення |
|---|---|
| `run.py` | Оркестратор, state machine |
| `config.yaml` | Профілі, STT, бюджет, data policy |
| `glossary.txt` | Терміни для STT |
| `scripts/` | Кроки pipeline і перевірки |
| `schemas/` | JSON Schema для всіх артефактів |
| `remotion/` | Композиція відео |
| `input/` `work/` `output/` | Оригінали / кеш / результат |

## Ліцензії

Remotion безкоштовний лише на етапі оцінки; для робочого використання компанією потрібна Company License — див. [remotion.dev/license](https://www.remotion.dev/license).
