"""Build an HTML-first deck twice and compare deterministic outputs."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from bento_converter.determinism import canonical_sha256, normalize_evidence, sha256_file
from bento_converter.errors import BentoConverterError
from bento_converter.html_pipeline import build_from_html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, help="Single deck HTML source")
    parser.add_argument("--registry", type=Path, help="Registry paired with --html")
    parser.add_argument("--html-dir", type=Path, help="Directory containing modular HTML sources")
    parser.add_argument("--registry-dir", type=Path, help="Directory containing modular registries")
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def _source_arguments(
    *, html: Path | None = None, registry: Path | None = None,
    html_dir: Path | None = None, registry_dir: Path | None = None,
) -> dict[str, Any]:
    explicit = html is not None or registry is not None
    modular = html_dir is not None or registry_dir is not None
    if explicit == modular:
        raise BentoConverterError("Choose exactly one source form: --html/--registry or --html-dir/--registry-dir.")
    if explicit:
        if html is None or registry is None:
            raise BentoConverterError("Single-file determinism requires both --html and --registry.")
        return {"html_path": html, "registry_path": registry}
    if html_dir is None or registry_dir is None:
        raise BentoConverterError("Modular determinism requires both --html-dir and --registry-dir.")
    return {"html_dir": html_dir, "registry_dir": registry_dir}


def run_check(
    *, base: Path, html: Path | None = None, registry: Path | None = None,
    html_dir: Path | None = None, registry_dir: Path | None = None,
) -> dict[str, object]:
    source_arguments = _source_arguments(
        html=html, registry=registry, html_dir=html_dir, registry_dir=registry_dir,
    )
    with tempfile.TemporaryDirectory(prefix="bento-determinism-a-") as first_dir, tempfile.TemporaryDirectory(prefix="bento-determinism-b-") as second_dir:
        roots = (Path(first_dir), Path(second_dir))
        builds = [
            build_from_html(
                **source_arguments,
                base_path=base,
                output_path=root / "presentation.bento.html",
                browser_check=False,
            )
            for root in roots
        ]
        html_hashes = [sha256_file(build.html_path) for build in builds]
        json_hashes = [sha256_file(build.json_path) for build in builds]
        normalized_reports = [normalize_evidence(build.report, root) for build, root in zip(builds, roots)]
        computed_layouts = [
            normalize_evidence(json.loads((root / "diagnostics" / "computed-layout.json").read_text(encoding="utf-8")), root)
            for root in roots
        ]
        report_hashes = [canonical_sha256(report) for report in normalized_reports]
        layout_hashes = [canonical_sha256(layout) for layout in computed_layouts]
        checks = {
            "rawHtmlIdentical": builds[0].html_path.read_bytes() == builds[1].html_path.read_bytes(),
            "rawBentoJsonIdentical": builds[0].json_path.read_bytes() == builds[1].json_path.read_bytes(),
            "normalizedConversionReportIdentical": normalized_reports[0] == normalized_reports[1],
            "normalizedComputedLayoutIdentical": computed_layouts[0] == computed_layouts[1],
        }
        return {
            "format": "bento/determinism-report/v1",
            "passed": all(checks.values()),
            "checks": checks,
            "sha256": {
                "html": html_hashes,
                "bentoJson": json_hashes,
                "normalizedConversionReport": report_hashes,
                "normalizedComputedLayout": layout_hashes,
            },
        }


def main() -> int:
    args = parse_args()
    report = run_check(
        html=args.html.resolve() if args.html else None,
        registry=args.registry.resolve() if args.registry else None,
        html_dir=args.html_dir.resolve() if args.html_dir else None,
        registry_dir=args.registry_dir.resolve() if args.registry_dir else None,
        base=args.base.resolve(),
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
