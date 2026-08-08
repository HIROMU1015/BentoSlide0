"""Machine-readable visual-strategy planning for HTML-first decks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .errors import BentoConverterError
from .registry_document import VISUAL_ORIGIN_KINDS


VISUAL_PLAN_VERSION = 1
VISUAL_TYPES = {"none", "native-diagram", "generated-image", "source-figure"}


def load_visual_plan(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BentoConverterError(f"Cannot read visual plan {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise BentoConverterError("Visual plan root must be an object")
    validate_visual_plan(value)
    return value


def validate_visual_plan(plan: dict[str, Any]) -> None:
    if plan.get("schemaVersion") != VISUAL_PLAN_VERSION:
        raise BentoConverterError(f"Visual plan schemaVersion must be {VISUAL_PLAN_VERSION}")
    slides = plan.get("slides")
    if not isinstance(slides, list):
        raise BentoConverterError("Visual plan slides must be an array")
    seen: set[str] = set()
    for index, slide in enumerate(slides):
        label = f"Visual plan slides[{index}]"
        if not isinstance(slide, dict):
            raise BentoConverterError(f"{label} must be an object")
        slide_id = slide.get("id")
        if not isinstance(slide_id, str) or not slide_id.strip():
            raise BentoConverterError(f"{label}.id must be a non-empty string")
        if slide_id in seen:
            raise BentoConverterError(f"Visual plan has duplicate slide id: {slide_id}")
        seen.add(slide_id)
        purpose = slide.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            raise BentoConverterError(f"{label}.purpose must be a non-empty string")
        visual = slide.get("visual")
        if not isinstance(visual, dict):
            raise BentoConverterError(f"{label}.visual must be an object")
        recommended = visual.get("recommended")
        visual_type = visual.get("type")
        if not isinstance(recommended, bool):
            raise BentoConverterError(f"{label}.visual.recommended must be boolean")
        if visual_type not in VISUAL_TYPES:
            raise BentoConverterError(f"{label}.visual.type must be one of {sorted(VISUAL_TYPES)}")
        if recommended and visual_type == "none":
            raise BentoConverterError(f"{label} recommends a visual but selects type none")
        if not recommended and visual_type != "none":
            raise BentoConverterError(f"{label} does not recommend a visual and must select type none")
        intent = visual.get("intent")
        if recommended and (not isinstance(intent, str) or not intent.strip()):
            raise BentoConverterError(f"{label}.visual.intent is required for a recommended visual")
        origin_kind = visual.get("originKind")
        if origin_kind is not None and origin_kind not in VISUAL_ORIGIN_KINDS:
            raise BentoConverterError(f"{label}.visual.originKind is invalid")
        if visual_type == "source-figure" and origin_kind not in {None, "source-original"}:
            raise BentoConverterError(f"{label} source-figure must use source-original origin")
        if visual_type == "generated-image" and origin_kind not in {None, "generated"}:
            raise BentoConverterError(f"{label} generated-image must use generated origin")
