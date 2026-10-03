"""Синтез голосу: ElevenLabs (основний, з таймкодами символів) і macOS say (локальні чернетки).

Кожен результат кешується за хешем (провайдер, голос, модель, налаштування, текст, контекст),
тож повторні прогони не витрачають кредити ElevenLabs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.common import WORK, run_cmd, write_json

CACHE = WORK / "tts_cache"
LOGS = WORK / "logs"
ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"


class TTSError(RuntimeError):
    pass


@dataclass
class Synth:
    audio: Path                                   # аудіофайл (будь-який формат, що читає ffmpeg)
    chars: list[str] | None                       # символи вимовленого тексту
    starts: list[float] | None                    # час початку кожного символу, с
    ends: list[float] | None
    cached: bool = False


def _key(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _log(entry: dict[str, Any]) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / "tts_calls.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}, ensure_ascii=False) + "\n")


class ElevenLabsTTS:
    name = "elevenlabs"

    def __init__(self, cfg: dict[str, Any]):
        if not str(cfg.get("output_format", "mp3_44100_128")).startswith("mp3_"):
            raise TTSError("tts.output_format: підтримуються лише mp3_* (кеш і декодування розраховані на MP3)")
        self.api_key = os.environ.get("ELEVENLABS_API_KEY", "")
        self.voice_id = cfg.get("voice_id") or os.environ.get("ELEVENLABS_VOICE_ID", "")
        if not self.api_key or not self.voice_id:
            raise TTSError("ElevenLabs: потрібні ELEVENLABS_API_KEY і ELEVENLABS_VOICE_ID у .env")
        self.model = cfg["model"]
        self.output_format = cfg.get("output_format", "mp3_44100_128")
        self.voice_settings = cfg.get("voice_settings", {})
        self.language_code = cfg.get("language_code")
        self.seed = cfg.get("seed")

    def synth(self, text: str, previous_text: str = "", next_text: str = "") -> Synth:
        body: dict[str, Any] = {"text": text, "model_id": self.model, "voice_settings": self.voice_settings}
        if previous_text:
            body["previous_text"] = previous_text[-1000:]
        if next_text:
            body["next_text"] = next_text[:1000]
        if self.language_code:
            body["language_code"] = self.language_code
        if self.seed is not None:
            body["seed"] = self.seed
        key = _key({"p": self.name, "voice": self.voice_id, "fmt": self.output_format, **body})
        audio, meta = CACHE / f"{key}.mp3", CACHE / f"{key}.json"
        if audio.exists() and meta.exists():
            m = json.loads(meta.read_text(encoding="utf-8"))
            return Synth(audio, m["chars"], m["starts"], m["ends"], cached=True)

        url = ELEVEN_URL.format(voice_id=self.voice_id) + f"?output_format={self.output_format}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "xi-api-key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"})
        data = self._request(req)
        CACHE.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(base64.b64decode(data["audio_base64"]))
        al = data.get("alignment") or {}
        chars = al.get("characters")
        starts = al.get("character_start_times_seconds")
        ends = al.get("character_end_times_seconds")
        write_json(meta, {"chars": chars, "starts": starts, "ends": ends, "text": text})
        _log({"provider": self.name, "model": self.model, "voice_id": self.voice_id,
              "characters": len(text), "has_alignment": bool(chars)})
        return Synth(audio, chars, starts, ends)

    @staticmethod
    def _request(req: urllib.request.Request, retries: int = 3) -> dict[str, Any]:
        last = ""
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    data = json.loads(r.read())
                if "audio_base64" not in data:
                    raise ValueError("у відповіді немає audio_base64")
                return data
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:400]
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt * 2)
                    continue
                raise TTSError(f"ElevenLabs HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                # мережа, таймаут читання, обірване з'єднання, зіпсований JSON
                last = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
                if attempt < retries:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise TTSError(f"ElevenLabs: {last}") from e
        raise TTSError(f"ElevenLabs: вичерпано спроби ({last})")


class MacSayTTS:
    """Локальний голос macOS (за замовчуванням Lesya). Без таймкодів символів — для чернеток."""
    name = "macos_say"

    def __init__(self, cfg: dict[str, Any]):
        self.voice = cfg.get("macos_voice", "Lesya")
        self.rate = cfg.get("macos_rate")

    def synth(self, text: str, previous_text: str = "", next_text: str = "") -> Synth:
        key = _key({"p": self.name, "voice": self.voice, "rate": self.rate, "text": text})
        audio = CACHE / f"{key}.aiff"
        if audio.exists():
            return Synth(audio, None, None, None, cached=True)
        CACHE.mkdir(parents=True, exist_ok=True)
        args = ["say", "-v", self.voice, "-o", str(audio)]
        if self.rate:
            args += ["-r", str(self.rate)]
        r = run_cmd(args + ["--", text], timeout=600)  # текст на «-» не сприймається як опція
        if not r.ok or not audio.exists():
            raise TTSError(f"say: {(r.stderr or r.stdout).strip()[-300:]}")
        _log({"provider": self.name, "voice": self.voice, "characters": len(text)})
        return Synth(audio, None, None, None)


def make_tts(cfg: dict[str, Any], data_policy: dict[str, Any]):
    provider = cfg["provider"]
    if provider == "elevenlabs":
        if data_policy.get("offline_only"):
            raise TTSError("data_policy.offline_only=true — хмарний синтез голосу заборонено (є --tts macos_say)")
        if not data_policy.get("allow_tts_cloud"):
            raise TTSError("data_policy.allow_tts_cloud=false — передавати сценарій в ElevenLabs заборонено")
        return ElevenLabsTTS(cfg)
    if provider == "macos_say":
        return MacSayTTS(cfg)
    raise TTSError(f"невідомий tts.provider: {provider}")
