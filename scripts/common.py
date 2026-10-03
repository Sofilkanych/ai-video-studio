"""Спільні утиліти: шляхи, конфіг, запуск процесів, валідація схем."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "input"
WORK = ROOT / "work"
OUTPUT = ROOT / "output"
SCHEMAS = ROOT / "schemas"
CONFIG_PATH = ROOT / "config.yaml"


def load_env(path: Path | None = None) -> None:
    """Підвантажує змінні з .env у os.environ (без перезапису вже заданих)."""
    import os
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def expand(path: str) -> Path:
    return Path(path).expanduser()


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_cmd(args: list[str], timeout: float = 120, cwd: Path | None = None) -> CmdResult:
    """Запускає зовнішній процес з таймаутом. Не кидає виключення на ненульовий код."""
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        return CmdResult(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return CmdResult(-1, out, err, timed_out=True)
    except FileNotFoundError as e:
        return CmdResult(127, "", str(e))


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def validate(name: str, data: Any) -> list[str]:
    """Повертає список помилок валідації (порожній — валідно)."""
    validator = Draft202012Validator(load_schema(name), format_checker=FormatChecker())
    return [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    ]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
