"""ffprobe/ffmpeg-утиліти."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from scripts.common import run_cmd

HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}


class MediaError(RuntimeError):
    pass


@dataclass
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str
    start_time: float
    duration: float | None
    raw: dict[str, Any]


@dataclass
class MediaInfo:
    path: Path
    duration: float
    video: StreamInfo | None
    audio: StreamInfo | None

    @property
    def is_hdr(self) -> bool:
        return bool(self.video and self.video.raw.get("color_transfer") in HDR_TRANSFERS)

    @property
    def fps(self) -> float | None:
        if not self.video:
            return None
        rate = self.video.raw.get("avg_frame_rate") or self.video.raw.get("r_frame_rate") or "0/1"
        f = Fraction(rate) if rate != "0/0" else Fraction(0)
        return float(f) if f else None

    @property
    def is_vfr(self) -> bool:
        if not self.video:
            return False
        r, a = self.video.raw.get("r_frame_rate"), self.video.raw.get("avg_frame_rate")
        return bool(r and a and r != a)

    @property
    def size(self) -> tuple[int, int] | None:
        if not self.video:
            return None
        return int(self.video.raw["width"]), int(self.video.raw["height"])


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def probe(path: Path) -> MediaInfo:
    r = run_cmd(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                 "-show_streams", str(path)], timeout=60)
    if not r.ok:
        raise MediaError(f"ffprobe {path.name}: {r.stderr.strip()}")
    data = json.loads(r.stdout)
    video = audio = None
    for s in data.get("streams", []):
        info = StreamInfo(s["index"], s.get("codec_type", ""), s.get("codec_name", ""),
                          _f(s.get("start_time")) or 0.0, _f(s.get("duration")), s)
        if info.codec_type == "video" and video is None and not s.get("disposition", {}).get("attached_pic"):
            video = info
        elif info.codec_type == "audio" and audio is None:
            audio = info
    duration = _f(data.get("format", {}).get("duration")) or 0.0
    return MediaInfo(path, duration, video, audio)


def ffmpeg(args: list[str], timeout: float = 3600) -> None:
    r = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], timeout=timeout)
    if not r.ok:
        raise MediaError(f"ffmpeg {' '.join(args[:6])}…: {(r.stderr or 'timeout').strip()[-500:]}")


def count_packets(path: Path) -> int:
    r = run_cmd(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                 "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)], timeout=300)
    return int(r.stdout.strip().strip(",") or 0)


def loudness(path: Path) -> dict[str, float]:
    """Інтегрована гучність (LUFS) і true peak (dBTP) через loudnorm у режимі вимірювання."""
    r = run_cmd(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
                 "-af", "loudnorm=print_format=json", "-f", "null", "-"], timeout=1800)
    text = r.stderr
    start = text.rfind("{")
    if start < 0:
        raise MediaError(f"loudnorm: немає виміру для {path.name}")
    data = json.loads(text[start:text.rfind("}") + 1])
    return {"i": float(data["input_i"]), "tp": float(data["input_tp"]),
            "lra": float(data["input_lra"]), "thresh": float(data["input_thresh"])}


def detect_intervals(path: Path, filt: str, kind: str, stream: str = "a",
                     start_key: str = "silence_start", end_key: str = "silence_end",
                     timeout: float = 1800) -> list[tuple[float, float]]:
    """Інтервали з silencedetect / blackdetect / freezedetect (за ключами логу)."""
    opt = "-af" if stream == "a" else "-vf"
    r = run_cmd(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), opt, filt, "-f", "null", "-"],
                timeout=timeout)
    # формати логів різні: "silence_start: 1.2", "black_start:0", "freeze_start: 3"
    pattern = re.compile(rf"({re.escape(start_key)}|{re.escape(end_key)}):\s*(-?[0-9.]+)")
    intervals: list[tuple[float, float]] = []
    start: float | None = None
    for line in r.stderr.splitlines():
        if kind not in line:
            continue
        for key, value in pattern.findall(line):
            if key == start_key:
                start = float(value)
            elif start is not None:
                intervals.append((start, float(value)))
                start = None
    if start is not None:  # інтервал до кінця файлу
        intervals.append((start, float("inf")))
    return intervals
