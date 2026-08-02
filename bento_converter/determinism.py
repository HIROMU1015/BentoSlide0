"""Normalized evidence and SHA-256 helpers for reproducibility checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalize_evidence(value: Any, build_root: str | Path | None = None) -> Any:
    """Remove only machine/run-local evidence while retaining computed values."""

    root = str(Path(build_root).resolve()).replace("\\", "/") if build_root else None
    if isinstance(value, dict):
        return {
            key: normalize_evidence(item, build_root)
            for key, item in sorted(value.items())
            if key not in {"browser"}
        }
    if isinstance(value, (list, tuple)):
        return [normalize_evidence(item, build_root) for item in value]
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        return normalized.replace(root, "$BUILD_ROOT") if root else normalized
    return value


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
