"""Transactional storage and validation for the localhost Bento Work editor."""

from __future__ import annotations

import copy
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import threading
from functools import wraps
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from .artifact_transaction import ArtifactTransactionStore, file_revision
from .bento_validator import validate_bento_doc
from .errors import BentoConverterError, ValidationError, issue
from .html_document import assert_runtime_integrity, embed_bento_doc, extract_bento_doc, load_html, runtime_fingerprint, serialize_bento_doc
from .resource_embedding import scan_document_resources


class WorkEditorConflict(BentoConverterError):
    """The browser attempted to save over a newer final revision."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def document_revision(document: dict[str, Any]) -> str:
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def document_persistence_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare authored state while ignoring Bento-owned session metadata."""

    return (
        {key: value for key, value in left.items() if key != "collab"}
        == {key: value for key, value in right.items() if key != "collab"}
    )


def _sidecar_path(target: Path) -> Path:
    name = target.name
    if name.endswith(".bento.html"):
        return target.with_name(name[: -len(".bento.html")] + ".bento.json")
    return target.with_suffix(".bento.json")


def _visible_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return html_lib.unescape("".join(parser.parts))


def _document_text(document: dict[str, Any]) -> str:
    values: list[str] = [str(document.get("title", ""))]
    for slide in document.get("slides", []):
        if not isinstance(slide, dict):
            continue
        values.append(str(slide.get("notes", "")))
        for element in slide.get("elements", []):
            if not isinstance(element, dict):
                continue
            if isinstance(element.get("html"), str):
                values.append(_visible_text(element["html"]))
            for row in element.get("rows", []) if isinstance(element.get("rows"), list) else []:
                for cell in row.get("cells", []) if isinstance(row, dict) else []:
                    if isinstance(cell, dict) and isinstance(cell.get("html"), str):
                        values.append(_visible_text(cell["html"]))
    return "\n".join(values)


def _indexed(document: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    slides: dict[str, dict[str, Any]] = {}
    elements: dict[tuple[str, str], dict[str, Any]] = {}
    for slide in document.get("slides", []):
        if not isinstance(slide, dict) or not isinstance(slide.get("id"), str):
            continue
        slides[slide["id"]] = slide
        for element in slide.get("elements", []):
            if isinstance(element, dict) and isinstance(element.get("id"), str):
                elements[(slide["id"], element["id"])] = element
    return slides, elements


def _protected_metadata(value: Any) -> list[str]:
    protected_keys = {"nonRemovableLogic", "paperSource", "figureId", "equationId"}
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in protected_keys:
                found.append(f"{key}={json.dumps(item, ensure_ascii=False, sort_keys=True)}")
            found.extend(_protected_metadata(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_protected_metadata(item))
    return sorted(found)


ALWAYS_PROTECTED_ELEMENT_FIELDS = {
    "id", "type", "equationId", "latexSource", "figureId", "paperSource", "nonRemovableLogic",
    "link", "morphId", "from", "to",
}
CONTENT_FIELDS_BY_TYPE = {
    "text": {"html"},
    "table": {"rows"},
    "chart": {"option", "preset"},
    "image": {"src"},
    "svg": {"asset", "markup"},
    "media": {"src", "poster", "kind"},
    "shape": {"shape", "d", "pathBox", "lineStart", "lineEnd"},
}
PRESENTATION_EDITABLE_ROOT_FIELDS = {"modified", "theme"}
PRESENTATION_EDITABLE_SLIDE_FIELDS = {"background"}
PRESENTATION_EDITABLE_ELEMENT_FIELDS = {
    "x", "y", "w", "h", "z", "zIndex", "rotation", "opacity",
    "shadow", "blur", "blend", "backdropFilter", "fx",
    "fontSize", "fontFamily", "fontWeight", "color", "colorGradient",
    "align", "valign", "lineHeight", "letterSpacing", "textStroke",
    "fill", "fillGradient", "stroke", "strokeWidth", "radius",
    "strokeDash", "strokeStyle", "fit", "style",
}


def protected_content_projection(document: dict[str, Any]) -> dict[str, Any]:
    """Return content/structure fields while excluding permitted presentation edits."""

    root = {
        key: value for key, value in document.items()
        if key not in PRESENTATION_EDITABLE_ROOT_FIELDS | {"slides", "collab"}
    }
    # Bento owns the root collab object and may inject or refresh it at runtime.
    # It is session bookkeeping, not author-authored presentation content.
    slides: list[dict[str, Any]] = []
    for slide in document.get("slides", []):
        if not isinstance(slide, dict):
            slides.append({"invalidSlide": slide})
            continue
        projected_slide = {
            key: value for key, value in slide.items()
            if key not in PRESENTATION_EDITABLE_SLIDE_FIELDS | {"elements"}
        }
        elements = []
        for element in slide.get("elements", []):
            if not isinstance(element, dict):
                elements.append({"invalidElement": element})
                continue
            elements.append({
                key: value for key, value in element.items()
                if key not in PRESENTATION_EDITABLE_ELEMENT_FIELDS
            })
        # Bento uses element array order as z-order, which is an allowed presentation edit.
        projected_slide["elements"] = sorted(
            elements,
            key=lambda item: (str(item.get("id", "")), json.dumps(item, ensure_ascii=False, sort_keys=True)),
        )
        slides.append(projected_slide)
    root["slides"] = slides
    return root


def protected_content_fingerprint(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        protected_content_projection(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_editor_document(
    document: dict[str, Any], *, current: dict[str, Any], registry: dict[str, Any],
    allow_content_edit: bool,
) -> dict[str, Any]:
    """Validate schema, references, protected content, registry IDs, and resources."""

    schema_report = validate_bento_doc(document)
    errors: list[str] = []
    current_slides, current_elements = _indexed(current)
    proposed_slides, proposed_elements = _indexed(document)

    if not allow_content_edit:
        for slide_id in proposed_slides.keys() - current_slides.keys():
            errors.append(issue(
                slide_id=slide_id, field="slide", actual="added",
                fix="Use --allow-content-edit to add final slides.",
            ))
        for slide_id, element_id in proposed_elements.keys() - current_elements.keys():
            errors.append(issue(
                slide_id=slide_id, element_id=element_id, field="element", actual="added",
                fix="Use --allow-content-edit to add final elements.",
            ))

    for slide_id, before in current_slides.items():
        after = proposed_slides.get(slide_id)
        if after is None:
            errors.append(issue(slide_id=slide_id, field="slide", actual=None, fix="Do not remove an existing final slide."))
            continue
        for field in ("stateOf", "transition"):
            if before.get(field) != after.get(field):
                errors.append(issue(slide_id=slide_id, field=field, actual=after.get(field), fix=f"Preserve final reference value {before.get(field)!r}."))
        if not allow_content_edit and before.get("notes") != after.get("notes"):
            errors.append(issue(slide_id=slide_id, field="notes", actual="changed", fix="Use --allow-content-edit to change presenter notes."))

    for (slide_id, element_id), before in current_elements.items():
        after = proposed_elements.get((slide_id, element_id))
        if after is None:
            errors.append(issue(slide_id=slide_id, element_id=element_id, field="element", actual=None, fix="Do not remove existing final content."))
            continue
        fields = set(ALWAYS_PROTECTED_ELEMENT_FIELDS)
        if not allow_content_edit:
            fields.update(CONTENT_FIELDS_BY_TYPE.get(str(before.get("type")), set()))
        for field in sorted(fields):
            if before.get(field) != after.get(field):
                errors.append(issue(
                    slide_id=slide_id, element_id=element_id, field=field, actual="changed",
                    fix="Preserve protected content/reference fields or explicitly enable content editing.",
                ))

    if not allow_content_edit and current.get("title") != document.get("title"):
        errors.append(issue(field="title", actual=document.get("title"), fix="Use --allow-content-edit to change the document title."))
    if _protected_metadata(current) != _protected_metadata(document):
        errors.append(issue(field="protected.metadata", actual="changed", fix="Preserve nonRemovableLogic, paperSource, figureId, and equationId metadata."))

    protected = registry.get("protected", {}) if isinstance(registry.get("protected"), dict) else {}
    for slide_id in protected.get("slideIds", []):
        if slide_id not in proposed_slides:
            errors.append(issue(slide_id=slide_id, field="protected.slideIds", actual=None, fix="Restore the registry-protected slide."))
    proposed_ids = {element_id for _, element_id in proposed_elements}
    for element_id in protected.get("elementIds", []):
        if element_id not in proposed_ids:
            errors.append(issue(element_id=element_id, field="protected.elementIds", actual=None, fix="Restore the registry-protected element."))
    complete_text = _document_text(document)
    for required in protected.get("requiredText", []):
        if required not in complete_text:
            errors.append(issue(field="protected.requiredText", actual=required, fix="Restore the exact registry-protected text."))

    reference_collections = {
        "equationId": "equations", "figureId": "figures", "chartId": "charts", "tableId": "tables",
    }
    for (slide_id, element_id), element in proposed_elements.items():
        for field, collection in reference_collections.items():
            reference = element.get(field)
            definitions = registry.get(collection, {})
            if reference and isinstance(definitions, dict) and definitions and reference not in definitions:
                errors.append(issue(slide_id=slide_id, element_id=element_id, field=field, actual=reference, fix=f"Reference an ID defined in registry.{collection}."))

    resource_scan = scan_document_resources(document)
    for unresolved in resource_scan["unresolved"]:
        errors.append(issue(
            slide_id=unresolved["slideId"], element_id=unresolved["elementId"], field=unresolved["field"],
            actual=unresolved["value"], fix="Embed the resource before saving the final Bento document.",
        ))
    if errors:
        raise ValidationError(errors)
    return {"schemaWarnings": list(schema_report.warnings), "resourceScan": resource_scan}


class WorkEditorStorage:
    """Own the generated/final boundary and all final-file mutations."""

    def __init__(
        self, *, source: str | Path, target: str | Path, registry: str | Path | None = None,
        reset_final: bool = False, allow_content_edit: bool = False, backup_limit: int = 10,
        repository: str | Path | None = None, hold_writer_lease: bool = False,
        baseline_document: dict[str, Any] | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        self.source = Path(source).resolve()
        self.target = Path(target).resolve()
        self.sidecar = _sidecar_path(self.target)
        self.registry_path = Path(registry).resolve() if registry else None
        self.allow_content_edit = allow_content_edit
        self.baseline_document = copy.deepcopy(baseline_document) if baseline_document is not None else None
        self.baseline_fingerprint = (
            protected_content_fingerprint(self.baseline_document)
            if self.baseline_document is not None else None
        )
        self.state_path = Path(state_path).resolve() if state_path is not None else None
        self.editing_mode = "finalization"
        self.backup_limit = max(1, backup_limit)
        self.revisions_dir = self.target.parent / "revisions"
        self.save_report_path = self.target.parent / "save-report.json"
        self._lock = threading.RLock()
        if self.source == self.target:
            raise BentoConverterError("Work editor source and target must be different files.")
        if not self.source.is_file():
            raise BentoConverterError(f"Generated source does not exist: {self.source}")
        self.repository = Path(repository).resolve() if repository else self._infer_repository()
        transaction_artifacts = [self.target, self.sidecar]
        if self.registry_path is not None:
            transaction_artifacts.append(self.registry_path)
        self.transactions = ArtifactTransactionStore(self.repository, transaction_artifacts)
        lease_acquired_here = False
        if hold_writer_lease:
            self.transactions.acquire_writer_lease()
            lease_acquired_here = True
        try:
            self._initialize(reset_final=reset_final)
        except BaseException:
            if lease_acquired_here:
                self.transactions.release_writer_lease()
            raise

    def _initialize(self, *, reset_final: bool) -> None:
        self._assert_workflow_editable()
        self.transactions.recover()
        self.registry = self._load_registry()
        self.target.parent.mkdir(parents=True, exist_ok=True)
        source_html = load_html(self.source)
        source_document = extract_bento_doc(source_html)
        validate_bento_doc(source_document)
        if reset_final or not self.target.is_file():
            if reset_final and self.target.is_file():
                self._create_backup()
            self._install_pair(source_html, source_document)
        target_html = load_html(self.target)
        target_document = extract_bento_doc(target_html)
        comparison = self.baseline_document or target_document
        validate_editor_document(
            target_document, current=comparison, registry=self.registry,
            allow_content_edit=self.allow_content_edit,
        )
        self._assert_baseline(target_document)
        self._runtime = runtime_fingerprint(target_html)
        self._sync_sidecar(target_document)

    def _assert_workflow_editable(self) -> None:
        if self.state_path is None:
            return
        try:
            state = yaml.safe_load(self.state_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise BentoConverterError(f"Cannot verify finalization workflow state: {exc}") from exc
        if not isinstance(state, dict) or state.get("schemaVersion") != 2:
            raise BentoConverterError("Final Work editor workflow guard requires deck schema v2")
        workflow = state.get("workflow", {})
        if workflow.get("stage") != "bento_finalization":
            raise BentoConverterError(
                f"Final Work editor requires 'bento_finalization'; current stage is {workflow.get('stage')!r}"
            )
        approval = state.get("approvals", {}).get("finalBento")
        approval_status = approval.get("status") if isinstance(approval, dict) else approval
        if approval_status != "pending" or not state.get("handoff", {}).get("readyForFinalEditing"):
            raise BentoConverterError("Final Work editor requires pending final approval and an active editing handoff")
        outputs = state.get("outputs", {})
        expected_source_value = outputs.get("authoringHtml") or outputs.get("generatedHtml")
        expected = {
            "source": (self.repository / expected_source_value).resolve() if isinstance(expected_source_value, str) else None,
            "target": (self.repository / outputs["finalHtml"]).resolve() if isinstance(outputs.get("finalHtml"), str) else None,
            "registry": (self.repository / outputs["finalRegistry"]).resolve() if isinstance(outputs.get("finalRegistry"), str) else None,
        }
        actual = {"source": self.source, "target": self.target, "registry": self.registry_path}
        for field in expected:
            if expected[field] != actual[field]:
                raise BentoConverterError(f"Final Work editor {field} does not match deck.yaml outputs")

    def _comparison_document(self, current: dict[str, Any]) -> dict[str, Any]:
        return self.baseline_document or current

    def _assert_baseline(self, document: dict[str, Any]) -> None:
        if (
            not self.allow_content_edit
            and self.baseline_fingerprint is not None
            and protected_content_fingerprint(document) != self.baseline_fingerprint
        ):
            raise ValidationError(["Final content/structure differs from its immutable finalization baseline"])

    def _infer_repository(self) -> Path:
        for parent in (self.target.parent, *self.target.parents):
            if (parent / "deck.yaml").is_file():
                return parent
        values = [str(self.source), str(self.target)]
        if self.registry_path is not None:
            values.append(str(self.registry_path))
        return Path(os.path.commonpath(values)).resolve()

    @property
    def writer_lease_acquired(self) -> bool:
        return self.transactions.writer_lease.acquired

    def acquire_writer_lease(self) -> None:
        if not self.writer_lease_acquired:
            self.transactions.acquire_writer_lease()

    def release_writer_lease(self) -> None:
        self.transactions.release_writer_lease()

    def _load_registry(self) -> dict[str, Any]:
        if self.registry_path is None:
            return {"protected": {}, "equations": {}, "figures": {}, "charts": {}, "tables": {}}
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BentoConverterError(f"Cannot read Work editor registry {self.registry_path}: {exc}") from exc
        if not isinstance(value, dict):
            raise BentoConverterError("Work editor registry root must be an object.")
        return value

    def _install_pair(
        self, html: str, document: dict[str, Any], *, operation: str = "final-install",
        report: dict[str, Any] | None = None, base_document_revision: str | None = None,
    ) -> dict[str, Any]:
        html_payload = html.encode("utf-8")
        json_payload = (serialize_bento_doc(document) + "\n").encode("utf-8")
        expected = {self.target: file_revision(self.target), self.sidecar: file_revision(self.sidecar)}

        def validate_base() -> None:
            if any(file_revision(path) != revision for path, revision in expected.items()):
                raise WorkEditorConflict("Artifact files changed before the transaction lock was acquired")
            if base_document_revision is not None:
                current_document = extract_bento_doc(self.target.read_text(encoding="utf-8-sig"))
                if document_revision(current_document) != base_document_revision:
                    raise WorkEditorConflict("Another edit was saved before the artifact transaction began")

        def validate_committed() -> None:
            installed_html = self.target.read_bytes().decode("utf-8-sig")
            installed_document = extract_bento_doc(installed_html)
            installed_sidecar = json.loads(self.sidecar.read_text(encoding="utf-8-sig"))
            if installed_document != installed_sidecar or installed_document != document:
                raise BentoConverterError("Final HTML and JSON sidecar differ after transactional replacement.")

        return self.transactions.commit(
            {self.target: html_payload, self.sidecar: json_payload},
            operation=operation,
            base_document_revision=base_document_revision,
            target_document_revision=document_revision(document),
            validate_base=validate_base,
            validate_committed=validate_committed,
            report_path=self.save_report_path if report is not None else None,
            report_payload=report,
        )

    def _sync_sidecar(self, document: dict[str, Any]) -> None:
        expected = (serialize_bento_doc(document) + "\n").encode("utf-8")
        if self.sidecar.is_file() and self.sidecar.read_bytes() == expected:
            return
        old_revision = file_revision(self.sidecar)

        def validate_base() -> None:
            if file_revision(self.sidecar) != old_revision:
                raise WorkEditorConflict("Final JSON sidecar changed before synchronization")

        self.transactions.commit(
            {self.sidecar: expected}, operation="final-sidecar-sync",
            target_document_revision=document_revision(document), validate_base=validate_base,
        )

    def _backup_prefix(self) -> str:
        name = self.target.name
        return name[: -len(".bento.html")] if name.endswith(".bento.html") else self.target.stem

    def _backups(self) -> list[Path]:
        prefix = re.escape(self._backup_prefix())
        pattern = re.compile(rf"^{prefix}\.rev-(\d{{6}})\.bento\.html$")
        return sorted(path for path in self.revisions_dir.glob("*.bento.html") if pattern.match(path.name)) if self.revisions_dir.is_dir() else []

    def _create_backup(self) -> Path:
        if not self.target.is_file():
            raise BentoConverterError("Cannot back up a missing final file.")
        self.revisions_dir.mkdir(parents=True, exist_ok=True)
        backups = self._backups()
        next_number = max((int(re.search(r"rev-(\d{6})", path.name).group(1)) for path in backups), default=0) + 1
        prefix = self._backup_prefix()
        html_backup = self.revisions_dir / f"{prefix}.rev-{next_number:06d}.bento.html"
        json_backup = self.revisions_dir / f"{prefix}.rev-{next_number:06d}.bento.json"
        shutil.copy2(self.target, html_backup)
        if self.sidecar.is_file():
            shutil.copy2(self.sidecar, json_backup)
        backups = self._backups()
        for old in backups[:-self.backup_limit]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)
        return html_backup

    def _current(self) -> tuple[str, dict[str, Any]]:
        self._assert_workflow_editable()
        snapshot = self.transactions.read_snapshot((self.target, self.sidecar))
        html_payload = snapshot[self.target]
        sidecar_payload = snapshot[self.sidecar]
        if html_payload is None or sidecar_payload is None:
            raise BentoConverterError("Final HTML and JSON sidecar must both exist")
        try:
            html = html_payload.decode("utf-8-sig")
            sidecar_document = json.loads(sidecar_payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BentoConverterError(f"Cannot read a consistent final artifact snapshot: {exc}") from exc
        if runtime_fingerprint(html) != self._runtime:
            raise BentoConverterError("Final runtime fingerprint changed outside #bento-doc; refusing to continue.")
        document = extract_bento_doc(html)
        if document != sidecar_document:
            raise BentoConverterError("Final HTML #bento-doc and JSON sidecar differ")
        return html, document

    @_locked
    def html_response(self) -> str:
        """Return final HTML from the transaction-consistent artifact snapshot."""

        html, _ = self._current()
        return html

    @_locked
    def status(self) -> dict[str, Any]:
        # A running server owns the writer lease for its lifetime, so no other
        # conforming writer can create a journal between requests. Offline
        # callers without that lease retain the conservative recovery scan.
        if not self.writer_lease_acquired:
            self.transactions.recover()
        _, document = self._current()
        validation = "pass"
        try:
            validate_editor_document(
                document, current=self._comparison_document(document), registry=self.registry,
                allow_content_edit=self.allow_content_edit,
            )
            self._assert_baseline(document)
        except BentoConverterError:
            validation = "fail"
        try:
            display_target = self.target.relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            display_target = self.target.name
        revision = document_revision(document)
        return {
            "target": display_target, "revision": revision,
            "documentRevision": revision,
            "runtimeFingerprint": "sha256:" + self._runtime,
            "backupCount": len(self._backups()), "validation": validation,
            "editingMode": self.editing_mode, "sourceOfTruth": display_target,
            "repository": str(self.repository),
        }

    @_locked
    def document_response(self) -> dict[str, Any]:
        _, document = self._current()
        revision = document_revision(document)
        return {"revision": revision, "documentRevision": revision, "document": document}

    @_locked
    def validate_serialized(self, serialized_html: str) -> dict[str, Any]:
        _, current = self._current()
        document = extract_bento_doc(serialized_html)
        validation = validate_editor_document(
            document, current=self._comparison_document(current), registry=self.registry,
            allow_content_edit=self.allow_content_edit,
        )
        self._assert_baseline(document)
        return {"revision": document_revision(current), "validation": "pass", **validation}

    @_locked
    def save_serialized(self, serialized_html: str, *, base_revision: str) -> dict[str, Any]:
        current_html, current = self._current()
        current_revision = document_revision(current)
        if base_revision != current_revision:
            raise WorkEditorConflict("Another edit has already been saved. Reload the latest final revision.")
        proposed = extract_bento_doc(serialized_html)
        validation = validate_editor_document(
            proposed, current=self._comparison_document(current), registry=self.registry,
            allow_content_edit=self.allow_content_edit,
        )
        self._assert_baseline(proposed)
        updated_html = embed_bento_doc(current_html, proposed)
        assert_runtime_integrity(current_html, updated_html)
        if document_persistence_equal(proposed, current):
            return {
                "saved": True, "noOp": True, "revision": current_revision,
                "runtimeFingerprint": "sha256:" + runtime_fingerprint(current_html),
                "backupCount": len(self._backups()), "validation": "pass",
                "transactionId": None, **validation,
            }
        self._create_backup()
        result = {
            "saved": True, "noOp": False, "revision": document_revision(proposed),
            "runtimeFingerprint": "sha256:" + runtime_fingerprint(updated_html),
            "backupCount": len(self._backups()), "validation": "pass", **validation,
        }
        transaction = self._install_pair(
            updated_html, proposed, operation="final-save", report={"operation": "save", **result},
            base_document_revision=current_revision,
        )
        saved_html = load_html(self.target)
        assert_runtime_integrity(current_html, saved_html)
        result["transactionId"] = transaction["transactionId"]
        return result

    @_locked
    def revert(self, *, base_revision: str) -> dict[str, Any]:
        current_html, current = self._current()
        if base_revision != document_revision(current):
            raise WorkEditorConflict("Another edit has already been saved. Reload before reverting.")
        backups = self._backups()
        if not backups:
            raise BentoConverterError("No Work editor backup is available.")
        backup = backups[-1]
        backup_html = load_html(backup)
        assert_runtime_integrity(current_html, backup_html)
        document = extract_bento_doc(backup_html)
        validate_editor_document(
            document, current=self._comparison_document(current), registry=self.registry,
            allow_content_edit=self.allow_content_edit,
        )
        self._assert_baseline(document)
        reverted_html = embed_bento_doc(current_html, document)
        report = {
            "operation": "revert", "reverted": True, "revision": document_revision(document),
            "runtimeFingerprint": "sha256:" + runtime_fingerprint(reverted_html),
            "backupCount": len(backups) - 1, "validation": "pass",
        }
        transaction = self._install_pair(
            reverted_html, document, operation="final-revert", report=report,
            base_document_revision=base_revision,
        )
        backup.unlink(missing_ok=True)
        backup.with_suffix(".json").unlink(missing_ok=True)
        result = {**report, "transactionId": transaction["transactionId"]}
        result.pop("operation")
        return result
