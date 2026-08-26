from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bento_converter.artifact_transaction import (
    ArtifactLeaseConflict,
    ArtifactRecoveryError,
    ArtifactReportError,
    ArtifactTransactionStore,
    WriterLease,
    _replace_with_retry,
    recover_repository_transactions,
)
from bento_converter.errors import BentoConverterError


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

    @staticmethod
    def access_denied() -> PermissionError:
        error = PermissionError(13, "Access is denied")
        error.winerror = 5
        return error

    def test_atomic_replace_retries_transient_windows_access_denied(self) -> None:
        source = self.root / "source.tmp"
        destination = self.root / "destination.json"
        source.write_bytes(b"new")
        destination.write_bytes(b"old")
        original_replace = os.replace
        calls = 0

        def flaky_replace(current_source, current_destination):
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise self.access_denied()
            return original_replace(current_source, current_destination)

        with patch("bento_converter.artifact_transaction.os.replace", side_effect=flaky_replace), patch(
            "bento_converter.artifact_transaction.time.sleep"
        ) as sleep:
            _replace_with_retry(source, destination)

        self.assertEqual(calls, 3)
        self.assertEqual(destination.read_bytes(), b"new")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.05, 0.1])

    def test_atomic_replace_stops_after_five_transient_retries(self) -> None:
        source = self.root / "source.tmp"
        destination = self.root / "destination.json"
        source.write_bytes(b"new")
        destination.write_bytes(b"old")

        with patch(
            "bento_converter.artifact_transaction.os.replace", side_effect=self.access_denied()
        ) as replace, patch("bento_converter.artifact_transaction.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                _replace_with_retry(source, destination)

        self.assertEqual(replace.call_count, 6)
        self.assertEqual(sleep.call_count, 5)
        self.assertEqual(destination.read_bytes(), b"old")
        self.assertEqual(source.read_bytes(), b"new")

    def test_atomic_replace_does_not_retry_unrelated_errors(self) -> None:
        with patch(
            "bento_converter.artifact_transaction.os.replace", side_effect=FileNotFoundError("missing")
        ) as replace, patch("bento_converter.artifact_transaction.time.sleep") as sleep:
            with self.assertRaises(FileNotFoundError):
                _replace_with_retry(self.root / "missing.tmp", self.root / "destination.json")

        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()

    def test_commit_retries_a_transient_journal_replace_without_user_retry(self) -> None:
        store = self.store()
        original_replace = os.replace
        rejected_once = False

        def reject_first_journal_replace(source, destination):
            nonlocal rejected_once
            destination_path = Path(destination)
            if (
                not rejected_once
                and destination_path.suffix == ".json"
                and ".bento-transactions" in destination_path.parts
                and "active" in destination_path.parts
            ):
                rejected_once = True
                raise self.access_denied()
            return original_replace(source, destination)

        with patch(
            "bento_converter.artifact_transaction.os.replace",
            side_effect=reject_first_journal_replace,
        ), patch("bento_converter.artifact_transaction.time.sleep") as sleep:
            result = store.commit(
                {self.first: b"new-html", self.second: b"new-json", self.third: b"new-registry"},
                operation="authoring-save",
            )

        self.assertTrue(rejected_once)
        self.assertEqual(sleep.call_args_list[0].args, (0.05,))
        self.assertEqual(self.first.read_bytes(), b"new-html")
        self.assertEqual(self.second.read_bytes(), b"new-json")
        self.assertEqual(self.third.read_bytes(), b"new-registry")
        archived = store.archive_dir / f"{result['transactionId']}.json"
        self.assertEqual(json.loads(archived.read_text(encoding="utf-8"))["state"], "committed")

    def test_persistent_target_access_denied_rolls_back_every_artifact(self) -> None:
        store = self.store()
        original_replace = os.replace
        denied_calls = 0

        def deny_first_target(source, destination):
            nonlocal denied_calls
            if Path(destination).resolve() == self.first:
                denied_calls += 1
                raise self.access_denied()
            return original_replace(source, destination)

        with patch(
            "bento_converter.artifact_transaction.os.replace", side_effect=deny_first_target
        ), patch("bento_converter.artifact_transaction.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                store.commit(
                    {self.first: b"new-html", self.second: b"new-json", self.third: b"new-registry"},
                    operation="authoring-save",
                )

        self.assertEqual(denied_calls, 6)
        self.assertEqual(sleep.call_count, 5)
        self.assertEqual(self.first.read_bytes(), b"old-html")
        self.assertEqual(self.second.read_bytes(), b"old-json")
        self.assertEqual(self.third.read_bytes(), b"old-registry")
        archived = list(store.archive_dir.glob("*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text(encoding="utf-8"))["state"], "rolled_back")

    def test_writer_lease_conflicts_for_overlapping_artifact_sets(self) -> None:
        first = WriterLease(self.root, self.artifacts)
        duplicate = WriterLease(self.root, reversed(self.artifacts))
        overlapping = WriterLease(self.root, (self.second, self.root / "output/other.json"))
        independent = WriterLease(self.root, (self.root / "output/other.html",))
        first.acquire()
        try:
            with self.assertRaises(ArtifactLeaseConflict):
                duplicate.acquire()
            with self.assertRaises(ArtifactLeaseConflict):
                overlapping.acquire()
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

    def test_report_cannot_overwrite_a_transaction_artifact(self) -> None:
        store = self.store()
        before = [path.read_bytes() for path in self.artifacts]

        with self.assertRaisesRegex(BentoConverterError, "must not overwrite"):
            store.commit(
                {self.first: b"new-html", self.second: b"new-json"},
                operation="authoring-save",
                report_path=self.first,
                report_payload={"format": "test-report/v1"},
            )

        self.assertEqual([path.read_bytes() for path in self.artifacts], before)
        self.assertFalse(any(store.active_dir.glob("*.json")))
        self.assertFalse(any(store.archive_dir.glob("*.json")))

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
