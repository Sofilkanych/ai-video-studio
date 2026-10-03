"""Фаза 0: перевірка середовища (python run.py doctor [--powerpoint] [--hdr] [--render] | --all).

Статуси: ok — готово; warn — не блокує поточну фазу, але потрібне пізніше; fail — блокер.
Звіт: work/doctor/env_report.json.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import time
from datetime import datetime
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.common import ROOT, WORK, expand, load_config, run_cmd, write_json

DOCTOR = WORK / "doctor"


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str = ""


def _bin(name: str, required: bool, purpose: str) -> Check:
    path = shutil.which(name)
    if path:
        return Check(f"bin:{name}", "ok", path)
    return Check(f"bin:{name}", "fail" if required else "warn", f"не знайдено ({purpose})")


def check_binaries() -> list[Check]:
    return [
        _bin("ffmpeg", True, "медіа"),
        _bin("ffprobe", True, "метадані медіа"),
        _bin("pdftoppm", True, "PDF → PNG"),
        _bin("pdfinfo", True, "кількість сторінок PDF"),
        _bin("pdftotext", True, "текстовий шар PDF (Фаза 2)"),
        _bin("osascript", True, "автоматизація PowerPoint"),
        _bin("node", True, "Remotion"),
        _bin("npm", True, "Remotion"),
        _bin("whisper-cli", True, "STT (whisper.cpp)"),
        _bin("soffice", False, "fallback рендеру слайдів"),
        _bin("avconvert", False, "HDR → SDR, якщо немає zscale"),
        _bin("git", False, "версіонування"),
    ]


def check_node() -> list[Check]:
    r = run_cmd(["node", "--version"], timeout=15)
    if not r.ok:
        return []
    version = r.stdout.strip()
    major = int(version.lstrip("v").split(".")[0])
    # Remotion 4 потребує Node 16+; беремо з запасом 18+
    return [Check("node:version", "ok" if major >= 18 else "fail", version)]


def check_ffmpeg_filters() -> tuple[list[Check], bool]:
    r = run_cmd(["ffmpeg", "-hide_banner", "-filters"], timeout=30)
    names = {line.split()[1] for line in r.stdout.splitlines() if len(line.split()) > 2}
    checks = []
    for f in ["loudnorm", "freezedetect", "blackdetect", "silencedetect", "afftdn", "aresample"]:
        checks.append(Check(f"ffmpeg:{f}", "ok" if f in names else "fail"))
    has_zscale = "zscale" in names
    checks.append(Check("ffmpeg:zscale", "ok" if has_zscale else "warn",
                        "" if has_zscale else "немає — HDR tone mapping через avconvert"))
    return checks, has_zscale


def check_tonemapper(has_zscale: bool, run_test: bool) -> list[Check]:
    if not has_zscale and not shutil.which("avconvert"):
        return [Check("hdr:tonemapper", "fail", "ні zscale, ні avconvert — HDR-відео буде вицвілим")]
    tool = "zscale" if has_zscale else "avconvert"
    if not run_test:
        return [Check("hdr:tonemapper", "warn", f"{tool} доступний, не перевірено (doctor --hdr)")]

    d = DOCTOR / "hdr"
    d.mkdir(parents=True, exist_ok=True)
    src, dst = d / "hlg.mov", d / "sdr.mov"
    gen = run_cmd([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx265", "-pix_fmt", "yuv420p10le",
        "-x265-params", "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:log-level=error",
        "-tag:v", "hvc1", "-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
        "-colorspace", "bt2020nc", "-c:a", "aac", str(src),
    ], timeout=120)
    if not gen.ok:
        return [Check("hdr:tonemapper", "warn", f"не вдалося згенерувати HLG-кліп: {gen.stderr.strip()[:200]}")]
    if tool == "zscale":
        chain = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                 "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
        conv = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        "-vf", chain, "-c:v", "libx264", "-color_trc", "bt709", "-color_primaries", "bt709",
                        "-colorspace", "bt709", "-an", str(dst)], timeout=180)
    else:
        conv = run_cmd(["avconvert", "-s", str(src), "-p", "PresetHighestQuality", "-o", str(dst),
                        "--replace"], timeout=180)
    probe = run_cmd(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                     "stream=color_transfer,r_frame_rate", "-of", "json", str(dst)], timeout=30)
    try:
        stream = json.loads(probe.stdout)["streams"][0]
    except (json.JSONDecodeError, KeyError, IndexError):
        return [Check("hdr:tonemapper", "fail", f"{tool} не дав результату: {(conv.stderr or conv.stdout).strip()[:200]}")]
    ok = stream.get("color_transfer") == "bt709" and stream.get("r_frame_rate") == "30/1"
    return [Check("hdr:tonemapper", "ok" if ok else "fail",
                  f"{tool} HLG → {stream.get('color_transfer')}, {stream.get('r_frame_rate')} fps "
                  "(синтетичний кліп; на реальному iPhone-відео перевірити у Фазі 1)")]


def check_whisper(cfg: dict) -> list[Check]:
    r = run_cmd(["whisper-cli", "--help"], timeout=30)
    text = r.stdout + r.stderr
    checks = []
    for flag in ["--carry-initial-prompt", "--dtw", "--vad", "--output-json-full", "--prompt"]:
        checks.append(Check(f"whisper:{flag}", "ok" if flag in text else "fail"))
    models_dir = expand(cfg["stt"]["models_dir"])
    model = models_dir / f"ggml-{cfg['stt']['model']}.bin"
    checks.append(Check("whisper:model", "ok" if model.exists() else "warn",
                        str(model) if model.exists() else f"{model} — завантажити у Фазі 1 (~3 ГБ)"))
    return checks


def check_python() -> list[Check]:
    checks = [Check("python:version", "ok", platform.python_version())]
    for mod in ["pptx", "yaml", "jsonschema", "PIL"]:
        try:
            __import__(mod)
            checks.append(Check(f"python:{mod}", "ok"))
        except ImportError:
            checks.append(Check(f"python:{mod}", "fail", "pip install -r requirements.txt"))
    return checks


def check_powerpoint_app() -> list[Check]:
    app = Path("/Applications/Microsoft PowerPoint.app")
    if not app.exists():
        return [Check("powerpoint:app", "warn", "не встановлено — буде використано LibreOffice")]
    with (app / "Contents" / "Info.plist").open("rb") as f:
        version = plistlib.load(f).get("CFBundleShortVersionString", "?")
    return [Check("powerpoint:app", "ok", f"версія {version}")]


def check_remotion() -> list[Check]:
    pkg = ROOT / "remotion" / "node_modules" / "remotion" / "package.json"
    if not pkg.exists():
        return [Check("remotion", "fail", "cd remotion && npm install")]
    version = json.loads(pkg.read_text())["version"]
    checks = [Check("remotion", "ok", f"версія {version} (безкоштовно лише для evaluation — remotion.dev/license)")]
    # node_modules має лежати поза iCloud і без «.nosync» у шляху (webpack ламається на node_modules.nosync)
    nm = ROOT / "remotion" / "node_modules"
    real = nm.resolve()
    in_icloud = "/Desktop/" in str(real) or "Mobile Documents" in str(real)
    bad = in_icloud or ".nosync" in str(real)
    checks.append(Check("remotion:node_modules", "warn" if bad else "ok",
                        f"{real}" + (" — у iCloud або з .nosync: див. README «iCloud»" if bad else "")))
    return checks


def check_secrets_and_policy(cfg: dict) -> list[Check]:
    env = ROOT / ".env"
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                key = line.split("=", 1)[1].strip()
    checks = [Check("secrets:ANTHROPIC_API_KEY", "ok" if key else "warn",
                    "задано" if key else "не задано в .env — потрібен з Фази 1 (Director)")]
    dp = cfg.get("data_policy", {})
    checks.append(Check("policy:consumer_subscription",
                        "ok" if not dp.get("allow_consumer_subscription") else "warn",
                        "заборонено для робочих даних" if not dp.get("allow_consumer_subscription")
                        else "дозволено — не використовувати з робочими матеріалами"))
    return checks


def check_slide_export(cfg: dict) -> list[Check]:
    """Реальний тест: фікстура з прихованим слайдом → PDF → PNG через PowerPoint і LibreOffice."""
    from scripts.export_slides import (ExportError, export_pdf_libreoffice,
                                       export_pdf_powerpoint, map_pages, rasterize,
                                       slide_visibility)
    from scripts.make_fixture import build

    d = DOCTOR / "slides"
    tag = time.strftime("doctor_%Y%m%d_%H%M%S")
    deck = build(d / f"{tag}.pptx")  # унікальне ім'я: не сплутати з відкритими презентаціями
    visibility = slide_visibility(deck)
    checks = []
    exporters = [("powerpoint", export_pdf_powerpoint)]
    if shutil.which("soffice"):
        exporters.append(("libreoffice", export_pdf_libreoffice))
    for name, fn in exporters:
        try:
            res = fn(deck, d / name / f"{tag}.pdf", cfg["slides"]["powerpoint_timeout_s"])
            mapping = map_pages(visibility, res.pages)
            pngs = rasterize(res.pdf, d / name, width=1280)
            checks.append(Check(
                f"slides:{name}", "ok",
                f"{len(visibility)} слайдів, {sum(visibility)} видимих → {res.pages} стор. PDF, "
                f"{len(pngs)} PNG за {res.seconds:.1f} с; mapping {mapping}"))
        except ExportError as e:
            checks.append(Check(f"slides:{name}", "fail" if name == "powerpoint" else "warn", str(e)))
    return checks


def check_chunked_render() -> list[Check]:
    """Remotion: 2 чанки --frames --muted → concat -c copy → звірка кількості пакетів."""
    d = DOCTOR / "render"
    d.mkdir(parents=True, exist_ok=True)
    remotion_dir = ROOT / "remotion"
    chunks = []
    for rng in ["0-89", "90-179"]:
        out = d / f"chunk_{rng}.mp4"
        r = run_cmd(["npx", "remotion", "render", "src/index.ts", "Main", str(out),
                     f"--frames={rng}", "--muted", "--codec=h264", "--log=error"],
                    timeout=600, cwd=remotion_dir)
        if not r.ok or not out.exists():
            return [Check("render:chunks", "fail", (r.stderr or r.stdout).strip()[-300:])]
        chunks.append(out)
    listing = d / "list.txt"
    listing.write_text("".join(f"file '{c.name}'\n" for c in chunks))
    joined = d / "joined.mp4"
    r = run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(listing), "-c", "copy", str(joined)], timeout=120)
    if not r.ok:
        return [Check("render:concat", "fail", r.stderr.strip()[-300:])]
    probe = run_cmd(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                     "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(joined)], timeout=60)
    # csv може мати кінцеву кому ("180,")
    packets = int(probe.stdout.strip().strip(",") or 0)
    return [Check("render:chunks+concat", "ok" if packets == 180 else "fail",
                  f"2 чанки × 90 кадрів → {packets} пакетів після concat (очікується 180)")]


def run(powerpoint: bool = False, hdr: bool = False, render: bool = False) -> int:
    cfg = load_config()
    checks: list[Check] = []
    checks += check_binaries()
    checks += check_node()
    ff, has_zscale = check_ffmpeg_filters()
    checks += ff
    checks += check_tonemapper(has_zscale, hdr)
    checks += check_whisper(cfg) if shutil.which("whisper-cli") else []
    checks += check_python()
    checks += check_powerpoint_app()
    checks += check_remotion()
    checks += check_secrets_and_policy(cfg)
    if powerpoint:
        checks += check_slide_export(cfg)
    if render:
        checks += check_chunked_render()

    icons = {"ok": "✓", "warn": "!", "fail": "✗"}
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f" {icons[c.status]} {c.name:<{width}}  {c.detail}")
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    print(f"\n{len(checks)} перевірок: {fails} fail, {warns} warn")

    write_json(DOCTOR / "env_report.json", {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "checks": [asdict(c) for c in checks],
    })
    return 1 if fails else 0
