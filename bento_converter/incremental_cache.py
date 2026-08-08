"""Slide-granular cache for interactive HTML-first builds."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .browser_harness import canonical_digest
from .errors import BentoConverterError
from .html_source import SourceChapter
from .section_approval import (
    HtmlNode,
    _DocumentParser,
    _canonical_json,
    _canonical_node,
    _global_css_payload,
    _hash_local_url,
    _registry_projection,
    _urls_from_node,
    _walk,
)


CACHE_FORMAT = "bento/incremental-slide-cache/v1"
CACHE_KEY_FORMAT = "bento/incremental-slide-key/v1"
VISUAL_ALGORITHM_VERSION = "bento/visual-comparison/v2"
CONVERTER_IMPLEMENTATION_FILES = (
    "browser_harness.py",
    "html_layout.py",
    "html_converter.py",
    "incremental_cache.py",
    "resource_embedding.py",
    "visual_comparison.py",
)


@dataclass(frozen=True)
class SourceFingerprintInput:
    slide_id: str
    chapter_id: str
    digest: str


@lru_cache(maxsize=1)
def converter_implementation_digest() -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in CONVERTER_IMPLEMENTATION_FILES:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update((root / name).read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _repository_for(paths: list[Path]) -> Path:
    resolved = [path.resolve() for path in paths]
    common = Path(os.path.commonpath([str(path.parent) for path in resolved])).resolve()
    for candidate in (common, *common.parents):
        if (candidate / "deck.yaml").is_file():
            return candidate
    return common


def _global_dom(node: HtmlNode) -> dict[str, Any] | None:
    if node.attrs.get("data-slide-id"):
        return None
    children: list[Any] = []
    for child in node.children:
        if isinstance(child, HtmlNode):
            value = _global_dom(child)
            if value is not None:
                children.append(value)
        else:
            children.append(child)
    return {
        "tag": node.tag,
        "attrs": [[key, node.attrs[key]] for key in sorted(node.attrs)],
        "children": children,
    }


def compute_source_fingerprint_inputs(chapters: list[SourceChapter]) -> dict[str, SourceFingerprintInput]:
    """Hash slide DOM, relevant registry/assets, and global document styling."""

    repository = _repository_for(
        [path for chapter in chapters for path in (chapter.html_path, chapter.registry_path)]
    )
    result: dict[str, SourceFingerprintInput] = {}
    for chapter in chapters:
        parser = _DocumentParser()
        parser.feed(chapter.html_path.read_text(encoding="utf-8-sig"))
        parser.close()
        global_payload = {
            "css": _global_css_payload(
                parser.root,
                html_path=chapter.html_path,
                repository=repository,
                registry=chapter.registry,
            ),
            "dom": _global_dom(parser.root),
        }
        global_digest = canonical_digest(global_payload)
        for node in _walk(parser.root):
            slide_id = node.attrs.get("data-slide-id")
            if not slide_id:
                continue
            if node.tag != "section":
                raise BentoConverterError(f"data-slide-id={slide_id!r} must be on a section element")
            if slide_id in result:
                raise BentoConverterError(f"Duplicate slide id in incremental source: {slide_id}")
            projection, references = _registry_projection([node], chapter.registry)
            asset_hashes: dict[str, str] = {}
            for url in _urls_from_node(node):
                resolved = _hash_local_url(url, base=chapter.html_path.parent, root=repository)
                if resolved:
                    asset_hashes[resolved[0]] = resolved[1]
            for asset_id in references.get("assets", ()):
                definition = chapter.registry.get("assets", {}).get(asset_id, {})
                if isinstance(definition, dict) and isinstance(definition.get("path"), str):
                    resolved = _hash_local_url(
                        definition["path"], base=chapter.html_path.parent, root=repository,
                    )
                    if resolved:
                        asset_hashes[resolved[0]] = resolved[1]
            payload = {
                "format": CACHE_KEY_FORMAT,
                "slideId": slide_id,
                "chapterId": chapter.chapter_id,
                "dom": _canonical_node(node),
                "registry": projection,
                "assets": asset_hashes,
                "globalDigest": global_digest,
            }
            result[slide_id] = SourceFingerprintInput(
                slide_id, chapter.chapter_id, canonical_digest(payload),
            )
    if not result:
        raise BentoConverterError("HTML source contains no section[data-slide-id] slides")
    return result


def source_cache_key(
    value: SourceFingerprintInput, *, environment_digest: str, runtime_fingerprint: str,
) -> str:
    return canonical_digest({
        "format": CACHE_KEY_FORMAT,
        "kind": "source-layout",
        "source": value.digest,
        "converter": converter_implementation_digest(),
        "environment": environment_digest,
        "runtime": runtime_fingerprint,
    })


def bento_cache_key(
    slide: dict[str, Any], *, environment_digest: str, runtime_fingerprint: str,
) -> str:
    return canonical_digest({
        "format": CACHE_KEY_FORMAT,
        "kind": "bento-render",
        "slide": slide,
        "converter": converter_implementation_digest(),
        "environment": environment_digest,
        "runtime": runtime_fingerprint,
    })


def comparison_cache_key(source_key: str, bento_key: str, decisions: list[dict[str, Any]]) -> str:
    return canonical_digest({
        "format": CACHE_KEY_FORMAT,
        "kind": "visual-comparison",
        "algorithm": VISUAL_ALGORITHM_VERSION,
        "source": source_key,
        "bento": bento_key,
        "decisions": decisions,
    })


class IncrementalSlideCache:
    """Read/write complete per-slide cache records without exposing partial files."""

    def __init__(self, root: str | Path, *, reuse: bool) -> None:
        self.root = Path(root).resolve()
        self.reuse = reuse
        self.stats = {
            "sourceHits": 0,
            "sourceMisses": 0,
            "bentoHits": 0,
            "bentoMisses": 0,
            "comparisonHits": 0,
            "comparisonMisses": 0,
        }

    def _directory(self, slide_id: str) -> Path:
        identifier = hashlib.sha256(slide_id.encode("utf-8")).hexdigest()[:20]
        return self.root / "slides" / identifier

    @staticmethod
    def _atomic_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _atomic_json(cls, path: Path, value: Any) -> None:
        cls._atomic_bytes(
            path,
            # Preserve insertion order because the converter's emitted Bento JSON is
            # byte-deterministic, not merely semantically deterministic.  Cache keys
            # are canonicalized separately, so sorting persisted payloads here would
            # make a cache hit reorder nested author data such as chart options.
            (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False, allow_nan=False) + "\n").encode("utf-8"),
        )

    @staticmethod
    def _read_record(path: Path, *, slide_id: str, fingerprint: str, kind: str) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("format") != CACHE_FORMAT:
            return None
        if value.get("kind") != kind or value.get("slideId") != slide_id or value.get("fingerprint") != fingerprint:
            return None
        return value

    def load_source(
        self, slide_id: str, fingerprint: str, screenshot_target: Path,
    ) -> tuple[dict[str, Any], dict[str, str]] | None:
        directory = self._directory(slide_id)
        record = self._read_record(
            directory / "source.json", slide_id=slide_id, fingerprint=fingerprint, kind="source-layout",
        ) if self.reuse else None
        screenshot = directory / "source.png"
        if record is None or not screenshot.is_file() or not isinstance(record.get("layout"), dict):
            self.stats["sourceMisses"] += 1
            return None
        screenshot_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(screenshot, screenshot_target)
        self.stats["sourceHits"] += 1
        fallbacks = record.get("fallbacks", {})
        return record["layout"], fallbacks if isinstance(fallbacks, dict) else {}

    def save_source(
        self, slide_id: str, fingerprint: str, layout: dict[str, Any],
        fallbacks: dict[str, str], screenshot: Path,
    ) -> None:
        directory = self._directory(slide_id)
        self._atomic_bytes(directory / "source.png", screenshot.read_bytes())
        self._atomic_json(directory / "source.json", {
            "format": CACHE_FORMAT,
            "kind": "source-layout",
            "slideId": slide_id,
            "fingerprint": fingerprint,
            "layout": layout,
            "fallbacks": fallbacks,
        })

    def load_bento(self, slide_id: str, fingerprint: str, screenshot_target: Path) -> bool:
        directory = self._directory(slide_id)
        record = self._read_record(
            directory / "bento.json", slide_id=slide_id, fingerprint=fingerprint, kind="bento-render",
        ) if self.reuse else None
        screenshot = directory / "bento.png"
        if record is None or not screenshot.is_file():
            self.stats["bentoMisses"] += 1
            return False
        screenshot_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(screenshot, screenshot_target)
        self.stats["bentoHits"] += 1
        return True

    def save_bento(
        self, slide_id: str, fingerprint: str, slide: dict[str, Any], screenshot: Path,
    ) -> None:
        directory = self._directory(slide_id)
        self._atomic_bytes(directory / "bento.png", screenshot.read_bytes())
        self._atomic_json(directory / "bento.json", {
            "format": CACHE_FORMAT,
            "kind": "bento-render",
            "slideId": slide_id,
            "fingerprint": fingerprint,
            "slide": slide,
        })

    def load_comparison(self, slide_id: str, fingerprint: str) -> dict[str, Any] | None:
        record = self._read_record(
            self._directory(slide_id) / "comparison.json",
            slide_id=slide_id,
            fingerprint=fingerprint,
            kind="visual-comparison",
        ) if self.reuse else None
        if record is None or not isinstance(record.get("comparison"), dict):
            self.stats["comparisonMisses"] += 1
            return None
        self.stats["comparisonHits"] += 1
        return record["comparison"]

    def save_comparison(self, slide_id: str, fingerprint: str, comparison: dict[str, Any]) -> None:
        self._atomic_json(self._directory(slide_id) / "comparison.json", {
            "format": CACHE_FORMAT,
            "kind": "visual-comparison",
            "slideId": slide_id,
            "fingerprint": fingerprint,
            "comparison": comparison,
        })

    def report(self) -> dict[str, Any]:
        return {
            "format": CACHE_FORMAT,
            "enabled": self.reuse,
            "cacheRoot": ".bento-cache",
            **self.stats,
        }
