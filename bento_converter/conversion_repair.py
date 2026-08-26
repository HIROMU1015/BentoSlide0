"""Persistent diagnostics and reproducible inputs for converter repair loops."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import BentoConverterError, ValidationError
from .html_slide_patch import _slide_spans
from .section_approval import compute_html_deck_structure_evidence


STATUS_FORMAT = "bento/conversion-repair-status/v1"
DIAGNOSTICS_FORMAT = "bento/conversion-repair-diagnostics/v1"
REPRODUCER_FORMAT = "bento/conversion-reproducer/v1"
ISSUE_PATTERN = re.compile(
    r"^slideId=(?P<slide>.*?); elementId=(?P<element>.*?); "
    r"field=(?P<field>.*?); actual=(?P<actual>.*?); fix=(?P<fix>.*)$",
    re.DOTALL,
)


@dataclass(frozen=True)
class RepairDiagnostic:
    code: str
    phase: str
    message: str
    slide_id: str | None = None
    element_id: str | None = None
    field: str | None = None
    actual: str | None = None
    fix: str | None = None
    repairable: bool = True

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return {
            "code": value.pop("code"),
            "phase": value.pop("phase"),
            "message": value.pop("message"),
            "slideId": value.pop("slide_id"),
            "elementId": value.pop("element_id"),
            **value,
        }


@dataclass(frozen=True)
class FocusedTest:
    test_id: str
    browser: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"testId": self.test_id, "browser": self.browser}


PHASE_TESTS: dict[str, tuple[FocusedTest, ...]] = {
    "discovering": (
        FocusedTest("tests.test_html_first.HtmlSourceTests"),
        FocusedTest("tests.test_registry_document.RegistryDocumentTests"),
    ),
    "preflighting": (
        FocusedTest("tests.test_html_preflight.HtmlPreflightTests"),
        FocusedTest("tests.test_html_first.HtmlSourceTests"),
    ),
    "fingerprinting": (
        FocusedTest("tests.test_section_approval.SingleHtmlWorkflowTests"),
        FocusedTest("tests.test_html_first.HtmlSourceTests"),
    ),
    "extracting-layout": (
        FocusedTest("tests.test_native_compatibility.NativeCompatibilityTests"),
        FocusedTest("tests.test_html_first.HtmlFirstBrowserIntegrationTests", browser=True),
    ),
    "converting": (
        FocusedTest("tests.test_html_first.HtmlConversionTests"),
        FocusedTest("tests.test_native_compatibility.NativeCompatibilityTests"),
        FocusedTest("tests.test_resource_embedding.ResourceEmbeddingTests"),
    ),
    "validating-document": (
        FocusedTest("tests.test_validation.DesignValidationTests"),
        FocusedTest("tests.test_html_first.HtmlConversionTests"),
    ),
    "checking-browser": (
        FocusedTest("tests.test_browser.BrowserIntegrationTests", browser=True),
        FocusedTest("tests.test_work_editor.WorkEditorBrowserTests", browser=True),
    ),
    "comparing-visuals": (
        FocusedTest("tests.test_visual_comparison.VisualComparisonTests"),
        FocusedTest("tests.test_critical_visual.CriticalVisualTests"),
    ),
    "reporting": (
        FocusedTest("tests.test_html_first.HtmlConversionTests"),
    ),
}

PATH_TESTS: tuple[tuple[str, tuple[FocusedTest, ...]], ...] = (
    ("bento_converter/html_preflight.py", PHASE_TESTS["preflighting"]),
    ("bento_converter/html_layout.py", PHASE_TESTS["extracting-layout"]),
    ("bento_converter/native_compatibility.py", PHASE_TESTS["extracting-layout"]),
    ("bento_converter/html_converter.py", PHASE_TESTS["converting"]),
    ("bento_converter/resource_embedding.py", (FocusedTest("tests.test_resource_embedding.ResourceEmbeddingTests"),)),
    ("bento_converter/bento_validator.py", PHASE_TESTS["validating-document"]),
    ("bento_converter/browser_check.py", PHASE_TESTS["checking-browser"]),
    ("bento_converter/browser_harness.py", (FocusedTest("tests.test_browser_harness.BrowserHarnessUnitTests"),) + PHASE_TESTS["checking-browser"]),
    ("bento_converter/visual_comparison.py", PHASE_TESTS["comparing-visuals"]),
    ("bento_converter/incremental_cache.py", (FocusedTest("tests.test_html_first.HtmlFirstBrowserIntegrationTests", browser=True),)),
    ("bento_converter/html_pipeline.py", (FocusedTest("tests.test_html_first.HtmlConversionTests"), FocusedTest("tests.test_html_first.HtmlFirstBrowserIntegrationTests", browser=True))),
)


def _literal(value: str) -> str | None:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        parsed = value
    if parsed is None or parsed == "<document>" or parsed == "<none>":
        return None
    return str(parsed)


def diagnostics_from_exception(exc: BaseException, *, phase: str) -> list[RepairDiagnostic]:
    messages: Sequence[str] = exc.issues if isinstance(exc, ValidationError) else (str(exc),)
    diagnostics: list[RepairDiagnostic] = []
    for index, message in enumerate(messages, start=1):
        match = ISSUE_PATTERN.match(message.strip())
        if match:
            diagnostics.append(RepairDiagnostic(
                code=f"{phase}-issue-{index}",
                phase=phase,
                message=message,
                slide_id=_literal(match.group("slide")),
                element_id=_literal(match.group("element")),
                field=match.group("field").strip() or None,
                actual=match.group("actual").strip() or None,
                fix=match.group("fix").strip() or None,
            ))
        else:
            diagnostics.append(RepairDiagnostic(
                code=f"{phase}-failure-{index}", phase=phase, message=message,
                repairable=True,
            ))
    return diagnostics


def select_focused_tests(
    diagnostics: Iterable[RepairDiagnostic] = (), *, changed_paths: Iterable[str] = (),
) -> list[FocusedTest]:
    selected: list[FocusedTest] = [FocusedTest("tests.test_conversion_repair.ConversionRepairUnitTests")]
    for diagnostic in diagnostics:
        selected.extend(PHASE_TESTS.get(diagnostic.phase, ()))
    normalized = {path.replace("\\", "/").lstrip("./") for path in changed_paths}
    for path, tests in PATH_TESTS:
        if path in normalized:
            selected.extend(tests)
    unique: list[FocusedTest] = []
    seen: set[tuple[str, bool]] = set()
    for item in selected:
        key = (item.test_id, item.browser)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
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


def _relative(repository: Path, path: Path, *, label: str) -> Path:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repository.resolve())
    except ValueError as exc:
        raise BentoConverterError(f"{label} escapes the repository: {resolved}") from exc


def _copy_repository_file(repository: Path, relative: Path, destination: Path) -> dict[str, str]:
    source = (repository / relative).resolve()
    _relative(repository, source, label="Repair input")
    if not source.is_file():
        raise BentoConverterError(f"Repair input is missing: {relative.as_posix()}")
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return {"path": relative.as_posix(), "revision": _sha256(target)}


def _single_slide_html(source: str, slide_id: str) -> str:
    spans = _slide_spans(source)
    if slide_id not in spans:
        raise BentoConverterError(f"Cannot create reproducer for unknown slide: {slide_id}")
    result = source
    for current_id, span in sorted(spans.items(), key=lambda item: item[1].start, reverse=True):
        if current_id != slide_id:
            result = result[:span.start] + result[span.end:]
    return result


def create_single_slide_reproducer(
    *, repository: str | Path, html_path: str | Path, registry_path: str | Path,
    base_path: str | Path, slide_id: str, destination: str | Path,
) -> dict[str, Any]:
    root = Path(repository).resolve()
    html = Path(html_path).resolve()
    registry = Path(registry_path).resolve()
    base = Path(base_path).resolve()
    destination_root = Path(destination).resolve()
    html_relative = _relative(root, html, label="HTML source")
    registry_relative = _relative(root, registry, label="Registry source")
    base_relative = _relative(root, base, label="Bento base")
    registry_value = json.loads(registry.read_text(encoding="utf-8-sig"))
    evidence = compute_html_deck_structure_evidence(html, registry_value, repository=root)
    if slide_id not in evidence.ordered_slide_ids:
        raise BentoConverterError(f"Cannot create reproducer for unknown slide: {slide_id}")

    records = [
        _copy_repository_file(root, registry_relative, destination_root),
        _copy_repository_file(root, base_relative, destination_root),
    ]
    for relative_value in evidence.dependency_hashes:
        relative = Path(relative_value)
        if (root / relative).is_file():
            records.append(_copy_repository_file(root, relative, destination_root))
    target_html = destination_root / html_relative
    target_html.parent.mkdir(parents=True, exist_ok=True)
    target_html.write_text(
        _single_slide_html(html.read_text(encoding="utf-8-sig"), slide_id), encoding="utf-8",
    )
    records.append({"path": html_relative.as_posix(), "revision": _sha256(target_html)})
    manifest = {
        "format": REPRODUCER_FORMAT,
        "slideId": slide_id,
        "html": html_relative.as_posix(),
        "registry": registry_relative.as_posix(),
        "base": base_relative.as_posix(),
        "files": sorted(records, key=lambda item: item["path"]),
        "command": (
            "python -m scripts.build_bento_from_html "
            f"--html {html_relative.as_posix()} --registry {registry_relative.as_posix()} "
            f"--base {base_relative.as_posix()} --output output/reproducer.bento.html"
        ),
    }
    atomic_json(destination_root / "reproducer.json", manifest)
    return manifest


def create_full_deck_reproducer(
    *, repository: str | Path, html_path: str | Path, registry_path: str | Path,
    base_path: str | Path, destination: str | Path,
) -> dict[str, Any]:
    root = Path(repository).resolve()
    html = Path(html_path).resolve()
    registry = Path(registry_path).resolve()
    base = Path(base_path).resolve()
    destination_root = Path(destination).resolve()
    html_relative = _relative(root, html, label="HTML source")
    registry_relative = _relative(root, registry, label="Registry source")
    base_relative = _relative(root, base, label="Bento base")
    records = [
        _copy_repository_file(root, html_relative, destination_root),
        _copy_repository_file(root, registry_relative, destination_root),
        _copy_repository_file(root, base_relative, destination_root),
    ]
    try:
        registry_value = json.loads(registry.read_text(encoding="utf-8-sig"))
        evidence = compute_html_deck_structure_evidence(html, registry_value, repository=root)
        for relative_value in evidence.dependency_hashes:
            relative = Path(relative_value)
            if (root / relative).is_file():
                records.append(_copy_repository_file(root, relative, destination_root))
    except (BentoConverterError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        # Invalid inputs are exactly what this snapshot may need to preserve.
        # The source pair and Bento base remain sufficient to reproduce discovery errors.
        pass
    manifest = {
        "format": REPRODUCER_FORMAT,
        "slideId": None,
        "html": html_relative.as_posix(),
        "registry": registry_relative.as_posix(),
        "base": base_relative.as_posix(),
        "files": sorted(records, key=lambda item: item["path"]),
        "command": (
            "python -m scripts.build_bento_from_html "
            f"--html {html_relative.as_posix()} --registry {registry_relative.as_posix()} "
            f"--base {base_relative.as_posix()} --output output/reproducer.bento.html"
        ),
    }
    atomic_json(destination_root / "reproducer.json", manifest)
    return manifest


def preserve_failure_evidence(current_root: str | Path, destination: str | Path) -> list[str]:
    source = Path(current_root).resolve()
    target = Path(destination).resolve()
    preserved: list[str] = []
    for relative in ("diagnostics", "screenshots", "conversion-report.json"):
        item = source / relative
        if not item.exists():
            continue
        output = target / "evidence" / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            shutil.copytree(item, output, dirs_exist_ok=True)
        else:
            shutil.copy2(item, output)
        preserved.append(relative)
    return preserved
