"""Deterministic, read-only capture of selected files from a GitHub repository."""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence
from urllib.parse import quote, urlsplit

from .errors import BentoConverterError


GITHUB_SOURCE_FORMAT = "bento/github-repository-source/v1"
DEFAULT_INCLUDE_PATTERNS = (
    "README*",
    "LICENSE*",
    "CHANGELOG*",
    "CONTRIBUTING*",
    "SECURITY*",
    "docs/**",
    "src/**",
    "app/**",
    "lib/**",
    "scripts/**",
    ".github/workflows/**",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "requirements*.txt",
)
DEFAULT_EXCLUDE_PATTERNS = (
    ".git/**",
    ".venv/**",
    "venv/**",
    "node_modules/**",
    "vendor/**",
    "dist/**",
    "build/**",
    "target/**",
    "coverage/**",
    "__pycache__/**",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
    "output/**",
)
SAFE_BINARY_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}
MAX_REPOSITORY_FILES = 250
MAX_REPOSITORY_FILE_BYTES = 5 * 1024 * 1024
MAX_REPOSITORY_TOTAL_BYTES = 40 * 1024 * 1024
_OWNER_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class GitHubSourceError(BentoConverterError):
    """A GitHub repository could not be captured safely and deterministically."""


@dataclass(frozen=True)
class GitHubRepositoryFile:
    path: str
    payload: bytes
    mode: str = "100644"

    @property
    def content_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class GitHubRepositorySnapshot:
    repository: str
    canonical_url: str
    requested_ref: str
    resolved_commit: str
    commit_time: str
    include_patterns: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    files: tuple[GitHubRepositoryFile, ...]
    skipped: tuple[dict[str, str], ...]

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repository.split("/", 1)[1]


def normalize_github_repository(value: str) -> tuple[str, str]:
    """Return a canonical HTTPS URL and owner/name identity."""

    candidate = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", candidate):
        candidate = "https://github.com/" + candidate
    parsed = urlsplit(candidate)
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GitHubSourceError(
            "GitHub source must be an HTTPS github.com repository URL without credentials, query, or fragment"
        )
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise GitHubSourceError("GitHub source URL must identify exactly owner/repository")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository or not all(
        _OWNER_REPOSITORY_PATTERN.fullmatch(part) for part in (owner, repository)
    ):
        raise GitHubSourceError("GitHub owner/repository contains unsupported characters")
    identity = f"{owner}/{repository}"
    return f"https://github.com/{identity}.git", identity


def validate_requested_ref(value: str) -> str:
    ref = value.strip()
    if (
        not ref
        or ref.startswith("-")
        or ref.endswith("/")
        or ".." in ref
        or "@{" in ref
        or "//" in ref
        or re.search(r"[\x00-\x20\x7f~^:?*\\\[]", ref)
    ):
        raise GitHubSourceError("GitHub ref is unsafe or invalid")
    return ref


def _match(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _secret_reason(path: str) -> str | None:
    parsed = PurePosixPath(path)
    lowered = parsed.name.casefold()
    secret_directories = {".secrets", "secrets", ".credentials", "credentials"}
    if any(part.casefold() in secret_directories for part in parsed.parts[:-1]):
        return "secret-like directory"
    if lowered == ".env" or lowered.startswith(".env."):
        return "secret-like environment file"
    if lowered in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "credentials.json"}:
        return "credential-like file"
    if lowered.endswith((".pem", ".key", ".p12", ".pfx", ".keystore")):
        return "private-key or credential container"
    if re.search(r"(^|[._-])(secret|secrets|credential|credentials)([._-]|$)", lowered):
        return "secret-like file name"
    return None


def _portable_path_reason(path: str) -> str | None:
    parsed = PurePosixPath(path)
    if "\\" in path or len(path) > 180:
        return "path is not portable to the local snapshot"
    for part in parsed.parts:
        if (
            not part
            or len(part) > 120
            or part.endswith((" ", "."))
            or any(character in '<>:"\\|?*' for character in part)
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            return "path is not portable to the local snapshot"
    return None


def select_repository_paths(
    entries: Iterable[tuple[str, str]],
    *,
    include_patterns: Sequence[str],
    exclude_patterns: Sequence[str],
) -> tuple[tuple[tuple[str, str], ...], tuple[dict[str, str], ...]]:
    """Select safe tracked blobs without reading their content."""

    includes = tuple(include_patterns) or DEFAULT_INCLUDE_PATTERNS
    excludes = (*DEFAULT_EXCLUDE_PATTERNS, *tuple(exclude_patterns))
    selected: list[tuple[str, str]] = []
    skipped: list[dict[str, str]] = []
    portable_paths: set[str] = set()
    for mode, raw_path in sorted(entries, key=lambda value: value[1].casefold()):
        path = PurePosixPath(raw_path).as_posix()
        if path != raw_path or path.startswith("/") or ".." in PurePosixPath(path).parts:
            skipped.append({"path": raw_path, "reason": "unsafe repository path"})
            continue
        if any(ord(character) < 32 or ord(character) == 127 for character in path):
            skipped.append({"path": path, "reason": "control character in path"})
            continue
        portable_reason = _portable_path_reason(path)
        if portable_reason:
            skipped.append({"path": path, "reason": portable_reason})
            continue
        portable_key = path.casefold()
        if portable_key in portable_paths:
            skipped.append({"path": path, "reason": "case-insensitive local path collision"})
            continue
        if mode not in {"100644", "100755"}:
            skipped.append({"path": path, "reason": "symlink, submodule, or unsupported Git mode"})
            continue
        secret_reason = _secret_reason(path)
        if secret_reason:
            skipped.append({"path": path, "reason": secret_reason})
            continue
        if _match(path, excludes):
            skipped.append({"path": path, "reason": "excluded path"})
            continue
        if not _match(path, includes):
            continue
        portable_paths.add(portable_key)
        selected.append((mode, path))
    if len(selected) > MAX_REPOSITORY_FILES:
        raise GitHubSourceError(
            f"GitHub source selects {len(selected)} files; narrow --include to at most {MAX_REPOSITORY_FILES}"
        )
    return tuple(selected), tuple(skipped)


def _validate_payload(path: str, payload: bytes) -> str | None:
    if len(payload) > MAX_REPOSITORY_FILE_BYTES:
        return f"file exceeds {MAX_REPOSITORY_FILE_BYTES} bytes"
    if payload.startswith(_LFS_PREFIX):
        return "Git LFS pointer was not resolved"
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix not in SAFE_BINARY_SUFFIXES:
        if b"\0" in payload:
            return "binary content is not an approved source format"
        try:
            payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            return "text source is not UTF-8"
    return None


def build_github_snapshot(
    *,
    canonical_url: str,
    repository: str,
    requested_ref: str,
    resolved_commit: str,
    commit_time: str,
    entries: Iterable[tuple[str, str]],
    read_blob: Callable[[str], bytes],
    include_patterns: Sequence[str] = (),
    exclude_patterns: Sequence[str] = (),
) -> GitHubRepositorySnapshot:
    if not _COMMIT_PATTERN.fullmatch(resolved_commit):
        raise GitHubSourceError("GitHub source did not resolve to a 40-character commit SHA")
    selected, initial_skipped = select_repository_paths(
        entries, include_patterns=include_patterns, exclude_patterns=exclude_patterns,
    )
    files: list[GitHubRepositoryFile] = []
    skipped = list(initial_skipped)
    total_bytes = 0
    for mode, path in selected:
        payload = read_blob(path)
        reason = _validate_payload(path, payload)
        if reason:
            skipped.append({"path": path, "reason": reason})
            continue
        total_bytes += len(payload)
        if total_bytes > MAX_REPOSITORY_TOTAL_BYTES:
            raise GitHubSourceError(
                f"GitHub source exceeds {MAX_REPOSITORY_TOTAL_BYTES} selected bytes; narrow --include"
            )
        files.append(GitHubRepositoryFile(path=path, payload=payload, mode=mode))
    if not files:
        raise GitHubSourceError("GitHub source selection produced no safe readable files")
    return GitHubRepositorySnapshot(
        repository=repository,
        canonical_url=canonical_url,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        commit_time=commit_time,
        include_patterns=tuple(include_patterns) or DEFAULT_INCLUDE_PATTERNS,
        exclude_patterns=tuple(exclude_patterns),
        files=tuple(files),
        skipped=tuple(sorted(skipped, key=lambda value: (value["path"].casefold(), value["reason"]))),
    )


def _run_git(arguments: Sequence[str], *, timeout: int = 120) -> bytes:
    environment = os.environ.copy()
    environment.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
    })
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            list(arguments), check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, timeout=timeout, creationflags=creationflags,
        )
    except FileNotFoundError as exc:
        raise GitHubSourceError("Git is required to freeze a GitHub repository source") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubSourceError("GitHub repository read timed out") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        summary = detail[-1] if detail else f"exit code {result.returncode}"
        raise GitHubSourceError(f"GitHub repository read failed: {summary}")
    return result.stdout


def _parse_tree(payload: bytes) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for record in payload.split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded_path = record.split(b"\t", 1)
            mode, _object_type, _object_id = metadata.split(b" ", 2)
            path = encoded_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubSourceError("GitHub repository contains an unsupported tree entry") from exc
        entries.append((mode.decode("ascii"), path))
    return tuple(entries)


def capture_github_repository(
    repository_value: str,
    *,
    requested_ref: str = "HEAD",
    include_patterns: Sequence[str] = (),
    exclude_patterns: Sequence[str] = (),
) -> GitHubRepositorySnapshot:
    """Fetch one Git commit without checkout and return selected immutable bytes."""

    canonical_url, repository = normalize_github_repository(repository_value)
    requested_ref = validate_requested_ref(requested_ref)
    with tempfile.TemporaryDirectory(prefix="bento-github-source-") as temporary:
        git_directory = Path(temporary) / "repository.git"
        prefix = ("git", "--git-dir", str(git_directory))
        _run_git((*prefix, "init", "--bare"))
        _run_git((*prefix, "remote", "add", "origin", canonical_url))
        _run_git((
            *prefix, "-c", "protocol.version=2", "fetch", "--depth=1",
            "--filter=blob:none", "origin", requested_ref,
        ))
        resolved_commit = _run_git((*prefix, "rev-parse", "FETCH_HEAD^{commit}")).decode("ascii").strip().lower()
        commit_time = _run_git(
            (*prefix, "show", "-s", "--format=%cI", resolved_commit),
        ).decode("utf-8").strip()
        entries = _parse_tree(_run_git((*prefix, "ls-tree", "-rz", "--full-tree", resolved_commit)))
        selected, _skipped = select_repository_paths(
            entries, include_patterns=include_patterns, exclude_patterns=exclude_patterns,
        )
        if not selected:
            raise GitHubSourceError("GitHub source selection produced no safe tracked files")
        selected_paths = tuple(path for _mode, path in selected)
        archive = _run_git(
            (*prefix, "archive", "--format=tar", resolved_commit, "--", *selected_paths),
            timeout=180,
        )
        blobs: dict[str, bytes] = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source_archive:
                for member in source_archive.getmembers():
                    if not member.isfile() or member.name not in selected_paths:
                        continue
                    stream = source_archive.extractfile(member)
                    if stream is None:
                        raise GitHubSourceError(f"Cannot read archived GitHub source file: {member.name}")
                    blobs[member.name] = stream.read()
        except tarfile.TarError as exc:
            raise GitHubSourceError("GitHub source archive is invalid") from exc
        missing = sorted(set(selected_paths) - set(blobs))
        if missing:
            raise GitHubSourceError("GitHub source archive omitted selected files: " + ", ".join(missing))

        def read_blob(path: str) -> bytes:
            return blobs[path]

        return build_github_snapshot(
            canonical_url=canonical_url,
            repository=repository,
            requested_ref=requested_ref,
            resolved_commit=resolved_commit,
            commit_time=commit_time,
            entries=entries,
            read_blob=read_blob,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )


def github_blob_url(snapshot: GitHubRepositorySnapshot, repository_path: str) -> str:
    return (
        f"https://github.com/{snapshot.repository}/blob/{snapshot.resolved_commit}/"
        + quote(repository_path, safe="/")
    )


def descriptor_payload(
    snapshot: GitHubRepositorySnapshot,
    *,
    local_paths: dict[str, str],
) -> bytes:
    files = []
    for source in snapshot.files:
        files.append({
            "repositoryPath": source.path,
            "localPath": local_paths[source.path],
            "sourceUrl": github_blob_url(snapshot, source.path),
            "gitMode": source.mode,
            "byteLength": len(source.payload),
            "contentDigest": source.content_digest,
        })
    value = {
        "format": GITHUB_SOURCE_FORMAT,
        "provider": "github",
        "repository": snapshot.repository,
        "canonicalUrl": snapshot.canonical_url,
        "requestedRef": snapshot.requested_ref,
        "resolvedCommit": snapshot.resolved_commit,
        "commitTime": snapshot.commit_time,
        "include": list(snapshot.include_patterns),
        "exclude": list(snapshot.exclude_patterns),
        "files": files,
        "skipped": list(snapshot.skipped),
    }
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def load_and_validate_descriptor(root: Path, descriptor_path: Path) -> dict[str, object]:
    """Validate descriptor identity and every captured file digest."""

    try:
        value = json.loads(descriptor_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubSourceError(f"Cannot read GitHub source descriptor {descriptor_path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("format") != GITHUB_SOURCE_FORMAT:
        raise GitHubSourceError(f"GitHub source descriptor format is invalid: {descriptor_path}")
    canonical_url, repository = normalize_github_repository(str(value.get("canonicalUrl", "")))
    if value.get("provider") != "github" or value.get("repository") != repository:
        raise GitHubSourceError("GitHub source descriptor repository identity is inconsistent")
    if canonical_url != value.get("canonicalUrl"):
        raise GitHubSourceError("GitHub source descriptor URL is not canonical")
    commit = str(value.get("resolvedCommit", ""))
    if not _COMMIT_PATTERN.fullmatch(commit):
        raise GitHubSourceError("GitHub source descriptor commit SHA is invalid")
    try:
        requested_ref = validate_requested_ref(str(value.get("requestedRef", "")))
    except GitHubSourceError as exc:
        raise GitHubSourceError("GitHub source descriptor requested ref is invalid") from exc
    if requested_ref != value.get("requestedRef"):
        raise GitHubSourceError("GitHub source descriptor requested ref is not canonical")
    commit_time = value.get("commitTime")
    try:
        parsed_commit_time = datetime.fromisoformat(str(commit_time))
    except ValueError as exc:
        raise GitHubSourceError("GitHub source descriptor commit time is invalid") from exc
    if parsed_commit_time.tzinfo is None:
        raise GitHubSourceError("GitHub source descriptor commit time requires a timezone")
    for field in ("include", "exclude"):
        patterns = value.get(field)
        if not isinstance(patterns, list) or any(
            not isinstance(pattern, str) or not pattern for pattern in patterns
        ):
            raise GitHubSourceError(f"GitHub source descriptor {field} patterns are invalid")
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise GitHubSourceError("GitHub source descriptor must contain captured files")
    seen: set[str] = set()
    seen_repository_paths: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise GitHubSourceError("GitHub source descriptor file entry is invalid")
        local_path = entry.get("localPath")
        if not isinstance(local_path, str) or not local_path or local_path in seen:
            raise GitHubSourceError("GitHub source descriptor localPath is invalid or duplicated")
        seen.add(local_path)
        repository_path = entry.get("repositoryPath")
        if (
            not isinstance(repository_path, str)
            or not repository_path
            or repository_path in seen_repository_paths
            or PurePosixPath(repository_path).as_posix() != repository_path
            or repository_path.startswith("/")
            or ".." in PurePosixPath(repository_path).parts
            or _portable_path_reason(repository_path) is not None
        ):
            raise GitHubSourceError("GitHub source descriptor repositoryPath is invalid or duplicated")
        seen_repository_paths.add(repository_path)
        if entry.get("gitMode") not in {"100644", "100755"}:
            raise GitHubSourceError("GitHub source descriptor Git mode is invalid")
        expected_source_url = (
            f"https://github.com/{repository}/blob/{commit}/"
            + quote(repository_path, safe="/")
        )
        if entry.get("sourceUrl") != expected_source_url:
            raise GitHubSourceError("GitHub source descriptor blob URL is inconsistent")
        if (
            PurePosixPath(local_path).as_posix() != local_path
            or local_path.startswith("/")
            or ".." in PurePosixPath(local_path).parts
            or "\\" in local_path
        ):
            raise GitHubSourceError("GitHub source descriptor localPath is unsafe")
        candidate = (root / local_path).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise GitHubSourceError("GitHub source descriptor file escapes the repository") from exc
        expected_candidate = (descriptor_path.parent / "files" / PurePosixPath(repository_path)).resolve()
        if candidate != expected_candidate:
            raise GitHubSourceError("GitHub source descriptor local snapshot path is inconsistent")
        if not candidate.is_file():
            raise GitHubSourceError(f"GitHub source snapshot file is missing: {local_path}")
        payload = candidate.read_bytes()
        expected = entry.get("contentDigest")
        actual = "sha256:" + hashlib.sha256(payload).hexdigest()
        if (
            not isinstance(expected, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected)
            or expected != actual
            or not isinstance(entry.get("byteLength"), int)
            or entry.get("byteLength") != len(payload)
        ):
            raise GitHubSourceError(f"GitHub source snapshot file changed: {local_path}")
    return value
