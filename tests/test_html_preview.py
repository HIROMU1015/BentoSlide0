from __future__ import annotations

import json
import os
import re
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import yaml

from bento_converter.html_change import html_change_proposal_digest
from scripts.deck_workflow import WorkflowError, atomic_write_state, load_state
from scripts.deck_workflow import migrate_v1_state
from scripts.run_html_preview import _index_html, _run_html_preview_action, create_preview_server


ROOT = Path(__file__).resolve().parents[1]


class HtmlPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "workflow").mkdir()
        (self.root / "chapters/assets").mkdir(parents=True)
        shutil.copy2(ROOT / "tests/fixtures/deck_v1.yaml", self.root / "deck.yaml")
        shutil.copy2(ROOT / "workflow/deck.schema.json", self.root / "workflow/deck.schema.json")
        shutil.copy2(ROOT / "workflow/deck.v1.schema.json", self.root / "workflow/deck.v1.schema.json")
        (self.root / "REQUEST.md").write_text("# Request\n", encoding="utf-8")
        (self.root / "chapters/chapter-01.preview.html").write_text(
            '<!doctype html><link rel="stylesheet" href="assets/theme.css"><section data-slide-id="slide-1">Preview</section>',
            encoding="utf-8",
        )
        (self.root / "chapters/assets/theme.css").write_text("body { color: #123456; }\n", encoding="utf-8")
        state = load_state(self.root)
        state["chapters"] = {
            "chapter-01": {
                "html": "chapters/chapter-01.preview.html",
                "registry": "chapters/chapter-01.registry.json",
                "status": "review",
                "visualApproval": "pending",
            }
        }
        state["workflow"].update(
            stage="html_review", status="awaiting_approval", owner="work",
            sourceOfTruth="chapters", currentChapter="chapter-01",
        )
        atomic_write_state(self.root, state)
        self.server = create_preview_server(self.root, port=self.free_port())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    @staticmethod
    def free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def read(self, path: str) -> tuple[int, str, str]:
        with urlopen(self.base + path, timeout=3) as response:
            return response.status, response.headers.get_content_type(), response.read().decode("utf-8")

    def test_index_lists_and_highlights_current_chapter(self) -> None:
        status, content_type, body = self.read("/")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/html")
        self.assertIn("chapter-01.preview.html", body)
        self.assertIn("current", body)
        self.assertIn("html_review", body)

    def test_status_is_dynamic_and_machine_readable(self) -> None:
        status, content_type, body = self.read("/api/status")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json")
        self.assertEqual(payload["format"], "bento/html-preview-status/v1")
        self.assertEqual(Path(payload["repository"]), self.root.resolve())
        self.assertEqual(payload["currentChapter"], "chapter-01")
        self.assertEqual(payload["currentPath"], "chapters/chapter-01.preview.html")
        self.assertEqual(payload["chapters"], ["chapters/chapter-01.preview.html"])

    def test_serves_chapter_html_css_and_head(self) -> None:
        self.assertIn("Preview", self.read("/chapters/chapter-01.preview.html")[2])
        status, content_type, body = self.read("/chapters/assets/theme.css")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/css")
        self.assertIn("#123456", body)
        request = __import__("urllib.request", fromlist=["Request"]).Request(
            self.base + "/chapters/chapter-01.preview.html", method="HEAD"
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")

    def test_traversal_and_non_chapter_files_are_rejected(self) -> None:
        for path in ("/%2e%2e/deck.yaml", "/chapters/%2e%2e/deck.yaml", "/deck.yaml", "/chapters\\..\\deck.yaml"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as captured:
                urlopen(self.base + path, timeout=3)
            self.assertEqual(captured.exception.code, 404)

    def test_external_bind_and_port_conflict_are_rejected(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "127.0.0.1"):
            create_preview_server(self.root, host="0.0.0.0", port=self.free_port())
        with self.assertRaises(OSError):
            other = create_preview_server(self.root, port=self.server.server_port)
            other.server_close()


class SingleHtmlPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "日本語 deck"
        (self.root / "workflow").mkdir(parents=True)
        (self.root / "sources").mkdir()
        (self.root / "deck/assets").mkdir(parents=True)
        shutil.copy2(ROOT / "tests/fixtures/deck_v2.initialized.yaml", self.root / "deck.yaml")
        shutil.copy2(ROOT / "workflow/deck.schema.json", self.root / "workflow/deck.schema.json")
        shutil.copy2(ROOT / "workflow/deck.v1.schema.json", self.root / "workflow/deck.v1.schema.json")
        (self.root / "REQUEST.md").write_text("# Request\n", encoding="utf-8")
        (self.root / "sources/source-manifest.yaml").write_text(
            yaml.safe_dump({"schemaVersion": 1, "authorityMode": "single", "items": []}), encoding="utf-8",
        )
        (self.root / "deck/deck.preview.html").write_text(
            '<section class="slide" data-slide-id="slide-1" data-section-id="intro">Single preview</section>',
            encoding="utf-8",
        )
        (self.root / "deck/assets/theme.css").write_text("body{color:#123456}", encoding="utf-8")
        (self.root / "deck/deck.registry.json").write_text("{}", encoding="utf-8")
        state, _, _ = migrate_v1_state(self.root, load_state(self.root), dry_run=True)
        state["sources"].update(manifest="sources/source-manifest.yaml", authorityMode="single")
        state["authoring"].update(mode="single", entryHtml="deck/deck.preview.html", registry="deck/deck.registry.json", currentSection="intro")
        state["chapters"] = {}
        state["sections"] = {
            "intro": {"title": "導入", "status": "approved", "slideIds": ["slide-1"], "approvalDigest": "sha256:" + "0" * 64},
        }
        state["workflow"].update(
            stage="html_review", status="awaiting_approval", owner="work", sourceOfTruth="html",
            currentChapter=None, currentSection="intro", blockingReason=None, blockedFrom=None,
        )
        atomic_write_state(self.root, state)
        self.server = create_preview_server(self.root, port=HtmlPreviewTests.free_port())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def read(self, path: str) -> tuple[int, str, str]:
        with urlopen(self.base + path, timeout=3) as response:
            return response.status, response.headers.get_content_type(), response.read().decode("utf-8")

    def test_index_status_navigation_and_deck_assets(self) -> None:
        body = self.read("/")[2]
        self.assertIn("deck/deck.preview.html", body)
        self.assertIn("#section=intro", body)
        self.assertIn("#slide=slide-1", body)
        self.assertIn("Reload", body)
        payload = json.loads(self.read("/api/status")[2])
        self.assertEqual(payload["mode"], "single")
        self.assertEqual(payload["currentSection"], "intro")
        self.assertEqual(payload["currentSlide"], "slide-1")
        self.assertEqual(payload["sections"], ["intro"])
        self.assertEqual(payload["slides"], ["slide-1"])
        self.assertIn("Single preview", self.read("/deck/deck.preview.html")[2])
        self.assertIn("#123456", self.read("/deck/assets/theme.css")[2])
        with self.assertRaises(HTTPError) as captured:
            urlopen(self.base + "/deck/%2e%2e/deck.yaml", timeout=3)
        self.assertEqual(captured.exception.code, 404)

    def test_active_whole_deck_change_is_visible_and_candidate_is_previewable(self) -> None:
        candidate = self.root / "deck/.bento-html-change-a1b2c3d4e5f6.candidate.html"
        candidate.write_text(
            '<section class="slide" data-slide-id="slide-1" data-section-id="intro">Candidate preview</section>',
            encoding="utf-8",
        )
        state = load_state(self.root)
        state["authoring"].update({
            "strategy": "whole_deck",
            "currentSection": None,
            "htmlReview": {
                "format": "bento/html-deck-review-baseline/v1",
                "htmlRevision": "sha256:" + "1" * 64,
                "registryRevision": "sha256:" + "2" * 64,
                "evidenceDigest": "sha256:" + "5" * 64,
                "dependencyRevisions": {},
                "source": "completed-authoring",
                "proposalDigest": None,
                "openedAt": "2026-08-10T00:00:00Z",
            },
            "htmlChange": {
                "format": "bento/html-change-proposal/v3",
                "proposalId": "a1b2c3d4e5f6",
                "status": "proposed",
                "baseHtmlRevision": "sha256:" + "1" * 64,
                "baseRegistryRevision": "sha256:" + "2" * 64,
                "baseReviewDigest": "sha256:" + "5" * 64,
                "baseDependencyRevisions": {},
                "candidateHtml": candidate.relative_to(self.root).as_posix(),
                "candidateRegistry": "deck/.bento-html-change-a1b2c3d4e5f6.candidate.registry.json",
                "candidateHtmlRevision": "sha256:" + "3" * 64,
                "candidateRegistryRevision": "sha256:" + "4" * 64,
                "candidateReviewDigest": "sha256:" + "6" * 64,
                "candidateDependencyRevisions": {},
                "proposalPath": "output/html-change-proposals/a1b2c3d4e5f6.json",
                "request": "導入を短くする",
                "summary": "導入スライドの説明を短くします",
                "impactSummary": "導入だけに影響し、他のスライドは変えません",
                "proposalDigest": None,
                "approvedProposalDigest": None,
                "postApplyReview": None,
                "scope": "local",
                "requestedSlideIds": ["slide-1"],
                "relatedSlideIds": [],
                "changedSlideIds": ["slide-1"],
                "affectedSlideIds": ["slide-1"],
                "addedSlideIds": [],
                "removedSlideIds": [],
                "changedSectionIds": ["intro"],
                "slideTitles": {"slide-1": "導入"},
                "reordered": False,
                "sectionMembershipChanged": False,
                "structuralImpact": False,
                "globalStyleChanged": False,
                "registryChanged": False,
                "proposedAt": "2026-08-10T00:00:00Z",
                "approvedAt": None,
                "appliedAt": None,
                "cancelledAt": None,
            },
        })
        proposal = state["authoring"]["htmlChange"]
        proposal["proposalDigest"] = html_change_proposal_digest(proposal)
        state["workflow"].update(currentSection=None, status="awaiting_approval")
        state["sections"]["intro"].update(
            status="html_review", canonical="html", approvalDigest=None,
        )
        atomic_write_state(self.root, state)

        body = self.read("/")[2]
        self.assertIn("変更案の確認", body)
        self.assertIn("表示中：</span><strong id=\"view-indicator-label\">現在案", body)
        self.assertIn('data-view="canonical" data-url="/deck/deck.preview.html" aria-pressed="true"', body)
        self.assertIn('data-view="candidate"', body)
        self.assertIn('id="sidebar-resizer" class="sidebar-resizer" role="separator"', body)
        self.assertIn('id="preview-scale-label">自動</strong>', body)
        self.assertIn('data-slide-number="01 / 01"', body)
        self.assertIn('data-slide-title="導入"', body)
        self.assertIn('impact-badge changed">変更あり', body)
        self.assertIn("他のスライドは変えません", body)
        self.assertIn('id="mark-reviewed"', body)
        self.assertIn('id="mark-needs-work"', body)
        self.assertIn('id="apply-proposal" class="primary-action" type="button" disabled', body)
        self.assertIn("この変更案全体を反映", body)
        self.assertIn('sandbox="allow-same-origin"', body)
        self.assertIn('"endpoint": "/api/html-change/action"', body)
        self.assertIn("Candidate preview", self.read("/deck/.bento-html-change-a1b2c3d4e5f6.candidate.html")[2])
        payload = json.loads(self.read("/api/status")[2])
        self.assertEqual(payload["htmlChange"]["status"], "proposed")
        self.assertEqual(payload["htmlChange"]["affectedSlideIds"], ["slide-1"])

        proposal["status"] = "applied"
        proposal["approvedProposalDigest"] = proposal["proposalDigest"]
        proposal["approvedAt"] = "2026-08-10T00:01:00Z"
        proposal["appliedAt"] = "2026-08-10T00:02:00Z"
        proposal["postApplyReview"] = {"status": "pending"}
        with patch("scripts.run_html_preview.load_state", return_value=state):
            applied_body = _index_html(self.root, action_token="test-token").decode("utf-8")
        self.assertIn("変更案を反映しました", applied_body)
        self.assertIn('id="retry-browser-check"', applied_body)
        self.assertNotIn('id="show-candidate"', applied_body)
        self.assertNotIn('id="apply-proposal"', applied_body)

        proposal["postApplyReview"] = {"status": "checked"}
        with patch("scripts.run_html_preview.load_state", return_value=state):
            checked_body = _index_html(self.root, action_token="test-token").decode("utf-8")
        self.assertIn("自動検証に成功しました", checked_body)
        self.assertIn('id="approve-html-deck"', checked_body)
        self.assertIn("このHTML全体でBentoSlideへ進む", checked_body)

    def test_structural_candidate_uses_version_specific_order_titles_and_numbers(self) -> None:
        canonical = self.root / "deck/deck.preview.html"
        candidate = self.root / "deck/.bento-html-change-abc123def456.candidate.html"
        canonical.write_text(
            """<section class="slide" data-slide-id="removed" data-section-id="intro"><h1>現在案だけ</h1></section>
<section class="slide" data-slide-id="shared-a" data-section-id="intro"><h1>共有A・現在</h1></section>
<section class="slide" data-slide-id="shared-b" data-section-id="details"><h1>共有B・現在</h1></section>""",
            encoding="utf-8",
        )
        candidate.write_text(
            """<section class="slide" data-slide-id="shared-b" data-section-id="new-section"><h1>共有B・変更</h1></section>
<section class="slide" data-slide-id="added" data-section-id="new-section"><h1>変更案だけ</h1></section>
<section class="slide" data-slide-id="shared-a" data-section-id="intro"><h1>共有A・変更</h1></section>""",
            encoding="utf-8",
        )
        (self.root / "deck/.bento-html-change-abc123def456.candidate.registry.json").write_text(
            "{}", encoding="utf-8",
        )
        state = load_state(self.root)
        intro = dict(state["sections"]["intro"])
        state["sections"] = {
            "intro": {**intro, "title": "導入", "slideIds": ["removed", "shared-a"]},
            "details": {**intro, "title": "詳細", "slideIds": ["shared-b"]},
        }
        state["authoring"].update({
            "strategy": "whole_deck",
            "currentSection": None,
            "htmlReview": {
                "format": "bento/html-deck-review-baseline/v1",
                "htmlRevision": "sha256:" + "1" * 64,
                "registryRevision": "sha256:" + "2" * 64,
                "evidenceDigest": "sha256:" + "5" * 64,
                "dependencyRevisions": {},
                "source": "completed-authoring",
                "proposalDigest": None,
                "openedAt": "2026-08-12T00:00:00Z",
            },
            "htmlChange": {
                "format": "bento/html-change-proposal/v3",
                "proposalId": "abc123def456",
                "status": "proposed",
                "baseHtmlRevision": "sha256:" + "1" * 64,
                "baseRegistryRevision": "sha256:" + "2" * 64,
                "baseReviewDigest": "sha256:" + "5" * 64,
                "baseDependencyRevisions": {},
                "candidateHtml": candidate.relative_to(self.root).as_posix(),
                "candidateRegistry": "deck/.bento-html-change-abc123def456.candidate.registry.json",
                "candidateHtmlRevision": "sha256:" + "3" * 64,
                "candidateRegistryRevision": "sha256:" + "4" * 64,
                "candidateReviewDigest": "sha256:" + "6" * 64,
                "candidateDependencyRevisions": {},
                "proposalPath": "output/html-change-proposals/abc123def456.json",
                "request": "追加・削除・並べ替え",
                "summary": "構造を変更します",
                "impactSummary": "全スライドを確認します",
                "proposalDigest": None,
                "approvedProposalDigest": None,
                "postApplyReview": None,
                "scope": "global",
                "requestedSlideIds": ["removed", "shared-a", "shared-b", "added"],
                "relatedSlideIds": [],
                "changedSlideIds": ["removed", "shared-a", "shared-b", "added"],
                "affectedSlideIds": ["removed", "shared-a", "shared-b", "added"],
                "addedSlideIds": ["added"],
                "removedSlideIds": ["removed"],
                "changedSectionIds": ["intro", "details", "new-section"],
                "slideTitles": {
                    "removed": "現在案だけ", "shared-a": "共有A・変更",
                    "shared-b": "共有B・変更", "added": "変更案だけ",
                },
                "reordered": True,
                "sectionMembershipChanged": True,
                "structuralImpact": True,
                "globalStyleChanged": False,
                "registryChanged": False,
                "proposedAt": "2026-08-12T00:00:00Z",
                "approvedAt": None,
                "appliedAt": None,
                "cancelledAt": None,
            },
        })
        proposal = state["authoring"]["htmlChange"]
        proposal["proposalDigest"] = html_change_proposal_digest(proposal)
        state["workflow"].update(currentSection=None, status="awaiting_approval")
        atomic_write_state(self.root, state)

        body = self.read("/")[2]
        canonical_nav = re.search(
            r'<ul class="version-nav" data-nav-kind="slides" data-nav-view="canonical">(.*?)</ul>',
            body, re.DOTALL,
        ).group(1)
        candidate_nav = re.search(
            r'<ul class="version-nav" data-nav-kind="slides" data-nav-view="candidate" hidden>(.*?)</ul>',
            body, re.DOTALL,
        ).group(1)
        self.assertLess(canonical_nav.index("現在案だけ"), canonical_nav.index("共有A・現在"))
        self.assertLess(canonical_nav.index("共有A・現在"), canonical_nav.index("共有B・現在"))
        self.assertLess(candidate_nav.index("共有B・変更"), candidate_nav.index("変更案だけ"))
        self.assertLess(candidate_nav.index("変更案だけ"), candidate_nav.index("共有A・変更"))
        self.assertIn('data-slide-number="01 / 03" data-slide-title="共有B・変更"', candidate_nav)
        self.assertIn('data-slide-number="02 / 03" data-slide-title="変更案だけ"', candidate_nav)

        canonical_review = re.search(
            r'<div class="review-version" data-review-view="canonical">(.*?)</div>', body, re.DOTALL,
        ).group(1)
        candidate_review = re.search(
            r'<div class="review-version" data-review-view="candidate" hidden>(.*?)</div>', body, re.DOTALL,
        ).group(1)
        self.assertLess(canonical_review.index("現在案だけ"), canonical_review.index("共有A・現在"))
        self.assertIn('data-slide-number="変更案のみ"', canonical_review)
        self.assertLess(candidate_review.index("共有B・変更"), candidate_review.index("変更案だけ"))
        self.assertLess(candidate_review.index("変更案だけ"), candidate_review.index("共有A・変更"))
        self.assertIn('data-slide-number="現在案のみ"', candidate_review)

        candidate_sections = re.search(
            r'<ul class="version-nav" data-nav-kind="sections" data-nav-view="candidate" hidden>(.*?)</ul>',
            body, re.DOTALL,
        ).group(1)
        self.assertLess(candidate_sections.index("new-section"), candidate_sections.index("導入"))

    def test_action_endpoint_rejects_missing_origin_and_invalid_token(self) -> None:
        payload = json.dumps({"action": "approve-apply-check"}).encode("utf-8")
        for headers in (
            {"Content-Type": "application/json"},
            {
                "Content-Type": "application/json",
                "Origin": self.base,
                "X-Bento-Preview-Token": "not-the-server-token",
            },
        ):
            request = Request(
                self.base + "/api/html-change/action",
                data=payload,
                headers=headers,
                method="POST",
            )
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as captured:
                urlopen(request, timeout=3)
            self.assertEqual(captured.exception.code, 403)

    def test_action_endpoint_dispatches_only_with_same_origin_token(self) -> None:
        payload = {"action": "approve-apply-check", "confirmed": True}
        request = Request(
            self.base + "/api/html-change/action",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": self.base,
                "X-Bento-Preview-Token": self.server.action_token,
            },
            method="POST",
        )
        expected = {"status": "checked", "stage": "html_review"}
        with patch("scripts.run_html_preview._run_html_preview_action", return_value=expected) as action:
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), expected)
        action.assert_called_once_with(self.root.resolve(), payload)


class HtmlPreviewActionTests(unittest.TestCase):
    @staticmethod
    def proposal(status: str, *, checked: bool = False) -> dict[str, object]:
        review = None
        if status == "applied":
            review = {"status": "checked" if checked else "pending"}
        return {
            "status": status,
            "proposalId": "proposal-1",
            "proposalDigest": "sha256:" + "1" * 64,
            "baseHtmlRevision": "sha256:" + "2" * 64,
            "baseRegistryRevision": "sha256:" + "3" * 64,
            "candidateHtmlRevision": "sha256:" + "4" * 64,
            "candidateRegistryRevision": "sha256:" + "5" * 64,
            "affectedSlideIds": ["slide-a", "slide-b"],
            "postApplyReview": review,
        }

    @staticmethod
    def state(proposal: dict[str, object], *, stage: str = "html_review") -> dict[str, object]:
        return {
            "workflow": {"stage": stage},
            "authoring": {"htmlChange": proposal},
        }

    @classmethod
    def payload(cls, action: str = "approve-apply-check") -> dict[str, object]:
        proposal = cls.proposal("proposed")
        return {
            "action": action,
            "confirmed": True,
            "proposalId": proposal["proposalId"],
            "proposalDigest": proposal["proposalDigest"],
            "baseHtmlRevision": proposal["baseHtmlRevision"],
            "baseRegistryRevision": proposal["baseRegistryRevision"],
            "candidateHtmlRevision": proposal["candidateHtmlRevision"],
            "candidateRegistryRevision": proposal["candidateRegistryRevision"],
            "reviewedSlideIds": ["slide-a", "slide-b"],
        }

    def test_combined_action_requires_every_affected_slide(self) -> None:
        state = self.state(self.proposal("proposed"))
        payload = self.payload()
        payload["reviewedSlideIds"] = ["slide-a"]
        with patch("scripts.run_html_preview.load_state", return_value=state):
            with self.assertRaisesRegex(WorkflowError, "Every affected slide"):
                _run_html_preview_action(Path("C:/repository"), payload)

    def test_combined_action_approves_applies_and_checks_exact_proposal(self) -> None:
        proposed = self.state(self.proposal("proposed"))
        approved = self.state(self.proposal("approved"))
        applied = self.state(self.proposal("applied"))
        checked = self.state(self.proposal("applied", checked=True))
        with (
            patch(
                "scripts.run_html_preview.load_state",
                side_effect=[proposed, approved, applied, checked],
            ),
            patch("scripts.run_html_preview.command_approve_html_change") as approve,
            patch("scripts.run_html_preview.command_apply_html_change") as apply,
            patch("scripts.run_html_preview.command_check_html_change") as check,
        ):
            result = _run_html_preview_action(Path("C:/repository"), self.payload())
        self.assertEqual(result["postApplyReviewStatus"], "checked")
        approve.assert_called_once_with(Path("C:/repository"), proposed)
        apply.assert_called_once_with(Path("C:/repository"), approved)
        check.assert_called_once_with(Path("C:/repository"), applied, browser_executable=None)

    def test_whole_deck_action_requires_checked_application_then_approves(self) -> None:
        applied = self.state(self.proposal("applied", checked=True))
        ready = self.state(self.proposal("applied", checked=True), stage="ready_for_conversion")
        with (
            patch("scripts.run_html_preview.load_state", side_effect=[applied, ready]),
            patch("scripts.run_html_preview.command_approve_html_deck") as approve,
        ):
            result = _run_html_preview_action(
                Path("C:/repository"), self.payload("approve-html-deck"),
            )
        self.assertEqual(result, {"status": "approved", "stage": "ready_for_conversion"})
        approve.assert_called_once_with(Path("C:/repository"), applied)


@unittest.skipUnless(
    os.environ.get("BENTO_BROWSER_TEST") == "1",
    "Set BENTO_BROWSER_TEST=1 for HTML preview interaction tests.",
)
class HtmlPreviewBrowserTests(unittest.TestCase):
    def test_version_indicator_toggle_and_review_slide_navigation_stay_in_sync(self) -> None:
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "日本語 preview"
            (root / "workflow").mkdir(parents=True)
            (root / "sources").mkdir()
            (root / "deck/assets").mkdir(parents=True)
            shutil.copy2(ROOT / "tests/fixtures/deck_v2.initialized.yaml", root / "deck.yaml")
            shutil.copy2(ROOT / "workflow/deck.schema.json", root / "workflow/deck.schema.json")
            shutil.copy2(ROOT / "workflow/deck.v1.schema.json", root / "workflow/deck.v1.schema.json")
            (root / "REQUEST.md").write_text("# Request\n", encoding="utf-8")
            (root / "sources/source-manifest.yaml").write_text(
                yaml.safe_dump({"schemaVersion": 1, "authorityMode": "single", "items": []}),
                encoding="utf-8",
            )
            canonical = root / "deck/deck.preview.html"
            candidate = root / "deck/.bento-html-change-a1b2c3d4e5f6.candidate.html"
            canonical.write_text(
                """<style>.slide{width:1280px;height:720px}</style>
<section class="slide" data-slide-id="removed" data-section-id="intro"><h1>現在案だけ</h1></section>
<section class="slide" data-slide-id="shared-a" data-section-id="intro"><h1>共有A・現在</h1></section>
<section class="slide" data-slide-id="shared-b" data-section-id="details"><h1>共有B・現在</h1></section>""",
                encoding="utf-8",
            )
            candidate.write_text(
                """<style>.slide{width:1280px;height:720px}</style>
<section class="slide" data-slide-id="shared-b" data-section-id="new-section"><h1>共有B・変更</h1></section>
<section class="slide" data-slide-id="added" data-section-id="new-section"><h1>変更案だけ</h1></section>
<section class="slide" data-slide-id="shared-a" data-section-id="intro"><h1>共有A・変更</h1></section>""",
                encoding="utf-8",
            )
            (root / "deck/deck.registry.json").write_text("{}", encoding="utf-8")
            (root / "deck/.bento-html-change-a1b2c3d4e5f6.candidate.registry.json").write_text(
                "{}", encoding="utf-8",
            )
            state, _, _ = migrate_v1_state(root, load_state(root), dry_run=True)
            state["sources"].update(manifest="sources/source-manifest.yaml", authorityMode="single")
            state["authoring"].update({
                "mode": "single",
                "entryHtml": "deck/deck.preview.html",
                "registry": "deck/deck.registry.json",
                "currentSection": None,
                "strategy": "whole_deck",
                "htmlReview": {
                    "format": "bento/html-deck-review-baseline/v1",
                    "htmlRevision": "sha256:" + "1" * 64,
                    "registryRevision": "sha256:" + "2" * 64,
                    "evidenceDigest": "sha256:" + "5" * 64,
                    "dependencyRevisions": {},
                    "source": "completed-authoring",
                    "proposalDigest": None,
                    "openedAt": "2026-08-12T00:00:00Z",
                },
                "htmlChange": {
                    "format": "bento/html-change-proposal/v3",
                    "proposalId": "a1b2c3d4e5f6",
                    "status": "proposed",
                    "baseHtmlRevision": "sha256:" + "1" * 64,
                    "baseRegistryRevision": "sha256:" + "2" * 64,
                    "baseReviewDigest": "sha256:" + "5" * 64,
                    "baseDependencyRevisions": {},
                    "candidateHtml": candidate.relative_to(root).as_posix(),
                    "candidateRegistry": "deck/.bento-html-change-a1b2c3d4e5f6.candidate.registry.json",
                    "candidateHtmlRevision": "sha256:" + "3" * 64,
                    "candidateRegistryRevision": "sha256:" + "4" * 64,
                    "candidateReviewDigest": "sha256:" + "6" * 64,
                    "candidateDependencyRevisions": {},
                    "proposalPath": "output/html-change-proposals/a1b2c3d4e5f6.json",
                    "request": "追加・削除・並べ替えを行う",
                    "summary": "資料構造を変更します",
                    "impactSummary": "全スライドを再確認します",
                    "proposalDigest": None,
                    "approvedProposalDigest": None,
                    "postApplyReview": None,
                    "scope": "global",
                    "requestedSlideIds": ["removed", "shared-a", "shared-b", "added"],
                    "relatedSlideIds": [],
                    "changedSlideIds": ["removed", "shared-a", "shared-b", "added"],
                    "affectedSlideIds": ["removed", "shared-a", "shared-b", "added"],
                    "addedSlideIds": ["added"],
                    "removedSlideIds": ["removed"],
                    "changedSectionIds": ["intro", "details", "new-section"],
                    "slideTitles": {
                        "removed": "現在案だけ", "shared-a": "共有A・変更",
                        "shared-b": "共有B・変更", "added": "変更案だけ",
                    },
                    "reordered": True,
                    "sectionMembershipChanged": True,
                    "structuralImpact": True,
                    "globalStyleChanged": False,
                    "registryChanged": False,
                    "proposedAt": "2026-08-12T00:00:00Z",
                    "approvedAt": None,
                    "appliedAt": None,
                    "cancelledAt": None,
                },
            })
            proposal = state["authoring"]["htmlChange"]
            proposal["proposalDigest"] = html_change_proposal_digest(proposal)
            state["chapters"] = {}
            state["sections"] = {
                "intro": {
                    "title": "導入", "status": "html_review", "canonical": "html",
                    "slideIds": ["removed", "shared-a"], "bentoSlideIds": [], "approvalDigest": None,
                    "bentoDocumentRevision": None, "bentoRegistryRevision": None,
                    "bentoSectionDigest": None, "acceptedAt": None,
                },
                "details": {
                    "title": "詳細", "status": "html_review", "canonical": "html",
                    "slideIds": ["shared-b"], "bentoSlideIds": [], "approvalDigest": None,
                    "bentoDocumentRevision": None, "bentoRegistryRevision": None,
                    "bentoSectionDigest": None, "acceptedAt": None,
                },
            }
            state["workflow"].update(
                stage="html_review", status="awaiting_approval", owner="work", sourceOfTruth="html",
                currentChapter=None, currentSection=None, blockingReason=None, blockedFrom=None,
            )
            atomic_write_state(root, state)
            server = create_preview_server(root, port=HtmlPreviewTests.free_port())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1100, "height": 800})
                    page.goto(f"http://127.0.0.1:{server.server_port}/")
                    expect(page.locator("#view-indicator-label")).to_have_text("現在案")
                    expect(page.locator("#show-canonical")).to_have_attribute("aria-pressed", "true")
                    expect(page.locator("#sidebar-resizer")).to_have_attribute("aria-valuenow", "300")
                    expect(page.locator("#preview-scale-label")).to_have_text(re.compile(r"^\d+%（自動）$"))

                    def preview_metrics() -> dict[str, float | str]:
                        return page.locator("#deck").evaluate(
                            """frame => {
                              const doc = frame.contentDocument;
                              const slide = doc.querySelector('[data-slide-id]');
                              const rect = slide.getBoundingClientRect();
                              return {
                                naturalWidth: slide.offsetWidth,
                                naturalHeight: slide.offsetHeight,
                                computedWidth: doc.defaultView.getComputedStyle(slide).width,
                                visualWidth: rect.width,
                                visualHeight: rect.height,
                                frameWidth: frame.clientWidth,
                                frameHeight: frame.clientHeight,
                                scale: Number(doc.documentElement.dataset.bentoPreviewScale),
                              };
                            }"""
                        )

                    initial_metrics = preview_metrics()
                    self.assertEqual(initial_metrics["naturalWidth"], 1280)
                    self.assertEqual(initial_metrics["naturalHeight"], 720)
                    self.assertAlmostEqual(
                        float(str(initial_metrics["computedWidth"]).removesuffix("px")),
                        1280,
                        delta=0.1,
                    )
                    self.assertLess(initial_metrics["visualWidth"], initial_metrics["naturalWidth"])
                    self.assertLessEqual(initial_metrics["visualWidth"], initial_metrics["frameWidth"])
                    expect(page.locator("#view-slide-label")).to_contain_text("01 / 03 現在案だけ")

                    sidebar_before = page.locator("aside").bounding_box()["width"]
                    resizer_box = page.locator("#sidebar-resizer").bounding_box()
                    page.mouse.move(
                        resizer_box["x"] + resizer_box["width"] / 2,
                        resizer_box["y"] + resizer_box["height"] / 2,
                    )
                    page.mouse.down()
                    page.mouse.move(
                        resizer_box["x"] + resizer_box["width"] / 2 + 120,
                        resizer_box["y"] + resizer_box["height"] / 2,
                        steps=5,
                    )
                    page.mouse.up()
                    sidebar_after = page.locator("aside").bounding_box()["width"]
                    self.assertGreater(sidebar_after, sidebar_before + 100)
                    dragged_metrics = preview_metrics()
                    self.assertLess(dragged_metrics["scale"], initial_metrics["scale"])
                    self.assertEqual(dragged_metrics["naturalWidth"], 1280)
                    expect(page.locator("#view-slide-label")).to_contain_text("01 / 03 現在案だけ")

                    page.set_viewport_size({"width": 1800, "height": 1100})
                    page.wait_for_function(
                        "previous => Number(document.querySelector('#deck').contentDocument.documentElement.dataset.bentoPreviewScale) > previous",
                        arg=dragged_metrics["scale"],
                    )
                    expanded_metrics = preview_metrics()
                    self.assertGreater(expanded_metrics["scale"], dragged_metrics["scale"])
                    self.assertEqual(expanded_metrics["naturalWidth"], 1280)
                    expect(page.locator("#view-slide-label")).to_contain_text("01 / 03 現在案だけ")
                    page.locator("#sidebar-resizer").dblclick()
                    expect(page.locator("#sidebar-resizer")).to_have_attribute("aria-valuenow", "300")

                    self.assertEqual(
                        page.locator('[data-nav-kind="slides"][data-nav-view="canonical"] .slide-nav').all_text_contents(),
                        ["01. 現在案だけ", "02. 共有A・現在", "03. 共有B・現在"],
                    )
                    expect(page.frame_locator("#deck").locator("h1").first).to_have_text("現在案だけ")

                    page.locator("#show-candidate").click()
                    expect(page.locator("#view-indicator-label")).to_have_text("変更案")
                    expect(page.locator("#show-candidate")).to_have_attribute("aria-pressed", "true")
                    expect(page.locator("#preview-scale-label")).to_have_text(re.compile(r"^\d+%（自動）$"))
                    self.assertEqual(preview_metrics()["naturalWidth"], 1280)
                    self.assertEqual(
                        page.locator('[data-nav-kind="slides"][data-nav-view="candidate"] .slide-nav').all_text_contents(),
                        ["01. 共有B・変更", "02. 変更案だけ", "03. 共有A・変更"],
                    )
                    expect(page.frame_locator("#deck").locator("h1").first).to_have_text("共有B・変更")
                    expect(page.locator("#preview-pane")).to_have_attribute("data-view", "candidate")

                    candidate_review = page.locator('[data-review-view="candidate"]')
                    self.assertEqual(
                        candidate_review.locator(".review-slide-title").all_text_contents(),
                        ["共有B・変更", "変更案だけ", "共有A・変更", "現在案だけ"],
                    )
                    expect(
                        candidate_review.locator('[data-review-slide="removed"] .review-slide-number')
                    ).to_have_text("現在案のみ")
                    candidate_review.locator('[data-review-slide="added"]').click()
                    expect(page).to_have_url(re.compile(r"#slide=added$"))
                    expect(candidate_review.locator('[data-review-slide="added"]')).to_have_class(
                        re.compile(r"\bis-active\b")
                    )
                    expect(page.locator("#view-slide-label")).to_contain_text("02 / 03 変更案だけ")

                    page.locator("#show-canonical").click()
                    expect(page.locator("#view-indicator-label")).to_have_text("現在案")
                    expect(page).to_have_url(re.compile(r"#slide=shared-b$"))
                    expect(page.locator("#view-slide-label")).to_contain_text("03 / 03 共有B・現在")
                    self.assertEqual(
                        page.locator('[data-review-view="canonical"] .review-slide-title').all_text_contents(),
                        ["現在案だけ", "共有A・現在", "共有B・現在", "変更案だけ"],
                    )

                    page.locator("#show-candidate").click()
                    expect(page.locator("#view-indicator-label")).to_have_text("変更案")
                    candidate_review.locator('[data-review-slide="removed"]').click()
                    expect(page.locator("#view-indicator-label")).to_have_text("現在案")
                    expect(page).to_have_url(re.compile(r"#slide=removed$"))
                    expect(page.locator("#view-slide-label")).to_contain_text("01 / 03 現在案だけ")

                    apply_button = page.locator("#apply-proposal")
                    expect(apply_button).to_be_disabled()
                    affected = ["removed", "shared-a", "shared-b", "added"]
                    for slide_id in affected:
                        preferred = (
                            page.locator(f'[data-review-view="canonical"] [data-review-slide="{slide_id}"]')
                            .get_attribute("data-preferred-view")
                        )
                        if preferred == "candidate":
                            page.locator("#show-candidate").click()
                        elif preferred == "canonical":
                            page.locator("#show-canonical").click()
                        visible_review = page.locator(
                            f'[data-review-view="{preferred or page.locator("#deck").get_attribute("data-view")}"] '
                            f'[data-review-slide="{slide_id}"]'
                        )
                        visible_review.click()
                        page.locator("#mark-reviewed").click()
                    expect(page.locator("#review-progress-count")).to_have_text("4 / 4")
                    expect(apply_button).to_be_enabled()
                    page.reload()
                    expect(page.locator("#review-progress-count")).to_have_text("4 / 4")
                    expect(page.locator("#apply-proposal")).to_be_enabled()
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
