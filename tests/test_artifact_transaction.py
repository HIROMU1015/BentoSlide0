from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from bento_converter.artifact_transaction import (
    ArtifactLeaseConflict,
    ArtifactRecoveryError,
    ArtifactReportError,
    ArtifactTransactionStore,
    WriterLease,
    recover_repository_transactions,
)


class SimulatedCrash(BaseException):
    pass


class ArtifactTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.first = self.root / "output/authoring.bento.html"
        self.second = self.root / "output/authoring.bento.json"
        self.third = self.root / "output/authoring.registry.json"
        self.first.parent.mkdir(parents=True)
        self.first.write_bytes(b"old-html")
        self.second.write_bytes(b"old-json")
        self.third.write_bytes(b"old-registry")
        self.artifacts = (self.first, self.second, self.third)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def store(self, fault=None) -> ArtifactTransactionStore:
        return ArtifactTransactionStore(self.root, self.artifacts, fault_injector=fault)

    def test_writer_lease_conflicts_only_for_the_same_artifact_set(self) -> None:
        first = WriterLease(self.root, self.artifacts)
        duplicate = WriterLease(self.root, reversed(self.artifacts))
        independent = WriterLease(self.root, (self.root / "output/other.html",))
        first.acquire()
        try:
            with self.assertRaises(ArtifactLeaseConflict):
                duplicate.acquire()
            independent.acquire()
            independent.release()
            self.assertTrue(first.acquired)
        finally:
            first.release()
        duplicate.acquire()
        duplicate.release()

    def test_commit_writes_all_artifacts_report_and_archived_journal(self) -> None:
        store = self.store()
        report_path = self.root / "output/operation-report.json"
        result = store.commit(
            {self.first: b"new-html", self.second: b"new-json", self.third: b"new-registry"},
            operation="authoring-save",
            base_document_revision="sha256:" + "1" * 64,
            base_registry_revision="sha256:" + "2" * 64,
            target_document_revision="sha256:" + "3" * 64,
            target_registry_revision="sha256:" + "4" * 64,
            report_path=report_path,
            report_payload={"format": "test-report/v1", "passed": True},
        )
        self.assertEqual([path.read_bytes() for path in self.artifacts], [b"new-html", b"new-json", b"new-registry"])
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["passed"], True)
        self.assertFalse(any(store.active_dir.glob("*.json")))
        archived = list(store.archive_dir.glob("*.json"))
        self.assertEqual(len(archived), 1)
        journal = json.loads(archived[0].read_text(encoding="utf-8"))
        self.assertEqual(journal["state"], "committed")
        self.assertEqual(journal["transactionId"], result["transactionId"])
        self.assertTrue(all(item["replaced"] for item in journal["artifacts"]))

    def test_partial_replace_is_rolled_back_on_next_start(self) -> None:
        def fault(event: str, _journal: dict) -> None:
            if event == "replaced:0":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            self.store(fault).commit(
                {self.first: b"new-html", self.second: b"new-json", self.third: b"new-registry"},
                operation="authoring-save",
            )
        self.assertEqual(self.first.read_bytes(), b"new-html")
        self.assertEqual(self.second.read_bytes(), b"old-json")
        recovered = self.store().recover()
        self.assertEqual(recovered[0]["action"], "rolled-back-partial")
        self.assertEqual([path.read_bytes() for path in self.artifacts], [b"old-html", b"old-json", b"old-registry"])

    def test_all_new_revisions_complete_commit_after_crash(self) -> None:
        def fault(event: str, _journal: dict) -> None:
            if event == "replaced:2":
                raise SimulatedCrash()

        report = self.root / "output/recovered-report.json"
        with self.assertRaises(SimulatedCrash):
            self.store(fault).commit(
                {self.first: b"new-html", self.second: b"new-json", self.third: b"new-registry"},
                operation="authoring-save", report_path=report, report_payload={"recovered": True},
            )
        recovered = self.store().recover()
        self.assertEqual(recovered[0]["action"], "commit-completed")
        self.assertEqual([path.read_bytes() for path in self.artifacts], [b"new-html", b"new-json", b"new-registry"])
        self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["recovered"])

    def test_missing_backup_refuses_unsafe_recovery_without_more_changes(self) -> None:
        def fault(event: str, _journal: dict) -> None:
            if event == "replaced:0":
                raise SimulatedCrash()

        store = self.store(fault)
        with self.assertRaises(SimulatedCrash):
            store.commit({self.first: b"new-html", self.second: b"new-json"}, operation="authoring-save")
        journal_path = next(store.active_dir.glob("*.json"))
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        Path(journal["artifacts"][0]["backup"]).unlink()
        before = (self.first.read_bytes(), self.second.read_bytes(), self.third.read_bytes())
        with self.assertRaises(ArtifactRecoveryError):
            self.store().recover()
        self.assertEqual((self.first.read_bytes(), self.second.read_bytes(), self.third.read_bytes()), before)

    def test_report_failure_preserves_commit_and_recovery_retries_report(self) -> None:
        store = self.store()
        original_writer = store._write_report

        def fail_report(_path: Path, _payload: dict) -> None:
            raise OSError("disk unavailable")

        store._write_report = fail_report  # type: ignore[method-assign]
        report = self.root / "output/report.json"
        with self.assertRaises(ArtifactReportError) as captured:
            store.commit(
                {self.first: b"new-html", self.second: b"new-json"}, operation="authoring-save",
                report_path=report, report_payload={"retry": True},
            )
        self.assertTrue(captured.exception.artifacts_committed)
        self.assertEqual((self.first.read_bytes(), self.second.read_bytes()), (b"new-html", b"new-json"))
        active = json.loads(next(store.active_dir.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(active["state"], "report_failed")
        store._write_report = original_writer  # type: ignore[method-assign]
        recovered = store.recover()
        self.assertEqual(recovered[0]["action"], "commit-completed")
        self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["retry"])

    def test_consistent_snapshot_waits_until_all_replacements_finish(self) -> None:
        first_replaced = threading.Event()
        allow_finish = threading.Event()

        def fault(event: str, _journal: dict) -> None:
            if event == "replaced:0":
                first_replaced.set()
                self.assertTrue(allow_finish.wait(timeout=10))

        writer = self.store(fault)
        reader = self.store()
        transaction = threading.Thread(target=lambda: writer.commit(
            {self.first: b"new-html", self.second: b"new-json"}, operation="authoring-save",
        ))
        transaction.start()
        self.assertTrue(first_replaced.wait(timeout=10))
        result: dict = {}
        reading = threading.Thread(target=lambda: result.update(reader.read_snapshot((self.first, self.second))))
        reading.start()
        time.sleep(0.1)
        self.assertTrue(reading.is_alive(), "snapshot read must wait for the transaction lock")
        allow_finish.set()
        transaction.join(timeout=10)
        reading.join(timeout=10)
        self.assertEqual(result[self.first], b"new-html")
        self.assertEqual(result[self.second], b"new-json")

    def test_repository_recovery_discovers_artifact_identity_from_journal(self) -> None:
        def fault(event: str, _journal: dict) -> None:
            if event == "replaced:0":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            self.store(fault).commit(
                {self.first: b"new-html", self.second: b"new-json"}, operation="authoring-save",
            )
        recovered = recover_repository_transactions(self.root)
        self.assertEqual(recovered[0]["action"], "rolled-back-partial")
        self.assertEqual((self.first.read_bytes(), self.second.read_bytes()), (b"old-html", b"old-json"))


if __name__ == "__main__":
    unittest.main()
