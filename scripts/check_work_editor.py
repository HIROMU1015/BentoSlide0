"""Create deterministic evidence for Work editor save, conflict, runtime, and sidecar behavior."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from bento_converter.errors import BentoConverterError
from bento_converter.html_document import embed_bento_doc, extract_bento_doc, load_html, runtime_fingerprint
from bento_converter.work_editor_storage import WorkEditorConflict, WorkEditorStorage


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source", required=True, type=Path)
    result.add_argument("--evidence-dir", required=True, type=Path)
    result.add_argument("--registry", type=Path)
    result.add_argument("--output", required=True, type=Path)
    return result


def run(args: argparse.Namespace) -> int:
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    generated = evidence / "presentation.generated.bento.html"
    target = evidence / "presentation.final.bento.html"
    shutil.copy2(args.source.resolve(), generated)
    storage = WorkEditorStorage(source=generated, target=target, registry=args.registry, reset_final=True)
    before_html = load_html(target)
    document = extract_bento_doc(before_html)
    editable = next(
        element for slide in document["slides"] for element in slide["elements"]
        if isinstance(element.get("x"), (int, float)) and element["x"] + element["w"] + 1 <= document["size"]["width"]
    )
    editable["x"] += 1
    base_revision = storage.status()["revision"]
    saved = storage.save_serialized(embed_bento_doc(before_html, document), base_revision=base_revision)
    conflict_passed = False
    try:
        storage.save_serialized(embed_bento_doc(before_html, document), base_revision=base_revision)
    except WorkEditorConflict:
        conflict_passed = True
    final_html = load_html(target)
    sidecar = json.loads(storage.sidecar.read_text(encoding="utf-8"))
    final_document = extract_bento_doc(final_html)
    report = {
        "format": "bento/work-editor-evidence/v1",
        "passed": bool(
            conflict_passed
            and runtime_fingerprint(before_html) == runtime_fingerprint(final_html)
            and final_document == sidecar
            and saved.get("validation") == "pass"
        ),
        "workEditorSaveTest": bool(saved.get("saved")),
        "revisionConflictTest": conflict_passed,
        "runtimeIntegrity": runtime_fingerprint(before_html) == runtime_fingerprint(final_html),
        "sidecarRoundtrip": final_document == sidecar,
        "revision": saved["revision"],
        "backupCount": saved["backupCount"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (BentoConverterError, OSError, ValueError, StopIteration) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
