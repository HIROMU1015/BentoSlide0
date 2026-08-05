"""Serve a localhost Bento editor that saves only into the protected final artifact."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bento_converter.errors import BentoConverterError
from bento_converter.authoring_storage import AuthoringArtifactStorage
from bento_converter.work_editor import create_work_editor_server
from bento_converter.work_editor_storage import WorkEditorStorage


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--mode", choices=("authoring", "finalization"), default="finalization")
    result.add_argument("--source", required=True, type=Path, help="Regenerable presentation.generated.bento.html")
    result.add_argument("--target", required=True, type=Path, help="Protected presentation.final.bento.html")
    result.add_argument("--registry", type=Path, help="Merged registry JSON used for protected-content validation")
    result.add_argument("--source-registry", type=Path, help="Generated registry used to initialize authoring mode")
    result.add_argument("--target-registry", type=Path, help="Writable authoring registry")
    result.add_argument("--repository", type=Path, help="Repository root for writer lease identity")
    result.add_argument("--host", default="127.0.0.1", help="Loopback bind address (default: 127.0.0.1)")
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--reset-final", action="store_true", help="Explicitly replace final with generated before opening")
    result.add_argument("--allow-content-edit", action="store_true", help="Allow body/media content edits; references and registry protections remain enforced")
    result.add_argument("--backup-limit", type=int, default=10)
    return result


def run(args: argparse.Namespace) -> int:
    if args.mode == "authoring":
        if args.source_registry is None or args.target_registry is None or args.repository is None:
            raise BentoConverterError("Authoring mode requires --source-registry, --target-registry, and --repository")
        if args.reset_final:
            raise BentoConverterError("--reset-final is not valid in authoring mode")
        storage = AuthoringArtifactStorage(
            source=args.source, source_registry=args.source_registry, target=args.target,
            target_registry=args.target_registry, repository=args.repository,
            state_path=(args.repository / "deck.yaml") if (args.repository / "deck.yaml").is_file() else None,
        )
        storage.acquire_writer_lease()
    else:
        storage = WorkEditorStorage(
            source=args.source, target=args.target, registry=args.registry, reset_final=args.reset_final,
            allow_content_edit=args.allow_content_edit, backup_limit=args.backup_limit,
            repository=args.repository, hold_writer_lease=True,
        )
    server = create_work_editor_server(storage, host=args.host, port=args.port)
    host, port = server.server_address[:2]
    print(f"Bento Work editor: http://{host}:{port}/")
    print(f"Editing mode: {storage.editing_mode}")
    print(f"Generated source (read-only): {storage.source}")
    print(f"Editing target: {storage.target}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (BentoConverterError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
