"""Експорт слайдів: PPTX → PDF (PowerPoint, fallback LibreOffice) → PNG (pdftoppm).

Враховує приховані слайди: їх немає в PDF, тому сторінки зіставляються лише з видимими.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from pptx import Presentation

from scripts.common import ROOT, run_cmd

APPLESCRIPT = ROOT / "scripts" / "export_slides_powerpoint.applescript"


class ExportError(RuntimeError):
    pass


@dataclass
class ExportResult:
    pdf: Path
    renderer: str
    pages: int
    seconds: float


def slide_visibility(pptx: Path) -> list[bool]:
    """True для видимих слайдів (атрибут show="0" означає прихований)."""
    prs = Presentation(str(pptx))
    return [s._element.get("show") != "0" for s in prs.slides]


def pdf_page_count(pdf: Path) -> int:
    r = run_cmd(["pdfinfo", str(pdf)], timeout=30)
    if not r.ok:
        raise ExportError(f"pdfinfo failed: {r.stderr.strip()}")
    for line in r.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    raise ExportError("pdfinfo: no page count")


def map_pages(visibility: list[bool], pages: int) -> list[int | None]:
    """Номер сторінки PDF для кожного слайда (None для прихованих).

    Кидає ExportError, якщо кількість сторінок не дорівнює кількості видимих слайдів.
    """
    visible = sum(visibility)
    if visible != pages:
        raise ExportError(
            f"PDF має {pages} стор., а видимих слайдів {visible} (усього {len(visibility)})"
        )
    result: list[int | None] = []
    page = 0
    for is_visible in visibility:
        if is_visible:
            page += 1
            result.append(page)
        else:
            result.append(None)
    return result


def export_pdf_powerpoint(pptx: Path, out_pdf: Path, timeout: float = 180) -> ExportResult:
    """PowerPoint відкриває файл за повним шляхом; ім'я файлу має бути унікальним."""
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    out_pdf.unlink(missing_ok=True)
    t0 = time.monotonic()
    r = run_cmd(
        ["osascript", str(APPLESCRIPT), str(pptx.resolve()), str(out_pdf.resolve())],
        timeout=timeout,
    )
    if r.timed_out:
        raise ExportError(f"PowerPoint не відповів за {timeout} с (діалог або зависання)")
    if not r.ok or not r.stdout.strip().startswith("OK"):
        raise ExportError(f"PowerPoint export failed: {(r.stderr or r.stdout).strip()}")
    if not out_pdf.exists():
        raise ExportError("PowerPoint повідомив OK, але PDF не створено")
    return ExportResult(out_pdf, "powerpoint", pdf_page_count(out_pdf), time.monotonic() - t0)


def export_pdf_libreoffice(pptx: Path, out_pdf: Path, timeout: float = 300) -> ExportResult:
    soffice = shutil.which("soffice")
    if not soffice:
        raise ExportError("LibreOffice (soffice) не знайдено")
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    r = run_cmd(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_pdf.parent), str(pptx)],
        timeout=timeout,
    )
    produced = out_pdf.parent / (pptx.stem + ".pdf")
    if not r.ok or not produced.exists():
        raise ExportError(f"LibreOffice export failed: {(r.stderr or r.stdout).strip()}")
    if produced != out_pdf:
        produced.replace(out_pdf)
    return ExportResult(out_pdf, "libreoffice", pdf_page_count(out_pdf), time.monotonic() - t0)


def export_pdf(pptx: Path, out_pdf: Path, renderer: str = "powerpoint",
               fallback: str | None = "libreoffice", timeout: float = 180) -> ExportResult:
    exporters = {"powerpoint": export_pdf_powerpoint, "libreoffice": export_pdf_libreoffice}
    try:
        return exporters[renderer](pptx, out_pdf, timeout)
    except ExportError:
        if not fallback or fallback == renderer:
            raise
        return exporters[fallback](pptx, out_pdf)


def rasterize(pdf: Path, out_dir: Path, width: int, prefix: str = "slide") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{prefix}-*.png"):
        old.unlink()
    r = run_cmd(
        ["pdftoppm", "-png", "-scale-to-x", str(width), "-scale-to-y", "-1",
         str(pdf), str(out_dir / prefix)],
        timeout=600,
    )
    if not r.ok:
        raise ExportError(f"pdftoppm failed: {r.stderr.strip()}")
    # pdftoppm доповнює номер нулями залежно від кількості сторінок
    return sorted(out_dir.glob(f"{prefix}-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
