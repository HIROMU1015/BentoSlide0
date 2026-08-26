from __future__ import annotations

import json
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from bento_converter.github_source import (
    GitHubRepositoryFile,
    GitHubRepositorySnapshot,
    GitHubSourceError,
    build_github_snapshot,
    descriptor_payload,
    load_and_validate_descriptor,
    normalize_github_repository,
)
from scripts.deck_workflow import (
    WorkflowError,
    _normalize_handoff,
    command_configure_sections,
    command_capture_github_source,
    command_initialize,
    command_submit_plan,
    load_source_manifest,
    load_state,
)


ROOT = Path(__file__).resolve().parents[1]


def snapshot(*, commit: str = "a" * 40) -> GitHubRepositorySnapshot:
    return GitHubRepositorySnapshot(
        repository="example/project",
        canonical_url="https://github.com/example/project.git",
        requested_ref="main",
        resolved_commit=commit,
        commit_time="2026-08-24T00:00:00+00:00",
        include_patterns=("README.md", "src/**"),
        exclude_patterns=(),
        files=(
            GitHubRepositoryFile("README.md", b"# Example\n"),
            GitHubRepositoryFile("src/main.py", b"print('example')\n"),
        ),
        skipped=({"path": ".env", "reason": "secret-like environment file"},),
    )


class GitHubSourceUnitTests(unittest.TestCase):
    def test_repository_url_is_canonical_and_never_accepts_credentials(self) -> None:
        self.assertEqual(
            normalize_github_repository("example/project"),
            ("https://github.com/example/project.git", "example/project"),
        )
        self.assertEqual(
            normalize_github_repository("https://github.com/example/project.git"),
            ("https://github.com/example/project.git", "example/project"),
        )
        for unsafe in (
            "http://github.com/example/project",
            "https://token@github.com/example/project",
            "https://gitlab.com/example/project",
            "https://github.com/example/project/tree/main",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(GitHubSourceError):
                normalize_github_repository(unsafe)

    def test_snapshot_selection_excludes_secrets_generated_paths_and_unsupported_entries(self) -> None:
        payloads = {
            "README.md": b"# Example\n",
            "docs/guide.md": b"guide\n",
            "src/main.py": b"print('ok')\n",
            "src/binary.bin": b"\0binary",
            "src/lfs.dat": b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\n",
        }
        result = build_github_snapshot(
            canonical_url="https://github.com/example/project.git",
            repository="example/project",
            requested_ref="main",
            resolved_commit="b" * 40,
            commit_time="2026-08-24T00:00:00+00:00",
            entries=(
                ("100644", "README.md"),
                ("100644", "docs/guide.md"),
                ("100644", "src/main.py"),
                ("100644", "src/binary.bin"),
                ("100644", "src/lfs.dat"),
                ("100644", ".env"),
                ("100644", "dist/bundle.js"),
                ("120000", "docs/link.md"),
                ("100644", "docs\\bad.md"),
                ("100644", "readme.md"),
            ),
            read_blob=payloads.__getitem__,
            include_patterns=("README.md", "docs/**", "src/**", ".env", "dist/**"),
        )
        self.assertEqual([item.path for item in result.files], ["docs/guide.md", "README.md", "src/main.py"])
        reasons = {item["path"]: item["reason"] for item in result.skipped}
        self.assertIn("secret", reasons[".env"])
        self.assertEqual(reasons["dist/bundle.js"], "excluded path")
        self.assertIn("unsupported Git mode", reasons["docs/link.md"])
        self.assertIn("binary content", reasons["src/binary.bin"])
        self.assertIn("LFS", reasons["src/lfs.dat"])
        self.assertIn("not portable", reasons["docs\\bad.md"])
        self.assertIn("collision", reasons["readme.md"])

    def test_descriptor_detects_snapshot_file_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = snapshot()
            local_paths = {
                item.path: f"sources/repositories/example__project/{source.resolved_commit}/files/{item.path}"
                for item in source.files
            }
            for item in source.files:
                destination = root / local_paths[item.path]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(item.payload)
            descriptor = root / "sources/repositories/example__project" / source.resolved_commit / "repository-source.json"
            descriptor.parent.mkdir(parents=True, exist_ok=True)
            descriptor.write_bytes(descriptor_payload(source, local_paths=local_paths))
            loaded = load_and_validate_descriptor(root, descriptor)
            self.assertEqual(loaded["resolvedCommit"], source.resolved_commit)

            tampered_descriptor = json.loads(descriptor.read_text(encoding="utf-8"))
            tampered_descriptor["files"][0]["sourceUrl"] = "https://github.com/example/project/blob/main/README.md"
            descriptor.write_text(json.dumps(tampered_descriptor), encoding="utf-8")
            with self.assertRaisesRegex(GitHubSourceError, "blob URL"):
                load_and_validate_descriptor(root, descriptor)

            descriptor.write_bytes(descriptor_payload(source, local_paths=local_paths))
            (root / local_paths["README.md"]).write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(GitHubSourceError, "changed"):
                load_and_validate_descriptor(root, descriptor)


class GitHubSourceWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "GitHub source 日本語"
        for directory in ("workflow", "sources/private", "planning"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for relative in (
            "REQUEST.md", "workflow/deck.schema.json", "workflow/deck.v1.schema.json",
            "planning/work-log.md",
        ):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        shutil.copy2(ROOT / "tests/fixtures/deck_v2.initialized.yaml", self.root / "deck.yaml")
        (self.root / "sources/private/spec.md").write_text("local evidence\n", encoding="utf-8")
        (self.root / "sources/source-manifest.yaml").write_text(
            yaml.safe_dump({
                "schemaVersion": 1,
                "authorityMode": "single",
                "items": [{
                    "id": "local-source",
                    "path": "sources/private/spec.md",
                    "type": "document",
                    "role": "primary",
                }],
            }, sort_keys=False),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self, *, replace_primary: bool) -> dict[str, object]:
        with mock.patch("scripts.deck_workflow.capture_github_repository", return_value=snapshot()):
            with redirect_stdout(io.StringIO()):
                return command_capture_github_source(
                    self.root,
                    load_state(self.root),
                    repository="https://github.com/example/project",
                    requested_ref="main",
                    include_patterns=("README.md", "src/**"),
                    exclude_patterns=(),
                    source_id=None,
                    role="primary",
                    replace_primary=replace_primary,
                )

    def test_capture_is_transactional_revision_bound_and_repeatable(self) -> None:
        result = self.capture(replace_primary=True)
        self.assertFalse(result["noOp"])
        state = load_state(self.root)
        manifest = load_source_manifest(self.root, state)
        descriptor = next(item for item in manifest["items"] if item["type"] == "github-repository")
        self.assertEqual(descriptor["role"], "primary")
        self.assertEqual(state["project"]["primarySource"], descriptor["path"])
        self.assertEqual(
            next(item for item in manifest["items"] if item["id"] == "local-source")["role"],
            "evidence",
        )
        repository_files = [item for item in manifest["items"] if item.get("repositorySource") == descriptor["id"]]
        self.assertEqual(len(repository_files), 2)
        for item in repository_files:
            self.assertTrue((self.root / item["path"]).is_file())
            self.assertRegex(item["contentDigest"], r"^sha256:[0-9a-f]{64}$")

        work_log_before = (self.root / "planning/work-log.md").read_bytes()
        second = self.capture(replace_primary=True)
        self.assertTrue(second["noOp"])
        self.assertEqual((self.root / "planning/work-log.md").read_bytes(), work_log_before)

        first_file = self.root / repository_files[0]["path"]
        first_file.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "changed"):
            load_state(self.root)

    def test_existing_primary_requires_explicit_replacement(self) -> None:
        manifest_before = (self.root / "sources/source-manifest.yaml").read_bytes()
        state_before = (self.root / "deck.yaml").read_bytes()
        with self.assertRaisesRegex(WorkflowError, "--replace-primary"):
            self.capture(replace_primary=False)
        self.assertEqual((self.root / "sources/source-manifest.yaml").read_bytes(), manifest_before)
        self.assertEqual((self.root / "deck.yaml").read_bytes(), state_before)

    def test_capture_invalidates_revision_bound_plan_review(self) -> None:
        command_initialize(self.root, load_state(self.root))
        for relative, body in (
            ("planning/explanation-policy.md", "# Policy\n\nExplain only verified repository facts.\n"),
            ("planning/story-outline.md", "# Story\n\nIntroduce, explain, and summarize the project.\n"),
            ("planning/slide-plan.md", "# Plan\n\nBuild one coherent whole-deck explanation.\n"),
        ):
            (self.root / relative).write_text(body, encoding="utf-8")
        command_configure_sections(self.root, load_state(self.root), ("workflow",))
        command_submit_plan(self.root, load_state(self.root))

        submitted = load_state(self.root)
        self.assertEqual(submitted["workflow"]["stage"], "awaiting_plan_approval")
        self.assertEqual(submitted["approvals"]["planReview"]["status"], "submitted")

        self.capture(replace_primary=True)
        refreshed = load_state(self.root)
        self.assertEqual(refreshed["workflow"]["stage"], "planning")
        self.assertEqual(refreshed["workflow"]["status"], "in_progress")
        self.assertIsNone(refreshed["approvals"]["planReview"])

    def test_late_stage_is_rejected_before_remote_access(self) -> None:
        state = load_state(self.root)
        state["workflow"].update(
            stage="html_review", status="awaiting_approval", owner="work", sourceOfTruth="html",
        )
        _normalize_handoff(state)
        (self.root / "deck.yaml").write_text(
            yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8",
        )
        with mock.patch("scripts.deck_workflow.capture_github_repository") as capture:
            with self.assertRaisesRegex(WorkflowError, "does not allow"):
                command_capture_github_source(
                    self.root,
                    load_state(self.root),
                    repository="example/project",
                    requested_ref="main",
                    include_patterns=(),
                    exclude_patterns=(),
                    source_id=None,
                    role="primary",
                    replace_primary=True,
                )
        capture.assert_not_called()
