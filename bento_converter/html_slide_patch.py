"""Build a complete HTML candidate by replacing only named slide fragments."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Mapping

from .errors import BentoConverterError
from .section_candidate import Node, Parser, _walk


@dataclass(frozen=True)
class _SlideSpan:
    start: int
    end: int
    slide_id: str
    section_id: str


class _SlideSpanParser(HTMLParser):
    """Locate exact slide source spans without rewriting unrelated HTML bytes."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_offsets = [0]
        for index, character in enumerate(source):
            if character == "\n":
                self.line_offsets.append(index + 1)
        self.section_stack: list[tuple[int, dict[str, str | None]]] = []
        self.spans: dict[str, _SlideSpan] = {}

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def _record(self, start: int, end: int, attrs: dict[str, str | None]) -> None:
        slide_id = attrs.get("data-slide-id")
        if not slide_id:
            return
        section_id = attrs.get("data-section-id")
        if not section_id:
            raise BentoConverterError(f"Slide {slide_id!r} has no data-section-id")
        if slide_id in self.spans:
            raise BentoConverterError(f"Duplicate data-slide-id in HTML source: {slide_id!r}")
        self.spans[slide_id] = _SlideSpan(start, end, slide_id, section_id)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "section":
            self.section_stack.append((self._offset(), dict(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "section":
            return
        start = self._offset()
        raw = self.get_starttag_text() or ""
        self._record(start, start + len(raw), dict(attrs))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "section" or not self.section_stack:
            return
        start, attrs = self.section_stack.pop()
        closing = self._offset()
        close_end = self.source.find(">", closing)
        if close_end < 0:
            raise BentoConverterError("Unterminated closing section tag")
        self._record(start, close_end + 1, attrs)


def _slide_spans(source: str) -> dict[str, _SlideSpan]:
    parser = _SlideSpanParser(source)
    parser.feed(source)
    parser.close()
    if parser.section_stack:
        raise BentoConverterError("Unclosed section element in HTML source")
    return parser.spans


def _slide_nodes(root: Node) -> list[Node]:
    return [
        node for node in _walk(root)
        if node.tag == "section" and node.attrs.get("data-slide-id")
    ]


def _replacement_slide(path: Path, expected_slide_id: str) -> tuple[Node, str]:
    parser = Parser()
    try:
        source = path.read_bytes().decode("utf-8-sig")
        parser.feed(source)
        parser.close()
    except (OSError, UnicodeDecodeError) as exc:
        raise BentoConverterError(f"Cannot read replacement slide {path}: {exc}") from exc
    slides = _slide_nodes(parser.root)
    if len(slides) != 1:
        raise BentoConverterError(
            f"Replacement for {expected_slide_id!r} must contain exactly one section slide"
        )
    slide = slides[0]
    actual = slide.attrs.get("data-slide-id")
    if actual != expected_slide_id:
        raise BentoConverterError(
            f"Replacement slide id differs from its target: expected={expected_slide_id!r}, actual={actual!r}"
        )
    if "slide" not in str(slide.attrs.get("class") or "").split():
        raise BentoConverterError(f"Replacement {expected_slide_id!r} must retain class=\"slide\"")
    if not slide.attrs.get("data-section-id"):
        raise BentoConverterError(f"Replacement {expected_slide_id!r} has no data-section-id")
    spans = _slide_spans(source)
    span = spans.get(expected_slide_id)
    if span is None or len(spans) != 1:
        raise BentoConverterError(
            f"Replacement for {expected_slide_id!r} must contain exactly one section slide"
        )
    return slide, source[span.start:span.end]


def build_localized_html_candidate(
    canonical_html: str | Path,
    replacements: Mapping[str, str | Path],
) -> str:
    """Return the canonical document with only the requested slide nodes replaced."""

    if not replacements:
        raise BentoConverterError("At least one replacement slide is required")
    source = Path(canonical_html)
    try:
        # Binary decode avoids universal-newline rewriting; a UTF-8 BOM is
        # retained as U+FEFF and encoded back to the same bytes by the caller.
        canonical_source = source.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BentoConverterError(f"Cannot read canonical HTML {source}: {exc}") from exc
    locations = _slide_spans(canonical_source)
    unknown = sorted(set(replacements) - set(locations))
    if unknown:
        raise BentoConverterError(f"Localized HTML change references unknown slides: {unknown}")

    patches: list[tuple[_SlideSpan, str]] = []
    for slide_id, fragment_path in replacements.items():
        current = locations[slide_id]
        replacement, replacement_source = _replacement_slide(Path(fragment_path), slide_id)
        current_section = current.section_id
        replacement_section = replacement.attrs.get("data-section-id")
        if replacement_section != current_section:
            raise BentoConverterError(
                "Localized HTML changes cannot move slides between sections: "
                f"{slide_id!r} ({current_section!r} -> {replacement_section!r})"
            )
        patches.append((current, replacement_source))
    result = canonical_source
    for span, replacement_source in sorted(patches, key=lambda item: item[0].start, reverse=True):
        result = result[:span.start] + replacement_source + result[span.end:]
    return result


def parse_replacement_arguments(values: Iterable[str]) -> dict[str, Path]:
    """Parse repeatable SLIDE_ID=PATH command-line values."""

    result: dict[str, Path] = {}
    for value in values:
        slide_id, separator, raw_path = value.partition("=")
        if not separator or not slide_id.strip() or not raw_path.strip():
            raise BentoConverterError("Slide replacements must use SLIDE_ID=PATH")
        slide_id = slide_id.strip()
        if slide_id in result:
            raise BentoConverterError(f"Duplicate replacement slide id: {slide_id!r}")
        result[slide_id] = Path(raw_path.strip())
    return result
