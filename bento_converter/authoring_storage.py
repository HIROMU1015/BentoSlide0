"""Revisioned three-artifact storage for Bento authoring mode."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .artifact_transaction import ArtifactTransactionStore
from .bento_validator import validate_bento_doc
from .errors import BentoConverterError, ValidationError, issue
from .html_document import assert_runtime_integrity, embed_bento_doc, extract_bento_doc, load_html, runtime_fingerprint, serialize_bento_doc
from .registry_document import REGISTRY_COLLECTIONS, normalize_registry, registry_revision, validate_registry
from .resource_embedding import scan_document_resources
from .work_editor_storage import document_revision


class AuthoringConflict(BentoConverterError):
    """Document or registry base revision is stale."""


REFERENCE_FIELDS = {
    "equationId": "equations", "figureId": "figures", "chartId": "charts",
    "tableId": "tables", "assetId": "assets",
}


def _sidecar_path(target: Path) -> Path:
    name = target.name
    return target.with_name(name[: -len(".bento.html")] + ".bento.json") if name.endswith(".bento.html") else target.with_suffix(".bento.json")


def _elements(document: dict[str, Any]):
    for slide in document.get("slides", []):
        for element in slide.get("elements", []) if isinstance(slide, dict) else []:
            if isinstance(element, dict):
                yield slide, element


def _visible_document_text(document: dict[str, Any]) -> str:
    values = [str(document.get("title", ""))]
    for slide in document.get("slides", []):
        if not isinstance(slide, dict):
            continue
        values.append(str(slide.get("notes", "")))
        for element in slide.get("elements", []):
            if isinstance(element, dict):
                values.append(str(element.get("html", "")))
                values.append(json.dumps(element.get("rows", []), ensure_ascii=False, sort_keys=True))
    return "\n".join(values)


def _reference_sets(document: dict[str, Any]) -> dict[str, set[str]]:
    result = {collection: set() for collection in REFERENCE_FIELDS.values()}
    for _, element in _elements(document):
        for field, collection in REFERENCE_FIELDS.items():
            value = element.get(field)
            if isinstance(value, str) and value:
                result[collection].add(value)
    return result


def _nested_source_ids(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "sourceId" and isinstance(item, str) and item:
                yield item
            yield from _nested_source_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nested_source_ids(item)


def _sensitive_projection(document: dict[str, Any]) -> dict[str, Any]:
    references = {key: sorted(value) for key, value in _reference_sets(document).items()}
    equations: dict[str, Any] = {}
    charts: dict[str, Any] = {}
    tables: dict[str, Any] = {}
    media: dict[str, Any] = {}
    metadata: list[Any] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"paperSource", "provenance", "sourceId", "nonRemovableLogic"}:
                    metadata.append([key, item])
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for slide, element in _elements(document):
        equation_id = element.get("equationId")
        if equation_id:
            equations[str(equation_id)] = element.get("latexSource")
        chart_id = element.get("chartId")
        if chart_id:
            charts[str(chart_id)] = {key: element.get(key) for key in ("option", "preset") if key in element}
        table_id = element.get("tableId")
        if table_id:
            tables[str(table_id)] = element.get("rows")
        if element.get("type") in {"image", "media", "svg"}:
            media[f"{slide.get('id')}:{element.get('id')}"] = {
                key: element.get(key) for key in ("src", "poster", "asset", "assetId") if key in element
            }
        visit(element)
    return {
        "references": references, "equations": equations, "charts": charts, "tables": tables,
        "media": media, "protectedMetadata": sorted(metadata, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)),
    }


def _requires_registry_update(current: dict[str, Any], proposed: dict[str, Any]) -> bool:
    before = _sensitive_projection(current)
    after = _sensitive_projection(proposed)
    for collection, after_ids in after["references"].items():
        if set(after_ids) - set(before["references"].get(collection, [])):
            return True
    return any(before[key] != after[key] for key in ("equations", "charts", "tables", "media", "protectedMetadata"))


def validate_authoring_document(document: dict[str, Any], *, current: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    schema = validate_bento_doc(document)
    validate_registry(registry, allow_v1=False)
    errors: list[str] = []
    definitions = {collection: registry.get(collection, {}) for collection in REFERENCE_FIELDS.values()}
    sources = registry.get("sources", {})
    for slide, element in _elements(document):
        for field, collection in REFERENCE_FIELDS.items():
            reference = element.get(field)
            if reference and reference not in definitions[collection]:
                errors.append(issue(slide_id=slide.get("id"), element_id=element.get("id"), field=field, actual=reference, fix=f"Define it in registry.{collection}."))
        equation_id = element.get("equationId")
        if equation_id and element.get("latexSource") is not None:
            definition = definitions["equations"].get(equation_id, {})
            if not isinstance(definition, dict) or str(definition.get("latex", "")).strip() != str(element["latexSource"]).strip():
                errors.append(issue(slide_id=slide.get("id"), element_id=element.get("id"), field="latexSource", actual="mismatch", fix="Update the equation registry definition in the same transaction."))
        for source_id in _nested_source_ids(element):
            if source_id not in sources:
                errors.append(issue(slide_id=slide.get("id"), element_id=element.get("id"), field="sourceId", actual=source_id, fix="Define the source in registry.sources."))

    protected = registry.get("protected", {})
    slide_ids = {slide.get("id") for slide in document.get("slides", []) if isinstance(slide, dict)}
    element_ids = {element.get("id") for _, element in _elements(document)}
    for slide_id in protected.get("slideIds", []):
        if slide_id not in slide_ids:
            errors.append(issue(slide_id=slide_id, field="protected.slideIds", actual=None, fix="Restore it or update protected metadata explicitly."))
    for element_id in protected.get("elementIds", []):
        if element_id not in element_ids:
            errors.append(issue(element_id=element_id, field="protected.elementIds", actual=None, fix="Restore it or update protected metadata explicitly."))
    full_text = _visible_document_text(document)
    for required in protected.get("requiredText", []):
        if required not in full_text:
            errors.append(issue(field="protected.requiredText", actual=required, fix="Restore it or update protected metadata explicitly."))

    current_slides = {slide.get("id"): slide for slide in current.get("slides", []) if isinstance(slide, dict)}
    proposed_slides = {slide.get("id"): slide for slide in document.get("slides", []) if isinstance(slide, dict)}
    for slide_id in current_slides.keys() & proposed_slides.keys():
        before_elements = current_slides[slide_id].get("elements", [])
        after_elements = proposed_slides[slide_id].get("elements", [])
        before_ids = {item.get("id") for item in before_elements if isinstance(item, dict)}
        after_ids = {item.get("id") for item in after_elements if isinstance(item, dict)}
        for index in range(min(len(before_elements), len(after_elements))):
            before, after = before_elements[index], after_elements[index]
            if not isinstance(before, dict) or not isinstance(after, dict):
                continue
            if (
                before.get("id") != after.get("id")
                and before.get("id") not in after_ids
                and after.get("id") not in before_ids
                and before.get("type") == after.get("type")
            ):
                errors.append(issue(slide_id=slide_id, element_id=after.get("id"), field="id", actual="changed in place", fix="Use an explicit replace operation for ID changes."))
            if before.get("id") == after.get("id") and before.get("type") != after.get("type"):
                errors.append(issue(slide_id=slide_id, element_id=after.get("id"), field="type", actual="changed in place", fix="Use an explicit replace operation for type changes."))

    resource_scan = scan_document_resources(document)
    for unresolved in resource_scan["unresolved"]:
        errors.append(issue(
            slide_id=unresolved["slideId"], element_id=unresolved["elementId"], field=unresolved["field"],
            actual=unresolved["value"], fix="Embed the resource before authoring save.",
        ))
    if errors:
        raise ValidationError(errors)
    return {"schemaWarnings": list(schema.warnings), "resourceScan": resource_scan, "referenceValidation": "pass"}


def _changed_ids(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    before_map = {slide["id"]: slide for slide in before.get("slides", [])}
    after_map = {slide["id"]: slide for slide in after.get("slides", [])}
    return {
        "added": sorted(after_map.keys() - before_map.keys()),
        "removed": sorted(before_map.keys() - after_map.keys()),
        "changed": sorted(
            slide_id for slide_id in before_map.keys() & after_map.keys()
            if before_map[slide_id] != after_map[slide_id]
        ),
    }


def _registry_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for collection in ("sources", *REGISTRY_COLLECTIONS):
        old = before.get(collection, {})
        new = after.get(collection, {})
        result[collection] = {
            "added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
            "changed": sorted(key for key in old.keys() & new.keys() if old[key] != new[key]),
        }
    return result


class AuthoringArtifactStorage:
    """Own generated→authoring initialization and every authoring save."""

    def __init__(
        self, *, source: str | Path, source_registry: str | Path,
        target: str | Path, target_registry: str | Path, repository: str | Path,
        reset_authoring: bool = False,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.editing_mode = "authoring"
        self.source = Path(source).resolve()
        self.source_registry = Path(source_registry).resolve()
        self.target = Path(target).resolve()
        self.sidecar = _sidecar_path(self.target)
        self.registry_path = Path(target_registry).resolve()
        self.report_path = self.target.parent / "authoring-save-report.json"
        self.revisions_dir = self.target.parent / "revisions"
        self.transactions = ArtifactTransactionStore(
            self.repository, (self.target, self.sidecar, self.registry_path),
        )
        self.transactions.recover()
        if not self.source.is_file() or not self.source_registry.is_file():
            raise BentoConverterError("Generated HTML and registry must exist before authoring initialization")
        source_html = load_html(self.source)
        source_document = extract_bento_doc(source_html)
        source_registry_value = normalize_registry(
            json.loads(self.source_registry.read_text(encoding="utf-8-sig")), unit_id="deck",
        )
        validate_authoring_document(source_document, current=source_document, registry=source_registry_value)
        existing = [path.is_file() for path in (self.target, self.sidecar, self.registry_path)]
        if reset_authoring or not any(existing):
            registry_payload = (json.dumps(source_registry_value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            self.transactions.commit(
                {
                    self.target: source_html.encode("utf-8"),
                    self.sidecar: (serialize_bento_doc(source_document) + "\n").encode("utf-8"),
                    self.registry_path: registry_payload,
                },
                operation="authoring-initialize",
                target_document_revision=document_revision(source_document),
                target_registry_revision=registry_revision(source_registry_value),
                report_path=self.report_path,
                report_payload={"operation": "initialize", "validation": "pass", "rollback": False},
            )
        elif not all(existing):
            raise BentoConverterError("Authoring HTML, JSON, and registry must all exist or all be absent")
        self._runtime = runtime_fingerprint(load_html(self.target))
        self._read_current()

    def acquire_writer_lease(self) -> None:
        if not self.transactions.writer_lease.acquired:
            self.transactions.acquire_writer_lease()

    def release_writer_lease(self) -> None:
        self.transactions.release_writer_lease()

    def _read_current(self) -> tuple[str, dict[str, Any], dict[str, Any]]:
        snapshot = self.transactions.read_snapshot((self.target, self.sidecar, self.registry_path))
        if any(snapshot[path] is None for path in (self.target, self.sidecar, self.registry_path)):
            raise BentoConverterError("Authoring artifact snapshot is incomplete")
        html = snapshot[self.target].decode("utf-8-sig")  # type: ignore[union-attr]
        document = extract_bento_doc(html)
        sidecar = json.loads(snapshot[self.sidecar].decode("utf-8-sig"))  # type: ignore[union-attr]
        registry = json.loads(snapshot[self.registry_path].decode("utf-8-sig"))  # type: ignore[union-attr]
        if document != sidecar:
            raise BentoConverterError("Authoring HTML #bento-doc and JSON sidecar differ")
        if runtime_fingerprint(html) != self._runtime:
            raise BentoConverterError("Authoring Bento runtime changed outside #bento-doc")
        validate_authoring_document(document, current=document, registry=registry)
        return html, document, registry

    def status(self) -> dict[str, Any]:
        self.transactions.recover()
        _, document, registry = self._read_current()
        try:
            target = self.target.relative_to(self.repository).as_posix()
        except ValueError:
            target = self.target.name
        return {
            "documentRevision": document_revision(document),
            "registryRevision": registry_revision(registry),
            "revision": document_revision(document), "target": target,
            "runtimeFingerprint": "sha256:" + self._runtime,
            "backupCount": len(self._backups()), "validation": "pass",
            "editingMode": "authoring", "sourceOfTruth": target,
            "repository": str(self.repository),
        }

    def document_response(self) -> dict[str, Any]:
        _, document, registry = self._read_current()
        return {
            "documentRevision": document_revision(document), "registryRevision": registry_revision(registry),
            "revision": document_revision(document), "document": document, "registry": registry,
        }

    def validate_serialized(
        self, serialized_html: str, *, base_document_revision: str,
        base_registry_revision: str, registry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, current_document, current_registry = self._read_current()
        if (
            base_document_revision != document_revision(current_document)
            or base_registry_revision != registry_revision(current_registry)
        ):
            raise AuthoringConflict("Another authoring document or registry revision has already been saved")
        proposed = extract_bento_doc(serialized_html)
        registry_update_required = _requires_registry_update(current_document, proposed)
        proposed_registry = current_registry if registry is None else normalize_registry(
            registry, unit_id=str(registry.get("unitId") or "deck"),
        )
        if registry_update_required and registry is None:
            raise ValidationError(["Authoring document change requires a registry update in the same transaction"])
        if registry_update_required and registry_revision(proposed_registry) == registry_revision(current_registry):
            raise ValidationError(["Registry-sensitive authoring changes require a changed registry revision"])
        validation = validate_authoring_document(proposed, current=current_document, registry=proposed_registry)
        return {
            "documentRevision": document_revision(current_document),
            "registryRevision": registry_revision(current_registry),
            "revision": document_revision(current_document), "validation": "pass", **validation,
        }

    def _backup_prefix(self) -> str:
        name = self.target.name
        return name[: -len(".bento.html")] if name.endswith(".bento.html") else self.target.stem

    def _backups(self) -> list[Path]:
        prefix = re.escape(self._backup_prefix())
        pattern = re.compile(rf"^{prefix}\.rev-(\d{{6}})\.bento\.html$")
        return sorted(path for path in self.revisions_dir.glob("*.bento.html") if pattern.match(path.name)) if self.revisions_dir.is_dir() else []

    def _create_backup(self) -> Path:
        snapshot = self.transactions.read_snapshot((self.target, self.sidecar, self.registry_path))
        if any(snapshot[path] is None for path in (self.target, self.sidecar, self.registry_path)):
            raise BentoConverterError("Cannot back up an incomplete authoring artifact set")
        self.revisions_dir.mkdir(parents=True, exist_ok=True)
        backups = self._backups()
        next_number = max((int(re.search(r"rev-(\d{6})", path.name).group(1)) for path in backups), default=0) + 1
        backup_stem = f"{self._backup_prefix()}.rev-{next_number:06d}"
        html_backup = self.revisions_dir / f"{backup_stem}.bento.html"
        html_backup.write_bytes(snapshot[self.target])  # type: ignore[arg-type]
        (self.revisions_dir / f"{backup_stem}.bento.json").write_bytes(snapshot[self.sidecar])  # type: ignore[arg-type]
        (self.revisions_dir / f"{backup_stem}.registry.json").write_bytes(snapshot[self.registry_path])  # type: ignore[arg-type]
        return html_backup

    def save_serialized(
        self, serialized_html: str, *, base_document_revision: str,
        base_registry_revision: str, registry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_html, current_document, current_registry = self._read_current()
        current_document_revision = document_revision(current_document)
        current_registry_revision = registry_revision(current_registry)
        if base_document_revision != current_document_revision or base_registry_revision != current_registry_revision:
            raise AuthoringConflict("Another authoring document or registry revision has already been saved")
        proposed_document = extract_bento_doc(serialized_html)
        registry_update_required = _requires_registry_update(current_document, proposed_document)
        if registry is None:
            if registry_update_required:
                raise ValidationError(["Authoring document change requires a registry update in the same transaction"])
            proposed_registry = current_registry
        else:
            proposed_registry = normalize_registry(registry, unit_id=str(registry.get("unitId") or "deck"))
            if registry_update_required and registry_revision(proposed_registry) == current_registry_revision:
                raise ValidationError(["Registry-sensitive authoring changes require a changed registry revision"])
        validation = validate_authoring_document(proposed_document, current=current_document, registry=proposed_registry)
        updated_html = embed_bento_doc(current_html, proposed_document)
        assert_runtime_integrity(current_html, updated_html)
        proposed_document_revision = document_revision(proposed_document)
        proposed_registry_revision = registry_revision(proposed_registry)
        report = {
            "operation": "authoring-save", "baseDocumentRevision": current_document_revision,
            "baseRegistryRevision": current_registry_revision, "resultDocumentRevision": proposed_document_revision,
            "resultRegistryRevision": proposed_registry_revision,
            "slides": _changed_ids(current_document, proposed_document),
            "registry": _registry_changes(current_registry, proposed_registry),
            "resourceValidation": validation["resourceScan"], "referenceValidation": "pass", "rollback": False,
        }
        self._create_backup()

        def validate_base() -> None:
            _, installed_document, installed_registry = self._read_current()
            if document_revision(installed_document) != current_document_revision or registry_revision(installed_registry) != current_registry_revision:
                raise AuthoringConflict("Authoring base revisions changed before the transaction began")

        def validate_commit() -> None:
            installed_html = self.target.read_text(encoding="utf-8-sig")
            installed_document = extract_bento_doc(installed_html)
            installed_sidecar = json.loads(self.sidecar.read_text(encoding="utf-8-sig"))
            installed_registry = json.loads(self.registry_path.read_text(encoding="utf-8-sig"))
            if installed_document != installed_sidecar or installed_document != proposed_document:
                raise BentoConverterError("Authoring document artifacts differ after commit")
            if installed_registry != proposed_registry:
                raise BentoConverterError("Authoring registry differs after commit")
            assert_runtime_integrity(current_html, installed_html)
            validate_authoring_document(installed_document, current=current_document, registry=installed_registry)

        transaction = self.transactions.commit(
            {
                self.target: updated_html.encode("utf-8"),
                self.sidecar: (serialize_bento_doc(proposed_document) + "\n").encode("utf-8"),
                self.registry_path: (json.dumps(proposed_registry, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"),
            },
            operation="authoring-save",
            base_document_revision=current_document_revision, base_registry_revision=current_registry_revision,
            target_document_revision=proposed_document_revision, target_registry_revision=proposed_registry_revision,
            validate_base=validate_base, validate_committed=validate_commit,
            report_path=self.report_path, report_payload=report,
        )
        return {
            "documentRevision": proposed_document_revision,
            "registryRevision": proposed_registry_revision,
            "contentApprovalInvalidated": True,
            "transactionId": transaction["transactionId"],
            "validation": "pass",
        }

    def revert(self, *, base_document_revision: str, base_registry_revision: str) -> dict[str, Any]:
        current_html, current_document, current_registry = self._read_current()
        if (
            base_document_revision != document_revision(current_document)
            or base_registry_revision != registry_revision(current_registry)
        ):
            raise AuthoringConflict("Another authoring document or registry revision has already been saved")
        backups = self._backups()
        if not backups:
            raise BentoConverterError("No authoring revision backup is available")
        html_backup = backups[-1]
        backup_stem = html_backup.name[: -len(".bento.html")]
        json_backup = html_backup.parent / f"{backup_stem}.bento.json"
        registry_backup = html_backup.parent / f"{backup_stem}.registry.json"
        backup_html = html_backup.read_text(encoding="utf-8-sig")
        backup_document = json.loads(json_backup.read_text(encoding="utf-8-sig"))
        backup_registry = json.loads(registry_backup.read_text(encoding="utf-8-sig"))
        if extract_bento_doc(backup_html) != backup_document:
            raise BentoConverterError("Authoring revision backup HTML and JSON differ")
        assert_runtime_integrity(current_html, backup_html)
        validate_authoring_document(backup_document, current=backup_document, registry=backup_registry)
        result_document_revision = document_revision(backup_document)
        result_registry_revision = registry_revision(backup_registry)
        report = {
            "operation": "authoring-revert", "baseDocumentRevision": base_document_revision,
            "baseRegistryRevision": base_registry_revision, "resultDocumentRevision": result_document_revision,
            "resultRegistryRevision": result_registry_revision, "rollback": False,
        }

        def validate_base() -> None:
            _, installed_document, installed_registry = self._read_current()
            if (
                document_revision(installed_document) != base_document_revision
                or registry_revision(installed_registry) != base_registry_revision
            ):
                raise AuthoringConflict("Authoring base revisions changed before the revert transaction began")

        transaction = self.transactions.commit(
            {
                self.target: backup_html.encode("utf-8"),
                self.sidecar: (serialize_bento_doc(backup_document) + "\n").encode("utf-8"),
                self.registry_path: (json.dumps(backup_registry, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"),
            },
            operation="authoring-revert", base_document_revision=base_document_revision,
            base_registry_revision=base_registry_revision, target_document_revision=result_document_revision,
            target_registry_revision=result_registry_revision, validate_base=validate_base,
            report_path=self.report_path, report_payload=report,
        )
        for path in (html_backup, json_backup, registry_backup):
            path.unlink(missing_ok=True)
        return {
            "reverted": True, "documentRevision": result_document_revision,
            "registryRevision": result_registry_revision, "revision": result_document_revision,
            "transactionId": transaction["transactionId"], "validation": "pass",
        }
