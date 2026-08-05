"""Normalize an untrusted imports/ HTML file into the repository's static HTML authoring contract."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

import yaml

from bento_converter.artifact_transaction import ArtifactTransactionStore, recover_repository_transactions
from bento_converter.errors import BentoConverterError
from bento_converter.html_import import normalize_imported_html
from scripts.deck_workflow import (
    STATE_RELATIVE, WorkflowError, load_source_manifest, load_state, repository_root,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, help="Repository root")
    result.add_argument("--input", required=True, type=Path)
    result.add_argument("--slide-selector")
    result.add_argument("--width", type=int, default=1280)
    result.add_argument("--height", type=int, default=720)
    result.add_argument("--copy-assets", action="store_true")
    result.add_argument("--generate-ids", action="store_true")
    result.add_argument("--force", action="store_true", help="Explicitly replace an existing imported deck authoring source")
    return result


def run(args: argparse.Namespace) -> int:
    root = repository_root(args.root)
    recover_repository_transactions(root)
    state = load_state(root)
    if state.get("schemaVersion") != 2:
        raise WorkflowError("HTML import requires deck schema v2")
    allowed_stages = {"initialized", "planning", "awaiting_plan_approval", "html_authoring", "html_review"}
    if state["workflow"]["stage"] not in allowed_stages:
        raise WorkflowError("HTML import is available only before HTML authoring is handed to conversion")
    if args.width < 1 or args.height < 1:
        raise BentoConverterError("Imported slide dimensions must be positive integers")
    input_path = args.input.resolve() if args.input.is_absolute() else (root / args.input).resolve()
    try:
        input_path.relative_to(root / "imports")
    except ValueError as exc:
        raise BentoConverterError("--input must point inside repository imports/") from exc
    if not input_path.is_file():
        raise BentoConverterError(f"Imported HTML does not exist: {input_path}")
    original = input_path.read_bytes()
    try:
        source = original.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BentoConverterError("Imported HTML must be UTF-8") from exc
    normalized, registry, asset_payloads, report = normalize_imported_html(
        source, input_path=input_path, repository=root,
        slide_selector=args.slide_selector, width=args.width, height=args.height,
        copy_assets=args.copy_assets, generate_ids=args.generate_ids,
    )
    html_path = root / "deck/deck.preview.html"
    registry_path = root / "deck/deck.registry.json"
    for path, payload in (
        (html_path, normalized.encode("utf-8")),
        (registry_path, (json.dumps(registry, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")),
    ):
        if path.is_file() and path.read_bytes() != payload and not args.force:
            raise BentoConverterError(f"Import destination already exists; use --force to replace it explicitly: {path}")

    next_state = copy.deepcopy(state)
    next_state["sources"]["authorityMode"] = "imported"
    next_state["authoring"].update({
        "mode": "imported", "entryHtml": "deck/deck.preview.html",
        "registry": "deck/deck.registry.json", "currentSection": "imported-deck",
    })
    next_state["chapters"] = {}
    next_state["sections"] = {
        "imported-deck": {
            "title": "Imported deck", "status": "authoring",
            "slideIds": report["slideIds"], "approvalDigest": None,
        }
    }
    next_state["workflow"]["currentChapter"] = None
    next_state["workflow"]["currentSection"] = "imported-deck"
    if next_state["workflow"]["stage"] in {"html_authoring", "html_review"}:
        next_state["workflow"].update(
            stage="html_authoring", status="in_progress", owner="work", sourceOfTruth="html",
        )
    next_state["handoff"].update(
        readyForCodex=False, readyForBentoAuthoring=False,
        readyForContentReview=False, readyForFinalEditing=False,
    )
    next_state["approvals"]["bentoContent"] = {
        "status": "pending", "documentRevision": None, "registryRevision": None,
        "approvalDigest": None, "approvedAt": None,
    }
    manifest = load_source_manifest(root, state, require_exists=False)
    items = [item for item in manifest["items"] if item.get("id") != "imported-html"]
    items.append({
        "id": "imported-html", "path": input_path.relative_to(root).as_posix(),
        "type": "html", "role": "imported",
    })
    next_manifest = {"schemaVersion": 1, "authorityMode": "imported", "items": items}
    manifest_path = (root / next_state["sources"]["manifest"]).resolve()
    payloads: dict[Path, bytes] = {
        html_path: normalized.encode("utf-8"),
        registry_path: (json.dumps(registry, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"),
        manifest_path: yaml.safe_dump(next_manifest, allow_unicode=True, sort_keys=False).encode("utf-8"),
        root / STATE_RELATIVE: yaml.safe_dump(next_state, allow_unicode=True, sort_keys=False).encode("utf-8"),
    }
    payloads.update({Path(path).resolve(): payload for path, payload in asset_payloads.items()})
    transaction = ArtifactTransactionStore(root, payloads)

    def validate_committed() -> None:
        if hashlib.sha256(input_path.read_bytes()).digest() != hashlib.sha256(original).digest():
            raise WorkflowError("Original imported HTML changed during normalization")
        installed = load_state(root)
        installed_manifest = load_source_manifest(root, installed)
        if installed_manifest != next_manifest:
            raise WorkflowError("Installed source manifest differs from the prepared HTML import")
        if html_path.read_text(encoding="utf-8-sig") != normalized:
            raise WorkflowError("Installed normalized HTML differs from the prepared import")

    result = transaction.commit(
        payloads,
        operation="import-html-deck",
        validate_committed=validate_committed,
        report_path=root / "output/import-report.json", report_payload=report,
    )
    print(json.dumps({
        "transactionId": result["transactionId"], "html": "deck/deck.preview.html",
        "registry": "deck/deck.registry.json", "slides": report["slideIds"],
        "report": "output/import-report.json",
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (BentoConverterError, WorkflowError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
