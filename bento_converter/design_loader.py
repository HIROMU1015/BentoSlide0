"""Load GPT-authored design JSON without mutating it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import JsonLoadError


def load_design(path: str | Path) -> dict[str, Any]:
    design_path = Path(path)
    try:
        raw = design_path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise JsonLoadError(f"Cannot read UTF-8 design JSON {design_path}: {exc}") from exc

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JsonLoadError(
            f"Invalid JSON in {design_path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise JsonLoadError(f"Design JSON root must be an object, got {type(value).__name__}")
    return value

