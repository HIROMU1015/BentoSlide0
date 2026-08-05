"""Safely discover and use the exact localhost Work editor writer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import BentoConverterError


class WorkEditorClient:
    def __init__(self, base_url: str, status: dict[str, Any]):
        self.base_url = base_url.rstrip("/")
        self.status = status

    def get(self, path: str) -> dict[str, Any]:
        try:
            with urlopen(self.base_url + path, timeout=10) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BentoConverterError(f"Cannot read Work editor API {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise BentoConverterError(f"Work editor API {path} returned a non-object")
        return value

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urlopen(request, timeout=60) as response:
                value = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error")
            except Exception:
                detail = str(exc)
            raise BentoConverterError(f"Work editor API rejected the operation ({exc.code}): {detail}") from exc
        except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BentoConverterError(f"Cannot write through Work editor API {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise BentoConverterError(f"Work editor API {path} returned a non-object")
        return value


def discover_work_editor(
    repository: str | Path, *, mode: str, target: str | Path,
) -> WorkEditorClient:
    root = Path(repository).resolve()
    session_path = root / "output/work-editor-session.json"
    try:
        session = json.loads(session_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BentoConverterError(
            "The writer lease is busy and no trustworthy Work editor session can be discovered"
        ) from exc
    if not isinstance(session, dict) or session.get("format") != "bento/work-editor-session/v1":
        raise BentoConverterError("The writer lease is busy and the Work editor session format is invalid")
    host = session.get("host")
    port = session.get("port")
    if host not in {"127.0.0.1", "localhost"} or not isinstance(port, int) or not 1 <= port <= 65535:
        raise BentoConverterError("The writer lease is busy and the Work editor session is not loopback-only")
    client = WorkEditorClient(f"http://127.0.0.1:{port}", {})
    status = client.get("/api/status")
    expected_target_path = Path(target).resolve()
    try:
        expected_target = expected_target_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise BentoConverterError("Expected Work editor target escapes the repository") from exc
    actual_repository = status.get("repository")
    if (
        not isinstance(actual_repository, str)
        or os.path.normcase(str(Path(actual_repository).resolve())) != os.path.normcase(str(root))
        or status.get("editingMode") != mode
        or status.get("target") != expected_target
    ):
        raise BentoConverterError(
            "The discovered localhost Work editor does not match the exact repository, mode, and target"
        )
    client.status = status
    return client
