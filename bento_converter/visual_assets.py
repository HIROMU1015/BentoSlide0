"""Transactional local visual-asset registration and PDF figure extraction."""

from __future__ import annotations

import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .artifact_transaction import ArtifactTransactionStore, file_revision, recover_repository_transactions
from .errors import BentoConverterError
from .registry_document import (
    GENERATED_FORBIDDEN_ROLES,
    canonical_registry_json,
    content_digest,
    load_registry,
    normalize_registry,
    registry_revision,
    validate_registry,
)


VISUAL_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}


@dataclass(frozen=True)
class SourceReference:
    source_id: str
    locator: str


def parse_source_reference(value: str) -> SourceReference:
    source_id, separator, locator = value.partition("::")
    if not separator or not source_id.strip() or not locator.strip():
        raise BentoConverterError("Source references must use SOURCE_ID::LOCATOR")
    return SourceReference(source_id.strip(), locator.strip())


def _safe_id(value: str, *, label: str) -> str:
    if not VISUAL_ID_PATTERN.fullmatch(value):
        raise BentoConverterError(f"{label} must match {VISUAL_ID_PATTERN.pattern}")
    return value


def _origin(kind: str, references: list[SourceReference], *, extraction: dict[str, Any] | None = None) -> dict[str, Any]:
    if kind == "source-original":
        if len(references) != 1:
            raise BentoConverterError("source-original requires exactly one source reference")
        value: dict[str, Any] = {
            "kind": kind, "sourceId": references[0].source_id, "locator": references[0].locator,
        }
    elif kind == "source-derived":
        if not references:
            raise BentoConverterError("source-derived requires at least one source reference")
        value = {
            "kind": kind,
            "sources": [{"sourceId": item.source_id, "locator": item.locator} for item in references],
        }
    elif kind == "generated":
        if references:
            raise BentoConverterError("generated visuals must not claim source references")
        value = {"kind": kind}
    else:
        raise BentoConverterError(f"Unsupported visual origin kind: {kind!r}")
    if extraction:
        value["extraction"] = extraction
    return value


def _target_directory(kind: str) -> str:
    return "source" if kind == "source-original" else "generated" if kind == "generated" else "local"


def register_visual_asset(
    *,
    repository: str | Path,
    registry_path: str | Path,
    input_path: str | Path,
    asset_id: str,
    kind: str,
    role: str,
    source_references: Iterable[SourceReference] = (),
    figure_id: str | None = None,
    caption: str | None = None,
    description: str | None = None,
    generator: dict[str, Any] | None = None,
    extraction: dict[str, Any] | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Copy one local image and update its asset/figure definitions as one transaction."""

    root = Path(repository).resolve()
    registry_file = Path(registry_path)
    if not registry_file.is_absolute():
        registry_file = root / registry_file
    registry_file = registry_file.resolve()
    try:
        registry_file.relative_to(root)
    except ValueError as exc:
        raise BentoConverterError("Registry path escapes the repository") from exc
    source = Path(input_path).resolve()
    if not source.is_file():
        raise BentoConverterError(f"Visual asset input does not exist: {source}")
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_IMAGE_SUFFIXES:
        raise BentoConverterError(f"Unsupported visual asset type: {suffix or '<none>'}")
    asset_id = _safe_id(asset_id, label="asset_id")
    figure_id = _safe_id(figure_id or asset_id, label="figure_id")
    if not isinstance(role, str) or not role.strip():
        raise BentoConverterError("role must be a non-empty string")
    if kind == "generated" and role in GENERATED_FORBIDDEN_ROLES:
        raise BentoConverterError(f"Generated visual cannot have role {role!r}")
    references = list(source_references)
    origin = _origin(kind, references, extraction=extraction)

    recover_repository_transactions(root)
    registry = normalize_registry(load_registry(registry_file), unit_id="deck")
    base_revision = file_revision(registry_file)
    assets = registry.setdefault("assets", {})
    figures = registry.setdefault("figures", {})
    if not replace and (asset_id in assets or figure_id in figures):
        raise BentoConverterError(f"Visual asset or figure ID already exists: {asset_id}/{figure_id}")
    destination = registry_file.parent / "assets" / _target_directory(kind) / f"{asset_id}{suffix}"
    destination = destination.resolve()
    try:
        relative_asset = destination.relative_to(registry_file.parent).as_posix()
        destination.relative_to(root)
    except ValueError as exc:
        raise BentoConverterError("Visual asset destination escapes the repository") from exc
    if destination.exists() and not replace and destination != source:
        raise BentoConverterError(f"Visual asset destination already exists: {destination}")

    asset_payload = source.read_bytes()
    asset_definition: dict[str, Any] = {
        "path": relative_asset,
        "mimeType": mimetypes.guess_type(destination.name)[0] or "application/octet-stream",
        "role": role,
        "origin": origin,
        "contentDigest": content_digest(asset_payload),
    }
    if description:
        asset_definition["description"] = description
    if generator is not None:
        if kind != "generated":
            raise BentoConverterError("generator metadata is allowed only for generated visuals")
        asset_definition["generator"] = generator
    if kind == "source-original":
        asset_definition["provenance"] = {
            "sourceId": references[0].source_id, "locator": references[0].locator,
        }
    figure_definition: dict[str, Any] = {"assetId": asset_id, "role": role, "origin": origin}
    if caption:
        figure_definition["caption"] = caption
    if description:
        figure_definition["description"] = description
    if kind == "source-original":
        figure_definition["provenance"] = dict(asset_definition["provenance"])
    assets[asset_id] = asset_definition
    figures[figure_id] = figure_definition
    validate_registry(registry, allow_v1=False)

    registry_payload = (canonical_registry_json(registry) + "\n").encode("utf-8")
    payloads = {registry_file: registry_payload, destination: asset_payload}
    store = ArtifactTransactionStore(root, payloads)

    def validate_base() -> None:
        if file_revision(registry_file) != base_revision:
            raise BentoConverterError("Registry changed while the visual asset was being prepared")

    result = store.commit(
        payloads,
        operation="register-visual-asset",
        base_registry_revision=base_revision,
        target_registry_revision=registry_revision(registry),
        validate_base=validate_base,
        validate_committed=lambda: validate_registry(load_registry(registry_file), allow_v1=False),
    )
    return {
        **result,
        "assetId": asset_id,
        "figureId": figure_id,
        "kind": kind,
        "path": destination.relative_to(root).as_posix(),
        "contentDigest": asset_definition["contentDigest"],
    }


def extract_pdf_figure(
    *,
    repository: str | Path,
    registry_path: str | Path,
    source_id: str,
    page: int,
    crop: tuple[float, float, float, float],
    dpi: float,
    asset_id: str,
    locator: str,
    figure_number: str | None = None,
    caption: str | None = None,
    role: str = "source-figure",
    replace: bool = False,
) -> dict[str, Any]:
    """Render a PDF crop to PNG, then register it as source-original."""

    asset_id = _safe_id(asset_id, label="asset_id")
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BentoConverterError("PDF figure extraction requires PyMuPDF") from exc
    root = Path(repository).resolve()
    registry_file = Path(registry_path)
    if not registry_file.is_absolute():
        registry_file = root / registry_file
    registry = normalize_registry(load_registry(registry_file), unit_id="deck")
    source_definition = registry.get("sources", {}).get(source_id)
    if not isinstance(source_definition, dict):
        raise BentoConverterError(f"Unknown registry sourceId: {source_id!r}")
    pdf_path = (root / source_definition["path"]).resolve()
    try:
        pdf_path.relative_to(root)
    except ValueError as exc:
        raise BentoConverterError("PDF source path escapes the repository") from exc
    if page < 1:
        raise BentoConverterError("PDF page is one-based and must be at least 1")
    x0, y0, x1, y1 = crop
    if not all(value >= 0 for value in crop) or x1 <= x0 or y1 <= y0:
        raise BentoConverterError("PDF crop must be x0 y0 x1 y1 with positive area")
    if dpi <= 0:
        raise BentoConverterError("PDF extraction dpi must be positive")
    try:
        document = pymupdf.open(pdf_path)
        if page > document.page_count:
            raise BentoConverterError(f"PDF page {page} exceeds page count {document.page_count}")
        pdf_page = document.load_page(page - 1)
        clip = pymupdf.Rect(x0, y0, x1, y1)
        if not pdf_page.rect.contains(clip):
            raise BentoConverterError(f"PDF crop {list(crop)} is outside page bounds {list(pdf_page.rect)}")
        pixmap = pdf_page.get_pixmap(clip=clip, dpi=dpi, alpha=False)
        png = pixmap.tobytes("png")
    finally:
        if "document" in locals():
            document.close()
    library_path = root / "images" / "extracted" / f"{asset_id}.png"
    if library_path.exists() and not replace:
        raise BentoConverterError(f"Extracted image library path already exists: {library_path}")
    library_path.parent.mkdir(parents=True, exist_ok=True)
    library_path.write_bytes(png)
    extraction = {
        "page": page,
        "crop": [x0, y0, x1, y1],
        "dpi": dpi,
        **({"figureNumber": figure_number} if figure_number else {}),
        **({"caption": caption} if caption else {}),
    }
    result = register_visual_asset(
        repository=root,
        registry_path=registry_file,
        input_path=library_path,
        asset_id=asset_id,
        kind="source-original",
        role=role,
        source_references=[SourceReference(source_id, locator)],
        caption=caption,
        extraction=extraction,
        replace=replace,
    )
    return {**result, "libraryPath": library_path.relative_to(root).as_posix()}
