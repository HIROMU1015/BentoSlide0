"""Crash-recoverable multi-artifact transactions and OS-backed writer leases."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from .errors import BentoConverterError


JOURNAL_FORMAT = "bento/artifact-transaction/v1"
JOURNAL_STATES = {"preparing", "prepared", "replacing", "committed", "report_failed", "rolled_back"}


class ArtifactLeaseConflict(BentoConverterError):
    """Another process owns the same writer lease or transaction lock."""


class ArtifactRecoveryError(BentoConverterError):
    """An incomplete transaction cannot be recovered without risking data."""


class ArtifactReportError(BentoConverterError):
    """Artifacts committed successfully, but the auxiliary report did not."""

    def __init__(self, message: str, *, transaction_id: str):
        super().__init__(message)
        self.transaction_id = transaction_id
        self.artifacts_committed = True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def bytes_revision(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def file_revision(path: str | Path) -> str | None:
    candidate = Path(path)
    return bytes_revision(candidate.read_bytes()) if candidate.is_file() else None


def _canonical_identity(repository: Path, artifacts: Iterable[Path]) -> str:
    values = [os.path.normcase(str(repository.resolve()))]
    values.extend(sorted(os.path.normcase(str(path.resolve())) for path in artifacts))
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes_fsync(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_json_fsync(destination: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    _write_bytes_fsync(destination, payload)


class OsFileLock:
    """One-byte cross-process lock held by an open OS file handle."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self._handle: Any | None = None
        self._gate = threading.RLock()
        self._depth = 0

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self, *, blocking: bool = False, timeout: float = 0.0) -> bool:
        gate_acquired = (
            self._gate.acquire(blocking=False)
            if not blocking else self._gate.acquire(timeout=max(0.0, timeout))
        )
        if not gate_acquired:
            return False
        if self.acquired:
            self._depth += 1
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("a+b")
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            self._gate.release()
            raise
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                self._depth = 1
                return True
            except (OSError, BlockingIOError):
                if not blocking or time.monotonic() >= deadline:
                    handle.close()
                    self._gate.release()
                    return False
                time.sleep(0.05)

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        if self._depth > 1:
            self._depth -= 1
            self._gate.release()
            return
        self._handle = None
        self._depth = 0
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._gate.release()

    def __enter__(self) -> "OsFileLock":
        if not self.acquire(blocking=True, timeout=10.0):
            raise ArtifactLeaseConflict(f"Timed out acquiring artifact lock: {self.path}")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class WriterLease:
    """Long-lived exclusive writer identity for every artifact in a set.

    A lease is acquired in canonical path order so two partially overlapping
    artifact sets cannot write the shared file concurrently or deadlock while
    acquiring their locks.
    """

    def __init__(self, repository: str | Path, artifacts: Iterable[str | Path], *, state_root: str | Path | None = None):
        self.repository = Path(repository).resolve()
        self.artifacts = tuple(sorted(
            {Path(path).resolve() for path in artifacts}, key=lambda path: str(path).casefold(),
        ))
        if not self.artifacts:
            raise BentoConverterError("Writer lease requires at least one artifact")
        self.identity = _canonical_identity(self.repository, self.artifacts)
        root = Path(state_root).resolve() if state_root else self.repository / "output/.bento-transactions"
        self.locks = tuple(
            OsFileLock(root / "leases" / f"artifact-{_canonical_identity(self.repository, (path,))}.lock")
            for path in self.artifacts
        )
        self.metadata_path = root / "leases" / f"writer-{self.identity}.json"

    @property
    def acquired(self) -> bool:
        return all(lock.acquired for lock in self.locks)

    def acquire(self) -> None:
        if self.acquired:
            return
        acquired: list[OsFileLock] = []
        try:
            for lock in self.locks:
                if not lock.acquire(blocking=False):
                    raise ArtifactLeaseConflict(
                        "Another process already owns a writer lease for an overlapping artifact set"
                    )
                acquired.append(lock)
            _write_json_fsync(self.metadata_path, {
                "format": "bento/writer-lease/v1", "identity": self.identity,
                "repository": str(self.repository), "artifacts": [str(path) for path in self.artifacts],
                "pid": os.getpid(), "acquiredAt": utc_now(),
            })
        except BaseException:
            for lock in reversed(acquired):
                lock.release()
            raise

    def release(self) -> None:
        if not self.acquired:
            return
        self.metadata_path.unlink(missing_ok=True)
        for lock in reversed(self.locks):
            lock.release()

    def __enter__(self) -> "WriterLease":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class ArtifactTransactionStore:
    """Prepare, replace, validate, report, and recover an artifact set."""

    def __init__(
        self,
        repository: str | Path,
        artifacts: Iterable[str | Path],
        *,
        state_root: str | Path | None = None,
        fault_injector: Callable[[str, dict[str, Any]], None] | None = None,
        inherited_writer_lease: WriterLease | None = None,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.artifacts = tuple(Path(path).resolve() for path in artifacts)
        if not self.artifacts:
            raise BentoConverterError("Artifact transaction requires at least one target")
        for path in self.artifacts:
            try:
                path.relative_to(self.repository)
            except ValueError as exc:
                raise BentoConverterError(f"Artifact target escapes the repository: {path}") from exc
        self.identity = _canonical_identity(self.repository, self.artifacts)
        self.state_root = Path(state_root).resolve() if state_root else self.repository / "output/.bento-transactions"
        self.active_dir = self.state_root / "active" / self.identity
        self.archive_dir = self.state_root / "archive" / self.identity
        self.transaction_lock = OsFileLock(self.state_root / "locks" / f"transaction-{self.identity}.lock")
        self.writer_lease = WriterLease(self.repository, self.artifacts, state_root=self.state_root)
        self.inherited_writer_lease = inherited_writer_lease
        if inherited_writer_lease is not None:
            if inherited_writer_lease.repository != self.repository:
                raise BentoConverterError("Inherited writer lease belongs to another repository")
            if not set(inherited_writer_lease.artifacts).intersection(self.artifacts):
                raise BentoConverterError("Inherited writer lease does not cover any transaction artifact")
        self.fault_injector = fault_injector

    @contextmanager
    def _transaction_guard(self) -> Iterator[None]:
        if not self.transaction_lock.acquire(blocking=True, timeout=10.0):
            raise ArtifactLeaseConflict("Timed out waiting for the artifact transaction lock")
        try:
            yield
        finally:
            self.transaction_lock.release()

    @contextmanager
    def _writer_guard(self) -> Iterator[None]:
        if self.inherited_writer_lease is not None:
            if not self.inherited_writer_lease.acquired:
                raise ArtifactLeaseConflict("Inherited writer lease is no longer held")
            inherited = set(self.inherited_writer_lease.artifacts)
            remaining = tuple(path for path in self.artifacts if path not in inherited)
            supplemental = (
                WriterLease(self.repository, remaining, state_root=self.state_root) if remaining else None
            )
            if supplemental is not None:
                supplemental.acquire()
            try:
                yield
            finally:
                if supplemental is not None:
                    supplemental.release()
            return
        acquired_here = not self.writer_lease.acquired
        if acquired_here:
            self.writer_lease.acquire()
        try:
            yield
        finally:
            if acquired_here:
                self.writer_lease.release()

    def acquire_writer_lease(self) -> None:
        self.writer_lease.acquire()

    def release_writer_lease(self) -> None:
        self.writer_lease.release()

    def read_snapshot(self, paths: Iterable[str | Path]) -> dict[Path, bytes | None]:
        requested = [Path(path).resolve() for path in paths]
        with self._transaction_guard():
            return {path: path.read_bytes() if path.is_file() else None for path in requested}

    def _fault(self, event: str, journal: dict[str, Any]) -> None:
        if self.fault_injector:
            self.fault_injector(event, journal)

    def _journal_path(self, transaction_id: str) -> Path:
        return self.active_dir / f"{transaction_id}.json"

    def _persist(self, journal: dict[str, Any]) -> None:
        journal["updatedAt"] = utc_now()
        _write_json_fsync(self._journal_path(journal["transactionId"]), journal)

    def _set_state(self, journal: dict[str, Any], state: str) -> None:
        if state not in JOURNAL_STATES:
            raise BentoConverterError(f"Unknown transaction journal state: {state}")
        journal["state"] = state
        self._persist(journal)

    def _archive(self, journal: dict[str, Any]) -> None:
        source = self._journal_path(journal["transactionId"])
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        destination = self.archive_dir / source.name
        os.replace(source, destination)
        _fsync_directory(self.archive_dir)
        for artifact in journal["artifacts"]:
            Path(artifact["temporary"]).unlink(missing_ok=True)
            if artifact.get("backup"):
                Path(artifact["backup"]).unlink(missing_ok=True)

    @staticmethod
    def _artifact_state(artifact: dict[str, Any]) -> str:
        actual = file_revision(artifact["target"])
        if actual == artifact["newRevision"]:
            return "new"
        if actual == artifact["oldRevision"]:
            return "old"
        return "unknown"

    def _rollback(self, journal: dict[str, Any]) -> None:
        # Validate that every required rollback source exists before touching any target.
        for artifact in journal["artifacts"]:
            if self._artifact_state(artifact) == "new" and artifact["oldRevision"] is not None:
                backup = Path(artifact["backup"])
                if not backup.is_file() or file_revision(backup) != artifact["oldRevision"]:
                    raise ArtifactRecoveryError(
                        f"Cannot safely roll back {artifact['target']}: backup is missing or invalid"
                    )
        for artifact in journal["artifacts"]:
            target = Path(artifact["target"])
            old_revision = artifact["oldRevision"]
            if old_revision is None:
                target.unlink(missing_ok=True)
            else:
                backup = Path(artifact["backup"])
                rollback_temp = _write_staged_file(target, backup.read_bytes(), journal["transactionId"], "rollback")
                os.replace(rollback_temp, target)
                _fsync_directory(target.parent)
            if file_revision(target) != old_revision:
                raise ArtifactRecoveryError(f"Rollback revision mismatch for {target}")
        self._set_state(journal, "rolled_back")
        self._archive(journal)

    def _write_report(self, path: Path, payload: dict[str, Any]) -> None:
        _write_json_fsync(path, payload)

    def _finish_report(self, journal: dict[str, Any]) -> None:
        report_path = journal.get("reportPath")
        report_payload = journal.get("reportPayload")
        if report_path is not None:
            if not isinstance(report_payload, dict):
                raise ArtifactRecoveryError("Transaction report payload is unavailable")
            self._write_report(Path(report_path), report_payload)
        if journal["state"] != "committed":
            self._set_state(journal, "committed")
        self._archive(journal)

    def recover(self) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        with self._writer_guard(), self._transaction_guard():
            if not self.active_dir.is_dir():
                return recovered
            for path in sorted(self.active_dir.glob("*.json")):
                try:
                    journal = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ArtifactRecoveryError(f"Cannot read transaction journal {path}: {exc}") from exc
                self._validate_journal(journal, path)
                if not journal["artifacts"]:
                    self._set_state(journal, "rolled_back")
                    self._archive(journal)
                    recovered.append({"transactionId": journal["transactionId"], "action": "rolled-back-empty"})
                    continue
                states = [self._artifact_state(artifact) for artifact in journal["artifacts"]]
                if journal["state"] in {"committed", "report_failed"}:
                    if not all(state == "new" for state in states):
                        raise ArtifactRecoveryError(f"Committed transaction artifacts do not match journal: {path}")
                    try:
                        self._finish_report(journal)
                    except OSError as exc:
                        self._set_state(journal, "report_failed")
                        raise ArtifactRecoveryError(f"Cannot regenerate transaction report: {exc}") from exc
                    recovered.append({"transactionId": journal["transactionId"], "action": "commit-completed"})
                elif all(state == "new" for state in states):
                    self._set_state(journal, "committed")
                    self._finish_report(journal)
                    recovered.append({"transactionId": journal["transactionId"], "action": "commit-completed"})
                elif all(state == "old" for state in states):
                    self._rollback(journal)
                    recovered.append({"transactionId": journal["transactionId"], "action": "rolled-back-unapplied"})
                elif set(states).issubset({"old", "new"}):
                    self._rollback(journal)
                    recovered.append({"transactionId": journal["transactionId"], "action": "rolled-back-partial"})
                else:
                    raise ArtifactRecoveryError(
                        f"Transaction targets match neither old nor new revisions; artifacts were left unchanged: {path}"
                    )
        return recovered

    def _validate_journal(self, journal: Any, path: Path) -> None:
        if not isinstance(journal, dict) or journal.get("format") != JOURNAL_FORMAT:
            raise ArtifactRecoveryError(f"Unknown transaction journal format: {path}")
        if journal.get("repository") != str(self.repository):
            raise ArtifactRecoveryError(f"Transaction journal belongs to another repository: {path}")
        if (
            journal.get("state") not in JOURNAL_STATES
            or not isinstance(journal.get("artifacts"), list)
            or not isinstance(journal.get("artifactSet"), list)
        ):
            raise ArtifactRecoveryError(f"Malformed transaction journal: {path}")
        expected_identity = _canonical_identity(
            self.repository, (Path(value) for value in journal["artifactSet"]),
        )
        if expected_identity != self.identity:
            raise ArtifactRecoveryError(f"Transaction journal artifact identity does not match its lock: {path}")
        for value in journal["artifactSet"]:
            try:
                Path(value).resolve().relative_to(self.repository)
            except (ValueError, TypeError) as exc:
                raise ArtifactRecoveryError(f"Journal artifact set escapes repository: {value}") from exc
        for artifact in journal["artifacts"]:
            if not isinstance(artifact, dict) or "target" not in artifact:
                raise ArtifactRecoveryError(f"Malformed artifact record in journal: {path}")
            target = Path(artifact["target"]).resolve()
            try:
                target.relative_to(self.repository)
            except ValueError as exc:
                raise ArtifactRecoveryError(f"Journal artifact escapes repository: {target}") from exc

    def commit(
        self,
        payloads: Mapping[str | Path, bytes],
        *,
        operation: str,
        base_document_revision: str | None = None,
        base_registry_revision: str | None = None,
        target_document_revision: str | None = None,
        target_registry_revision: str | None = None,
        validate_base: Callable[[], None] | None = None,
        validate_prepared: Callable[[], None] | None = None,
        validate_committed: Callable[[], None] | None = None,
        report_path: str | Path | None = None,
        report_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = {Path(path).resolve(): bytes(value) for path, value in payloads.items()}
        if not normalized or any(path not in self.artifacts for path in normalized):
            raise BentoConverterError("Transaction payload targets must be a non-empty subset of the artifact set")
        resolved_report = Path(report_path).resolve() if report_path else None
        if resolved_report is not None:
            try:
                resolved_report.relative_to(self.repository)
            except ValueError as exc:
                raise BentoConverterError(f"Transaction report escapes the repository: {resolved_report}") from exc
        transaction_id = uuid.uuid4().hex
        journal: dict[str, Any] = {
            "format": JOURNAL_FORMAT, "transactionId": transaction_id, "operation": operation,
            "state": "preparing", "repository": str(self.repository), "startedAt": utc_now(),
            "baseDocumentRevision": base_document_revision,
            "baseRegistryRevision": base_registry_revision,
            "targetDocumentRevision": target_document_revision,
            "targetRegistryRevision": target_registry_revision,
            "artifactSet": [str(path) for path in self.artifacts],
            "reportPath": str(resolved_report) if resolved_report else None,
            "reportPayload": ({**report_payload, "transactionId": transaction_id} if report_payload is not None else None),
            "artifacts": [],
        }
        with self._writer_guard(), self._transaction_guard():
            if any(self.active_dir.glob("*.json")):
                raise ArtifactRecoveryError("Recover incomplete artifact transactions before starting a new write")
            self._persist(journal)
            try:
                if validate_base:
                    validate_base()
                for index, (target, payload) in enumerate(normalized.items()):
                    old_revision = file_revision(target)
                    backup = None
                    if old_revision is not None:
                        backup_path = target.with_name(f".{target.name}.{transaction_id}.backup")
                        _write_bytes_fsync(backup_path, target.read_bytes())
                        backup = str(backup_path)
                    temporary = _write_staged_file(target, payload, transaction_id, f"new-{index}")
                    journal["artifacts"].append({
                        "target": str(target), "temporary": str(temporary), "backup": backup,
                        "oldRevision": old_revision, "newRevision": bytes_revision(payload), "replaced": False,
                    })
                    self._persist(journal)
                if validate_prepared:
                    validate_prepared()
                self._set_state(journal, "prepared")
                self._fault("prepared", journal)
                self._set_state(journal, "replacing")
                for index, artifact in enumerate(journal["artifacts"]):
                    os.replace(artifact["temporary"], artifact["target"])
                    _fsync_directory(Path(artifact["target"]).parent)
                    artifact["replaced"] = True
                    self._persist(journal)
                    self._fault(f"replaced:{index}", journal)
                for artifact in journal["artifacts"]:
                    if self._artifact_state(artifact) != "new":
                        raise BentoConverterError(f"Artifact revision mismatch after replacement: {artifact['target']}")
                if validate_committed:
                    validate_committed()
                self._set_state(journal, "committed")
                self._fault("committed", journal)
            except Exception:
                self._rollback(journal)
                raise
            try:
                self._finish_report(journal)
            except Exception as exc:
                self._set_state(journal, "report_failed")
                raise ArtifactReportError(
                    f"Artifacts committed, but operation report failed: {exc}", transaction_id=transaction_id,
                ) from exc
        return {
            "transactionId": transaction_id,
            "documentRevision": target_document_revision,
            "registryRevision": target_registry_revision,
        }


def _write_staged_file(target: Path, payload: bytes, transaction_id: str, label: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    path = target.with_name(f".{target.name}.{transaction_id}.{label}.tmp")
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def recover_repository_transactions(
    repository: str | Path, *, state_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Recover every active artifact-set journal before repository APIs start."""

    root = Path(repository).resolve()
    transaction_root = Path(state_root).resolve() if state_root else root / "output/.bento-transactions"
    active_root = transaction_root / "active"
    if not active_root.is_dir():
        return []
    recovered: list[dict[str, Any]] = []
    for identity_dir in sorted(path for path in active_root.iterdir() if path.is_dir()):
        journals = sorted(identity_dir.glob("*.json"))
        if not journals:
            continue
        try:
            sample = json.loads(journals[0].read_text(encoding="utf-8-sig"))
            artifact_paths = [Path(value).resolve() for value in sample["artifactSet"]]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ArtifactRecoveryError(f"Cannot discover active transaction {identity_dir}: {exc}") from exc
        if not artifact_paths:
            raise ArtifactRecoveryError(f"Cannot infer artifact identity from journal: {journals[0]}")
        store = ArtifactTransactionStore(root, artifact_paths, state_root=transaction_root)
        if store.identity != identity_dir.name:
            raise ArtifactRecoveryError(f"Transaction identity directory does not match journal artifacts: {identity_dir}")
        recovered.extend(store.recover())
    return recovered
