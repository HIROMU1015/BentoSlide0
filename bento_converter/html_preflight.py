"""Fast static checks for HTML structures that reduce Bento editability."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .errors import ValidationError, issue
from .html_source import SourceUnit


PREFLIGHT_FORMAT = "bento/html-preflight/v1"
ANNOTATION_ATTRIBUTES = {"data-bento-id", "data-bento-type", "data-bento-export"}
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
TEXTLESS_ELEMENTS = {"script", "style", "template"}


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: "_Node | None"
    children: list["_Node"] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    slide_id: str | None = None

    @property
    def annotated(self) -> bool:
        return any(name in self.attrs for name in ANNOTATION_ATTRIBUTES)

    @property
    def element_id(self) -> str | None:
        value = self.attrs.get("data-bento-id", "").strip()
        return value or None

    @property
    def element_type(self) -> str:
        explicit = self.attrs.get("data-bento-type", "").strip()
        if explicit:
            return explicit
        return {
            "table": "table", "img": "image", "svg": "svg",
            "video": "media", "audio": "media", "canvas": "canvas",
        }.get(self.tag, "text")

    @property
    def export_mode(self) -> str:
        return self.attrs.get("data-bento-export", "auto").strip() or "auto"

    @property
    def fallback_scope(self) -> str:
        explicit = self.attrs.get("data-bento-fallback-scope", "").strip()
        if explicit:
            return explicit
        return "background" if self.rendered_text() or self.annotated_descendants() else "whole"

    def rendered_text(self) -> str:
        values = list(self.text) if self.tag not in TEXTLESS_ELEMENTS else []
        for child in self.children:
            values.append(child.rendered_text())
        return " ".join(" ".join(values).split())

    def annotated_descendants(self) -> list["_Node"]:
        result: list[_Node] = []
        for child in self.children:
            if child.annotated:
                result.append(child)
            result.extend(child.annotated_descendants())
        return result


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.roots: list[_Node] = []
        self.stack: list[_Node] = []
        self.slides: list[_Node] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = {name.lower(): value or "" for name, value in attrs}
        parent = self.stack[-1] if self.stack else None
        node = _Node(tag.lower(), normalized, parent)
        if parent is None:
            self.roots.append(node)
        else:
            parent.children.append(node)
        inherited_slide = parent.slide_id if parent else None
        classes = set(normalized.get("class", "").split())
        if node.tag == "section" and "slide" in classes:
            node.slide_id = normalized.get("data-slide-id", "").strip() or None
            self.slides.append(node)
        else:
            node.slide_id = inherited_slide
        if node.tag not in VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1].tag == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == normalized:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1].tag not in TEXTLESS_ELEMENTS:
            self.stack[-1].text.append(data)


@dataclass(frozen=True)
class PreflightIssue:
    severity: str
    code: str
    unit_id: str
    source: str
    slide_id: str | None
    element_id: str | None
    ancestor_id: str | None
    message: str
    fix: str

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return {
            "severity": value["severity"],
            "code": value["code"],
            "unitId": value["unit_id"],
            "source": value["source"],
            "slideId": value["slide_id"],
            "elementId": value["element_id"],
            "ancestorId": value["ancestor_id"],
            "message": value["message"],
            "fix": value["fix"],
        }

    def validation_issue(self) -> str:
        actual: Any = self.code
        if self.ancestor_id:
            actual = {"code": self.code, "ancestorId": self.ancestor_id}
        return issue(
            slide_id=self.slide_id,
            element_id=self.element_id,
            field="htmlPreflight",
            actual=actual,
            fix=self.fix,
        )


@dataclass(frozen=True)
class HtmlPreflightReport:
    issues: tuple[PreflightIssue, ...]
    units: int
    slides: int
    annotated_elements: int

    @property
    def errors(self) -> tuple[PreflightIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "error")

    @property
    def warnings(self) -> tuple[PreflightIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "warning")

    @property
    def passed(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.issues:
            counts[item.code] = counts.get(item.code, 0) + 1
        return {
            "format": PREFLIGHT_FORMAT,
            "passed": self.passed,
            "summary": {
                "units": self.units,
                "slides": self.slides,
                "annotatedElements": self.annotated_elements,
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "issueCounts": dict(sorted(counts.items())),
            },
            "issues": [item.as_dict() for item in self.issues],
        }

    def raise_for_errors(self) -> None:
        if self.errors:
            raise ValidationError(item.validation_issue() for item in self.errors)


def _is_static_image_fallback(node: _Node) -> bool:
    if node.export_mode == "image":
        return node.fallback_scope == "whole"
    return node.element_type == "canvas"


def _issue(
    severity: str, code: str, unit: SourceUnit, node: _Node, message: str, fix: str,
    *, ancestor: _Node | None = None,
) -> PreflightIssue:
    return PreflightIssue(
        severity=severity,
        code=code,
        unit_id=unit.unit_id,
        source=unit.html_path.name,
        slide_id=node.slide_id,
        element_id=node.element_id,
        ancestor_id=ancestor.element_id if ancestor else None,
        message=message,
        fix=fix,
    )


def _annotated_nodes(parser: _StructureParser) -> list[_Node]:
    result: list[_Node] = []

    def visit(node: _Node) -> None:
        if node.annotated:
            result.append(node)
        for child in node.children:
            visit(child)

    for root in parser.roots:
        visit(root)
    return result


def analyze_html_sources(
    units: Iterable[SourceUnit], registry: dict[str, Any],
) -> HtmlPreflightReport:
    """Inspect annotation structure without launching a browser or changing sources."""

    source_units = list(units)
    protected_ids = set(registry.get("protected", {}).get("elementIds", []))
    issues: list[PreflightIssue] = []
    slide_count = 0
    annotated_count = 0
    global_slide_ids: set[str] = set()

    for unit in source_units:
        parser = _StructureParser()
        parser.feed(unit.html_path.read_text(encoding="utf-8-sig"))
        parser.close()
        annotated = _annotated_nodes(parser)
        annotated_count += len(annotated)
        slide_count += len(parser.slides)

        for slide in parser.slides:
            if not slide.slide_id:
                issues.append(_issue(
                    "error", "missing-slide-id", unit, slide,
                    "A section.slide has no stable data-slide-id.",
                    "Add a unique data-slide-id before conversion.",
                ))
            elif slide.slide_id in global_slide_ids:
                issues.append(_issue(
                    "error", "duplicate-slide-id", unit, slide,
                    f"Slide id {slide.slide_id!r} is duplicated.",
                    "Use a globally unique data-slide-id.",
                ))
            else:
                global_slide_ids.add(slide.slide_id)

        ids_by_slide: dict[str | None, set[str]] = {}
        for node in annotated:
            if node.slide_id is None:
                issues.append(_issue(
                    "error", "annotation-outside-slide", unit, node,
                    "A Bento conversion annotation is outside section.slide[data-slide-id].",
                    "Move the annotated element inside exactly one identified slide.",
                ))
                continue
            if node.element_id:
                seen = ids_by_slide.setdefault(node.slide_id, set())
                if node.element_id in seen:
                    issues.append(_issue(
                        "error", "duplicate-element-id", unit, node,
                        f"Element id {node.element_id!r} is duplicated within the slide.",
                        "Use an id unique within this slide.",
                    ))
                else:
                    seen.add(node.element_id)
            elif node.export_mode in {"image", "svg"}:
                issues.append(_issue(
                    "error", "fallback-without-stable-id", unit, node,
                    "An explicit fallback has no stable data-bento-id.",
                    "Add data-bento-id so the fallback capture is deterministic.",
                ))
            else:
                issues.append(_issue(
                    "warning", "annotated-without-stable-id", unit, node,
                    "An explicitly annotated element relies on a generated id.",
                    "Add a stable data-bento-id for reproducible conversion and references.",
                ))

            descendants = node.annotated_descendants()
            if node.export_mode == "image" and node.fallback_scope not in {"background", "whole"}:
                issues.append(_issue(
                    "error", "invalid-fallback-scope", unit, node,
                    f"Fallback scope {node.fallback_scope!r} is unsupported.",
                    "Use data-bento-fallback-scope=background or whole.",
                ))
            if (
                node.export_mode == "image"
                and node.fallback_scope == "whole"
                and (node.rendered_text() or descendants)
            ):
                issues.append(_issue(
                    "error", "whole-fallback-covers-editable-content", unit, node,
                    "A whole-element image fallback would bake rendered text or annotated descendants into pixels.",
                    "Use background fallback scope so text, equations, tables, and annotated children remain editable.",
                ))
            annotated_ancestors: list[_Node] = []
            ancestor = node.parent
            while ancestor is not None:
                if ancestor.annotated:
                    annotated_ancestors.append(ancestor)
                ancestor = ancestor.parent
            covering = next((item for item in annotated_ancestors if _is_static_image_fallback(item)), None)

            if covering is None and node.element_type == "shape" and node.rendered_text():
                issues.append(_issue(
                    "warning", "text-bearing-shape", unit, node,
                    "A shape contains rendered text and will be decomposed into a visual background plus editable text.",
                    "Review the generated native text overlay and its alignment after conversion.",
                ))
            if covering is None and node.element_type == "text" and descendants:
                issues.append(_issue(
                    "warning", "annotated-group", unit, node,
                    "A text/group annotation contains separately annotated descendants.",
                    "The container will contribute only its own residual text/background; descendants remain independent.",
                ))
            if covering is None and node.export_mode == "image":
                issues.append(_issue(
                    "warning", "explicit-image-fallback", unit, node,
                    "This element uses a raster fallback for its visual layer.",
                    "Keep background scope when it contains editable text, equations, tables, or annotated descendants.",
                ))

            if annotated_ancestors:
                nearest = annotated_ancestors[0]
                issues.append(_issue(
                    "warning", "nested-conversion-annotation", unit, node,
                    "A conversion annotation is nested inside another annotated element.",
                    "Prefer sibling native elements, or keep only the smallest intended fallback annotation.",
                    ancestor=nearest,
                ))
                if covering is not None and node.element_id in protected_ids:
                    issues.append(_issue(
                        "error", "protected-element-covered", unit, node,
                        "A protected element would be absorbed into an ancestor image fallback.",
                        "Keep the protected element independently convertible outside the fallback ancestor.",
                        ancestor=covering,
                    ))

    issues.sort(key=lambda item: (
        item.source, item.slide_id or "", item.element_id or "", item.code, item.ancestor_id or "",
    ))
    return HtmlPreflightReport(tuple(issues), len(source_units), slide_count, annotated_count)


def write_preflight_report(path: str | Path, report: HtmlPreflightReport) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(report.as_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
