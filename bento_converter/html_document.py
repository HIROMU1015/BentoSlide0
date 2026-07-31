"""Locate, extract, and replace Bento's single JSON script block losslessly."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .errors import HtmlDocumentError


@dataclass(frozen=True)
class BentoDocSpan:
    open_start: int
    content_start: int
    content_end: int
    close_end: int


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for index, char in enumerate(text):
        if char == "\n":
            offsets.append(index + 1)
    return offsets


class _BentoDocParser(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.offsets = _line_offsets(source)
        self.spans: list[BentoDocSpan] = []
        self._active: tuple[int, int] | None = None

    def _absolute(self) -> int:
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr_map = {name.lower(): value for name, value in attrs}
        if attr_map.get("id") != "bento-doc" or attr_map.get("type") != "application/bento+json":
            return
        raw = self.get_starttag_text()
        start = self._absolute()
        self._active = (start, start + len(raw))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or self._active is None:
            return
        end_start = self._absolute()
        open_start, content_start = self._active
        close_text = "</script>"
        actual = self.source[end_start : end_start + len(close_text)]
        if actual.lower() != close_text:
            raise HtmlDocumentError("Malformed closing tag for #bento-doc")
        self.spans.append(
            BentoDocSpan(
                open_start=open_start,
                content_start=content_start,
                content_end=end_start,
                close_end=end_start + len(close_text),
            )
        )
        self._active = None


def locate_bento_doc(html: str) -> BentoDocSpan:
    parser = _BentoDocParser(html)
    try:
        parser.feed(html)
        parser.close()
    except HtmlDocumentError:
        raise
    except Exception as exc:
        raise HtmlDocumentError(f"Cannot parse Bento HTML: {exc}") from exc
    if len(parser.spans) != 1:
        raise HtmlDocumentError(
            f"Expected exactly one <script type=\"application/bento+json\" id=\"bento-doc\">, "
            f"found {len(parser.spans)}."
        )
    return parser.spans[0]


def load_html(path: str | Path) -> str:
    html_path = Path(path)
    try:
        return html_path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise HtmlDocumentError(f"Cannot read UTF-8 Bento HTML {html_path}: {exc}") from exc


def extract_bento_doc(html: str) -> dict[str, Any]:
    span = locate_bento_doc(html)
    raw = html[span.content_start : span.content_end].strip()
    if not raw:
        raise HtmlDocumentError("The #bento-doc block is empty.")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HtmlDocumentError(
            f"Invalid JSON in #bento-doc at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise HtmlDocumentError("The #bento-doc JSON root must be an object.")
    return value


def serialize_bento_doc(document: dict[str, Any]) -> str:
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
        allow_nan=False,
    )
    return serialized.replace("<", "\\u003c")


def embed_bento_doc(base_html: str, document: dict[str, Any]) -> str:
    span = locate_bento_doc(base_html)
    serialized = serialize_bento_doc(document)
    return base_html[: span.content_start] + "\n" + serialized + "\n" + base_html[span.content_end :]


def without_bento_doc_content(html: str) -> str:
    span = locate_bento_doc(html)
    return html[: span.content_start] + html[span.content_end :]


def runtime_fingerprint(html: str) -> str:
    runtime = without_bento_doc_content(html).encode("utf-8")
    return hashlib.sha256(runtime).hexdigest()


def assert_runtime_integrity(base_html: str, output_html: str) -> None:
    base_runtime = without_bento_doc_content(base_html)
    output_runtime = without_bento_doc_content(output_html)
    if base_runtime != output_runtime:
        raise HtmlDocumentError(
            "Runtime integrity failed: content outside #bento-doc differs from the base HTML."
        )


def write_embedded_document(
    base_path: str | Path,
    output_path: str | Path,
    document: dict[str, Any],
) -> None:
    base = Path(base_path)
    output = Path(output_path)
    if base.resolve() == output.resolve():
        raise HtmlDocumentError("Output path must differ from the base HTML path.")
    base_html = load_html(base)
    result = embed_bento_doc(base_html, document)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result.encode("utf-8"))

