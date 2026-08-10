"""Canonical single-HTML section validation and approval digests."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from .errors import BentoConverterError
from .registry_document import REGISTRY_V1, REGISTRY_V2, validate_registry, validate_registry_asset_content
from .segment import registry_dependency_closure


SECTION_DIGEST_FORMAT = "bento/section-approval/v1"
REFERENCE_ATTRIBUTES = {
    "data-equation-id": "equations",
    "data-figure-id": "figures",
    "data-chart-id": "charts",
    "data-table-id": "tables",
    "data-asset-id": "assets",
}
URL_ATTRIBUTES = {"src", "href", "poster", "data-src"}
URL_PATTERN = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}


@dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str | None]
    children: list["HtmlNode | str"] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("#document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = HtmlNode(tag.lower(), {key.lower(): value for key, value in attrs})
        self.stack[-1].children.append(node)
        if node.tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = HtmlNode(tag.lower(), {key.lower(): value for key, value in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == normalized:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data.replace("\r\n", "\n").replace("\r", "\n"))


@dataclass(frozen=True)
class SectionApprovalEvidence:
    section_id: str
    slide_ids: tuple[str, ...]
    digest: str
    asset_hashes: dict[str, str]
    registry_references: dict[str, tuple[str, ...]]
    global_css_digest: str


@dataclass(frozen=True)
class HtmlDeckStructureEvidence:
    """Canonical slide-level structure used for reviewed HTML change proposals."""

    ordered_slide_ids: tuple[str, ...]
    slide_section_ids: dict[str, str]
    slide_digests: dict[str, str]
    slide_titles: dict[str, str]
    section_digests: dict[str, str]
    global_css_digest: str


def _walk(node: HtmlNode) -> Iterable[HtmlNode]:
    yield node
    for child in node.children:
        if isinstance(child, HtmlNode):
            yield from _walk(child)


def _canonical_node(node: HtmlNode) -> dict[str, Any]:
    return {
        "tag": node.tag,
        "attrs": [[key, node.attrs[key]] for key in sorted(node.attrs)],
        "children": [
            _canonical_node(child) if isinstance(child, HtmlNode) else child
            for child in node.children
        ],
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _local_path(raw_url: str, *, base: Path, root: Path) -> Path | None:
    value = raw_url.strip()
    if not value or value.startswith("#"):
        return None
    split = urlsplit(value)
    if split.scheme or split.netloc or value.startswith("//"):
        return None
    candidate = (base / unquote(split.path)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise BentoConverterError(f"Section resource escapes the repository: {raw_url}") from exc
    return candidate


def _hash_local_url(raw_url: str, *, base: Path, root: Path) -> tuple[str, str] | None:
    path = _local_path(raw_url, base=base, root=root)
    if path is None:
        return None
    if not path.is_file():
        raise BentoConverterError(f"Referenced section resource does not exist: {path}")
    return path.relative_to(root).as_posix(), _sha256_bytes(path.read_bytes())


def _urls_from_node(node: HtmlNode) -> set[str]:
    result: set[str] = set()
    for item in _walk(node):
        for key, value in item.attrs.items():
            if not value:
                continue
            if key in URL_ATTRIBUTES:
                result.add(value)
            if key == "style":
                result.update(match.group(2) for match in URL_PATTERN.finditer(value))
    return result


def _node_text(node: HtmlNode) -> str:
    return "".join(
        _node_text(child) if isinstance(child, HtmlNode) else child
        for child in node.children
    )


def _slide_title(node: HtmlNode, slide_id: str) -> str:
    for item in _walk(node):
        if item.tag in {"h1", "h2", "h3"}:
            value = " ".join(_node_text(item).split())
            if value:
                return value
    return slide_id


def _global_css_payload(root_node: HtmlNode, *, html_path: Path, repository: Path, registry: dict[str, Any]) -> dict[str, Any]:
    styles: list[str] = []
    linked: dict[str, str] = {}
    css_assets: dict[str, str] = {}
    theme_nodes: list[dict[str, Any]] = []
    for node in _walk(root_node):
        if node.tag == "style":
            styles.append(_node_text(node))
        if node.tag == "link" and "stylesheet" in (node.attrs.get("rel") or "").lower().split():
            href = node.attrs.get("href") or ""
            resolved = _hash_local_url(href, base=html_path.parent, root=repository)
            if resolved:
                relative, digest = resolved
                linked[relative] = digest
                css_text = (repository / relative).read_text(encoding="utf-8-sig")
                for match in URL_PATTERN.finditer(css_text):
                    asset = _hash_local_url(match.group(2), base=(repository / relative).parent, root=repository)
                    if asset:
                        css_assets[asset[0]] = asset[1]
        if node.tag in {"html", "body"} or (node.tag == "main" and "data-bento-deck" in node.attrs):
            theme_nodes.append({"tag": node.tag, "attrs": [[key, node.attrs[key]] for key in sorted(node.attrs)]})
    for style in styles:
        for match in URL_PATTERN.finditer(style):
            asset = _hash_local_url(match.group(2), base=html_path.parent, root=repository)
            if asset:
                css_assets[asset[0]] = asset[1]
    return {
        "styles": styles,
        "linkedStylesheets": linked,
        "stylesheetAssets": css_assets,
        "themeNodes": theme_nodes,
        "registryDocument": registry.get("document", {}),
        "registryTheme": registry.get("theme", {}),
    }


def _registry_projection(nodes: list[HtmlNode], registry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, tuple[str, ...]]]:
    referenced: dict[str, set[str]] = {key: set() for key in REFERENCE_ATTRIBUTES.values()}
    for root in nodes:
        for node in _walk(root):
            for attribute, collection in REFERENCE_ATTRIBUTES.items():
                value = node.attrs.get(attribute)
                if value:
                    referenced[collection].add(value)
                    if attribute == "data-equation-id" and node.attrs.get("data-latex") is not None:
                        definition = registry.get("equations", {}).get(value)
                        expected = definition.get("latex") if isinstance(definition, dict) else None
                        if node.attrs["data-latex"].strip() != str(expected).strip():
                            raise BentoConverterError(f"Equation {value} data-latex does not match registry latex")
    seed_fields = {
        "assets": "assetId", "equations": "equationId", "figures": "figureId",
        "tables": "tableId", "charts": "chartId",
    }
    dependencies = (
        registry_dependency_closure(
            [{seed_fields[collection]: identifier} for collection, ids in referenced.items() for identifier in ids],
            registry,
        )
        if registry.get("format") in {REGISTRY_V1, REGISTRY_V2}
        else {collection: set(ids) for collection, ids in referenced.items()} | {"sources": set()}
    )
    for collection in referenced:
        referenced[collection].update(dependencies.get(collection, set()))
    projection: dict[str, Any] = {}
    for collection, ids in referenced.items():
        definitions = registry.get(collection, {})
        missing = sorted(identifier for identifier in ids if identifier not in definitions)
        if missing:
            raise BentoConverterError(f"Section references missing registry {collection}: {missing}")
        projection[collection] = {identifier: definitions[identifier] for identifier in sorted(ids)}
    source_ids = dependencies.get("sources", set())
    sources = registry.get("sources", {})
    missing_sources = sorted(source_id for source_id in source_ids if source_id not in sources)
    if missing_sources:
        raise BentoConverterError(f"Section provenance references missing registry sources: {missing_sources}")
    projection["sources"] = {source_id: sources[source_id] for source_id in sorted(source_ids)}
    return projection, {key: tuple(sorted(value)) for key, value in referenced.items() if value}


def compute_section_approval_evidence(
    html_path: str | Path,
    registry: dict[str, Any],
    *,
    repository: str | Path,
) -> dict[str, SectionApprovalEvidence]:
    """Validate a single deck and compute deterministic approval evidence per section."""

    source = Path(html_path).resolve()
    root = Path(repository).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise BentoConverterError(f"Single HTML source escapes the repository: {source}") from exc
    if not source.is_file():
        raise BentoConverterError(f"Single HTML source does not exist: {source}")
    validate_registry(registry, allow_v1=True)
    validate_registry_asset_content(registry, asset_base=source.parent)
    parser = _DocumentParser()
    try:
        parser.feed(source.read_text(encoding="utf-8-sig"))
        parser.close()
    except (OSError, UnicodeDecodeError) as exc:
        raise BentoConverterError(f"Cannot read single HTML source {source}: {exc}") from exc

    sections: dict[str, list[HtmlNode]] = {}
    slide_ids: set[str] = set()
    section_slides: dict[str, list[str]] = {}
    element_ids: set[str] = set()
    for node in _walk(parser.root):
        slide_id = node.attrs.get("data-slide-id")
        if not slide_id:
            continue
        if node.tag != "section":
            raise BentoConverterError(f"data-slide-id={slide_id!r} must be on a section element")
        section_id = node.attrs.get("data-section-id")
        if not section_id:
            raise BentoConverterError(f"Slide {slide_id!r} has no data-section-id")
        if slide_id in slide_ids:
            raise BentoConverterError(f"Duplicate slide id in single HTML: {slide_id}")
        slide_ids.add(slide_id)
        slide_elements = [
            item.attrs["data-bento-id"] for item in _walk(node)
            if item.attrs.get("data-bento-id")
        ]
        duplicates = sorted({item for item in slide_elements if slide_elements.count(item) > 1})
        if duplicates:
            raise BentoConverterError(f"Duplicate element ids in slide {slide_id}: {duplicates}")
        element_ids.update(slide_elements)
        sections.setdefault(section_id, []).append(node)
        section_slides.setdefault(section_id, []).append(slide_id)
    if not sections:
        raise BentoConverterError("Single HTML contains no section[data-slide-id][data-section-id] slides")
    protected = registry.get("protected", {})
    missing_slides = sorted(set(protected.get("slideIds", [])) - slide_ids)
    if missing_slides:
        raise BentoConverterError(f"Registry-protected slides are absent from single HTML: {missing_slides}")
    missing_elements = sorted(set(protected.get("elementIds", [])) - element_ids)
    if missing_elements:
        raise BentoConverterError(f"Registry-protected elements are absent from single HTML: {missing_elements}")
    full_text = _node_text(parser.root)
    missing_text = [value for value in protected.get("requiredText", []) if value not in full_text]
    if missing_text:
        raise BentoConverterError(f"Registry-protected required text is absent from single HTML: {missing_text}")

    global_payload = _global_css_payload(parser.root, html_path=source, repository=root, registry=registry)
    global_css_digest = _sha256_bytes(_canonical_json(global_payload).encode("utf-8"))
    evidence: dict[str, SectionApprovalEvidence] = {}
    for section_id, nodes in sections.items():
        projection, references = _registry_projection(nodes, registry)
        asset_hashes: dict[str, str] = {}
        for node in nodes:
            for url in _urls_from_node(node):
                resolved = _hash_local_url(url, base=source.parent, root=root)
                if resolved:
                    asset_hashes[resolved[0]] = resolved[1]
        for asset_id in references.get("assets", ()):
            definition = registry.get("assets", {}).get(asset_id, {})
            if isinstance(definition, dict) and isinstance(definition.get("path"), str):
                resolved = _hash_local_url(definition["path"], base=source.parent, root=root)
                if resolved:
                    asset_hashes[resolved[0]] = resolved[1]
        digest_payload = {
            "format": SECTION_DIGEST_FORMAT,
            "sectionId": section_id,
            "dom": [_canonical_node(node) for node in nodes],
            "registry": projection,
            "assets": asset_hashes,
            "globalCssDigest": global_css_digest,
        }
        digest = _sha256_bytes((SECTION_DIGEST_FORMAT + "\0" + _canonical_json(digest_payload)).encode("utf-8"))
        evidence[section_id] = SectionApprovalEvidence(
            section_id, tuple(section_slides[section_id]), digest, asset_hashes, references, global_css_digest,
        )
    return evidence


def compute_html_deck_structure_evidence(
    html_path: str | Path,
    registry: dict[str, Any],
    *,
    repository: str | Path,
) -> HtmlDeckStructureEvidence:
    """Return deterministic per-slide evidence after full section validation."""

    source = Path(html_path).resolve()
    evidence = compute_section_approval_evidence(source, registry, repository=repository)
    parser = _DocumentParser()
    try:
        parser.feed(source.read_text(encoding="utf-8-sig"))
        parser.close()
    except (OSError, UnicodeDecodeError) as exc:
        raise BentoConverterError(f"Cannot read single HTML source {source}: {exc}") from exc

    ordered: list[str] = []
    slide_sections: dict[str, str] = {}
    slide_digests: dict[str, str] = {}
    slide_titles: dict[str, str] = {}
    for node in _walk(parser.root):
        slide_id = node.attrs.get("data-slide-id")
        if not slide_id:
            continue
        section_id = node.attrs.get("data-section-id")
        if not section_id:  # compute_section_approval_evidence already reports this precisely.
            raise BentoConverterError(f"Slide {slide_id!r} has no data-section-id")
        ordered.append(slide_id)
        slide_sections[slide_id] = section_id
        slide_digests[slide_id] = _sha256_bytes(
            _canonical_json(_canonical_node(node)).encode("utf-8")
        )
        slide_titles[slide_id] = _slide_title(node, slide_id)

    return HtmlDeckStructureEvidence(
        ordered_slide_ids=tuple(ordered),
        slide_section_ids=slide_sections,
        slide_digests=slide_digests,
        slide_titles=slide_titles,
        section_digests={section_id: item.digest for section_id, item in evidence.items()},
        global_css_digest=next(iter(evidence.values())).global_css_digest,
    )
