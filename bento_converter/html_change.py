"""Deterministic impact analysis for review-before-apply HTML deck changes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import BentoConverterError
from .registry_document import registry_revision
from .section_approval import HtmlDeckStructureEvidence, compute_html_deck_structure_evidence


HTML_CHANGE_FORMAT = "bento/html-change-proposal/v3"
HTML_CHANGE_DIGEST_FORMAT = "bento/html-change-proposal-digest/v2"
LEGACY_HTML_CHANGE_FORMAT = "bento/html-change-proposal/v2"
LEGACY_HTML_CHANGE_DIGEST_FORMAT = "bento/html-change-proposal-digest/v1"
HTML_CHANGE_SCOPES = {"local", "related", "global"}
PROPOSAL_DIGEST_FIELDS = (
    "format", "proposalId",
    "baseHtmlRevision", "baseRegistryRevision",
    "baseReviewDigest", "baseDependencyRevisions",
    "candidateHtml", "candidateRegistry",
    "candidateHtmlRevision", "candidateRegistryRevision",
    "candidateReviewDigest", "candidateDependencyRevisions",
    "request", "summary", "impactSummary", "scope",
    "requestedSlideIds", "relatedSlideIds", "changedSlideIds", "affectedSlideIds",
    "addedSlideIds", "removedSlideIds", "changedSectionIds", "slideTitles",
    "reordered", "sectionMembershipChanged", "structuralImpact",
    "globalStyleChanged", "registryChanged",
)
LEGACY_PROPOSAL_DIGEST_FIELDS = tuple(
    field for field in PROPOSAL_DIGEST_FIELDS
    if field not in {
        "baseReviewDigest", "baseDependencyRevisions",
        "candidateReviewDigest", "candidateDependencyRevisions",
    }
)


@dataclass(frozen=True)
class HtmlChangeImpact:
    scope: str
    requested_slide_ids: tuple[str, ...]
    related_slide_ids: tuple[str, ...]
    changed_slide_ids: tuple[str, ...]
    affected_slide_ids: tuple[str, ...]
    added_slide_ids: tuple[str, ...]
    removed_slide_ids: tuple[str, ...]
    reordered: bool
    section_membership_changed: bool
    global_style_changed: bool
    registry_changed: bool
    changed_section_ids: tuple[str, ...]
    slide_titles: dict[str, str]

    @property
    def structural_impact(self) -> bool:
        return bool(
            self.added_slide_ids
            or self.removed_slide_ids
            or self.reordered
            or self.section_membership_changed
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "requestedSlideIds": list(self.requested_slide_ids),
            "relatedSlideIds": list(self.related_slide_ids),
            "changedSlideIds": list(self.changed_slide_ids),
            "affectedSlideIds": list(self.affected_slide_ids),
            "addedSlideIds": list(self.added_slide_ids),
            "removedSlideIds": list(self.removed_slide_ids),
            "reordered": self.reordered,
            "sectionMembershipChanged": self.section_membership_changed,
            "structuralImpact": self.structural_impact,
            "globalStyleChanged": self.global_style_changed,
            "registryChanged": self.registry_changed,
            "changedSectionIds": list(self.changed_section_ids),
            "slideTitles": dict(self.slide_titles),
        }


def html_change_proposal_digest(proposal: dict[str, Any]) -> str:
    """Bind human explanation and machine impact to the exact candidate bytes."""

    legacy = proposal.get("format") == LEGACY_HTML_CHANGE_FORMAT
    fields = LEGACY_PROPOSAL_DIGEST_FIELDS if legacy else PROPOSAL_DIGEST_FIELDS
    digest_format = LEGACY_HTML_CHANGE_DIGEST_FORMAT if legacy else HTML_CHANGE_DIGEST_FORMAT
    missing = [field for field in fields if field not in proposal]
    if missing:
        raise BentoConverterError(f"HTML change proposal digest fields are missing: {missing}")
    payload = {
        "format": digest_format,
        "proposal": {field: proposal[field] for field in fields},
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _ordered_union(*values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for collection in values:
        for value in collection:
            if value not in seen:
                result.append(value)
                seen.add(value)
    return tuple(result)


def _changed_sections(
    before: HtmlDeckStructureEvidence,
    after: HtmlDeckStructureEvidence,
) -> tuple[str, ...]:
    order = _ordered_union(before.slide_section_ids.values(), after.slide_section_ids.values())
    return tuple(
        section_id for section_id in order
        if before.section_digests.get(section_id) != after.section_digests.get(section_id)
    )


def analyze_html_change(
    *,
    base_html: str | Path,
    base_registry: dict[str, Any],
    candidate_html: str | Path,
    candidate_registry: dict[str, Any],
    repository: str | Path,
    requested_slide_ids: Iterable[str],
    related_slide_ids: Iterable[str] = (),
) -> HtmlChangeImpact:
    """Validate both decks and conservatively calculate the visible review scope."""

    before = compute_html_deck_structure_evidence(
        base_html, base_registry, repository=repository,
    )
    after = compute_html_deck_structure_evidence(
        candidate_html, candidate_registry, repository=repository,
    )
    requested = _ordered_union(requested_slide_ids)
    related = _ordered_union(related_slide_ids)
    if not requested:
        raise BentoConverterError("An HTML change proposal requires at least one requested slide")
    known = set(before.ordered_slide_ids) | set(after.ordered_slide_ids)
    unknown = sorted((set(requested) | set(related)) - known)
    if unknown:
        raise BentoConverterError(f"HTML change proposal references unknown slides: {unknown}")

    common = set(before.ordered_slide_ids) & set(after.ordered_slide_ids)
    changed_set = {
        slide_id for slide_id in common
        if before.slide_digests[slide_id] != after.slide_digests[slide_id]
    }
    added = tuple(slide_id for slide_id in after.ordered_slide_ids if slide_id not in common)
    removed = tuple(slide_id for slide_id in before.ordered_slide_ids if slide_id not in common)
    changed = _ordered_union(
        (slide_id for slide_id in before.ordered_slide_ids if slide_id in changed_set),
        added,
        removed,
    )
    common_before = tuple(slide_id for slide_id in before.ordered_slide_ids if slide_id in common)
    common_after = tuple(slide_id for slide_id in after.ordered_slide_ids if slide_id in common)
    reordered = common_before != common_after
    membership_changed = any(
        before.slide_section_ids[slide_id] != after.slide_section_ids[slide_id]
        for slide_id in common
    )
    global_style_changed = before.global_css_digest != after.global_css_digest
    registry_changed = registry_revision(base_registry) != registry_revision(candidate_registry)
    changed_sections = _changed_sections(before, after)
    if not (changed or reordered or membership_changed or global_style_changed or registry_changed):
        raise BentoConverterError("HTML change proposal is a no-op")

    all_slides = _ordered_union(before.ordered_slide_ids, after.ordered_slide_ids)
    affected = _ordered_union(requested, related, changed)
    structural = bool(added or removed or reordered or membership_changed)
    if structural or global_style_changed:
        # Ordering, membership, and shared-style changes can alter the reading or
        # rendering of every slide. Review the complete deck conservatively.
        affected = all_slides
    elif registry_changed:
        section_slides = (
            slide_id for slide_id in all_slides
            if (
                before.slide_section_ids.get(slide_id) in changed_sections
                or after.slide_section_ids.get(slide_id) in changed_sections
            )
        )
        affected = _ordered_union(affected, section_slides)

    requested_set = set(requested)
    related_effect = bool(set(changed) - requested_set) or bool(related) or registry_changed
    if structural or global_style_changed:
        scope = "global"
    elif related_effect:
        scope = "related"
    else:
        scope = "local"

    titles = dict(before.slide_titles)
    titles.update(after.slide_titles)
    return HtmlChangeImpact(
        scope=scope,
        requested_slide_ids=requested,
        related_slide_ids=related,
        changed_slide_ids=changed,
        affected_slide_ids=affected,
        added_slide_ids=added,
        removed_slide_ids=removed,
        reordered=reordered,
        section_membership_changed=membership_changed,
        global_style_changed=global_style_changed,
        registry_changed=registry_changed,
        changed_section_ids=changed_sections,
        slide_titles={slide_id: titles.get(slide_id, slide_id) for slide_id in affected},
    )
