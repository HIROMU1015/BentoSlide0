# Artifact transactions, recovery, and writer leases

## Durable commit

Every authoring save and final initialization uses `bento/artifact-transaction/v1`. The journal records operation/transaction identity, repository, base/target revisions, every target/temporary/backup revision, replacement progress, and state.

```text
preparing -> prepared -> replacing -> committed
                                  \-> rolled_back
                                  \-> report_failed
```

The writer obtains an OS-level artifact-set lease and a short transaction lock, rechecks base revisions, validates the complete candidate, prepares same-directory backups and temporary files, flushes/fsyncs them, persists the journal, replaces each target while updating journal progress, and validates the installed set. State changes such as approval invalidation are included in the same artifact transaction.

Authoring revision history uses a separate four-artifact transaction for backup HTML, JSON, registry, and a complete manifest written last. The manifest fixes each filename and byte revision plus the document and registry revisions. Number allocation and backup creation run under the authoring writer lease and in-process storage lock. Revert ignores incomplete or mismatched sets; complete legacy three-file backups receive a validated manifest before use.

The operation report is post-commit evidence. If only report writing fails, the journal becomes `report_failed`, the validated new artifacts remain, and the command returns a warning/error. Recovery retries the report and then completes the journal; it does not roll back a sound commit.

## Recovery

Recovery runs before normal storage status/read/write and at repository-aware CLI startup:

- all targets at new revisions: finish validation/report/commit;
- only some targets at new revisions: restore every target from backups;
- all targets at old revisions: finish rollback without applying;
- missing or contradictory evidence: report recovery failure and change nothing.

Reads use a consistent snapshot under the shared storage lock and cannot observe a partial replacement. Recovery must finish before the server exposes its API.

## Writer ownership

A Work editor server holds OS-exclusive per-artifact writer leases from startup to close. Locks are acquired in canonical path order. Disjoint artifact sets may proceed concurrently, while sets sharing even one target are refused; this prevents differently shaped HTML/JSON/registry/state transactions from bypassing one another. PID/session JSON is discovery evidence, not mutual exclusion.

Offline CLIs first attempt the identical lease. If it is held, they may use only a localhost server whose `/api/status` proves the same repository, editing mode, and target artifacts. If that writer cannot be identified safely, the operation is refused. This closes the server-detection/offline-write race.
