"""Create a deterministic single-section HTML/registry conversion candidate."""

from __future__ import annotations

import copy
import html
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .errors import BentoConverterError
from .registry_document import REGISTRY_COLLECTIONS, normalize_registry
from .section_approval import REFERENCE_ATTRIBUTES, VOID_TAGS


class Node:
    def __init__(self, tag: str, attrs: list[tuple[str, str | None]] | None = None) -> None:
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.children: list[Node | str] = []


class Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_decl(self, decl: str) -> None:
        self.stack[-1].children.append(f"<!{decl}>")

    def handle_comment(self, data: str) -> None:
        self.stack[-1].children.append(f"<!--{data}-->")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag.lower(), attrs)
        self.stack[-1].children.append(node)
        if node.tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(Node(tag.lower(), attrs))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag.lower():
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def handle_entityref(self, name: str) -> None:
        self.stack[-1].children.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.stack[-1].children.append(f"&#{name};")


def _walk(node: Node) -> Iterable[Node]:
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from _walk(child)


def _serialize(node: Node | str) -> str:
    if isinstance(node, str):
        return node
    if node.tag == "#document":
        return "".join(_serialize(child) for child in node.children)
    attributes = "".join(
        f" {key}" if value is None else f' {key}="{html.escape(value, quote=True)}"'
        for key, value in node.attrs.items()
    )
    opening = f"<{node.tag}{attributes}>"
    if node.tag in VOID_TAGS:
        return opening
    return opening + "".join(_serialize(child) for child in node.children) + f"</{node.tag}>"


def section_candidate(
    html_path: str | Path, registry: dict[str, Any], *, section_id: str,
) -> tuple[str, dict[str, Any], list[str]]:
    """Return HTML and a registry projection containing only one logical section."""

    source = Path(html_path)
    parser = Parser()
    parser.feed(source.read_text(encoding="utf-8-sig"))
    parser.close()
    selected: list[Node] = []
    for parent in list(_walk(parser.root)):
        kept: list[Node | str] = []
        for child in parent.children:
            if isinstance(child, Node) and child.tag == "section" and child.attrs.get("data-slide-id"):
                if child.attrs.get("data-section-id") == section_id:
                    selected.append(child)
                    kept.append(child)
            else:
                kept.append(child)
        parent.children = kept
    if not selected:
        raise BentoConverterError(f"HTML contains no slides for section {section_id!r}")
    slide_ids = [str(node.attrs["data-slide-id"]) for node in selected]
    element_ids = {
        str(node.attrs["data-bento-id"])
        for slide in selected for node in _walk(slide) if node.attrs.get("data-bento-id")
    }
    references: dict[str, set[str]] = {collection: set() for collection in REFERENCE_ATTRIBUTES.values()}
    for slide in selected:
        for node in _walk(slide):
            for attribute, collection in REFERENCE_ATTRIBUTES.items():
                if node.attrs.get(attribute):
                    references[collection].add(str(node.attrs[attribute]))
    normalized = normalize_registry(registry, unit_id=section_id)
    references.setdefault("fonts", set()).update(normalized.get("fonts", {}).keys())
    for definition in normalized.get("fonts", {}).values():
        if isinstance(definition, dict) and isinstance(definition.get("asset"), str):
            references.setdefault("assets", set()).add(definition["asset"])
    projected = copy.deepcopy(normalized)
    source_ids: set[str] = set()
    for collection in REGISTRY_COLLECTIONS:
        wanted = references.get(collection, set())
        definitions = normalized.get(collection, {})
        projected[collection] = {key: copy.deepcopy(definitions[key]) for key in sorted(wanted) if key in definitions}
        for definition in projected[collection].values():
            provenance = definition.get("provenance", {}) if isinstance(definition, dict) else {}
            if isinstance(provenance, dict) and isinstance(provenance.get("sourceId"), str):
                source_ids.add(provenance["sourceId"])
    projected["sources"] = {
        key: copy.deepcopy(normalized.get("sources", {})[key])
        for key in sorted(source_ids) if key in normalized.get("sources", {})
    }
    text = "".join(_serialize(slide) for slide in selected)
    protected = normalized.get("protected", {})
    projected["protected"] = {
        "slideIds": [value for value in protected.get("slideIds", []) if value in slide_ids],
        "elementIds": [value for value in protected.get("elementIds", []) if value in element_ids],
        "requiredText": [value for value in protected.get("requiredText", []) if value in text],
    }
    projected["unitId"] = section_id
    return _serialize(parser.root), projected, slide_ids


def write_section_candidate(
    html_path: str | Path, registry: dict[str, Any], *, section_id: str,
    output_html: str | Path, output_registry: str | Path,
) -> tuple[Path, Path, list[str]]:
    candidate_html, candidate_registry, slide_ids = section_candidate(
        html_path, registry, section_id=section_id,
    )
    html_target = Path(output_html)
    registry_target = Path(output_registry)
    html_target.write_text(candidate_html, encoding="utf-8")
    registry_target.write_text(
        json.dumps(candidate_registry, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return html_target, registry_target, slide_ids
