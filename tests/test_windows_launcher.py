from __future__ import annotations

import hashlib
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from bento_converter.html_document import embed_bento_doc, extract_bento_doc, load_html


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"


@unittest.skipUnless(WINDOWS, "Windows-only launcher tests")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repositories: list[Path] = []

    def tearDown(self) -> None:
        for repository in self.repositories:
            if (repository / "output" / "work-editor-session.json").is_file():
                self.run_powershell(repository / "scripts" / "stop_bento_editor.ps1", timeout=20)
        self.temporary.cleanup()

    def copy_repository(self, name: str) -> Path:
        repository = self.root / name
        repository.mkdir(parents=True)
        shutil.copytree(ROOT / "bento_converter", repository / "bento_converter", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "scripts", repository / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for filename in ("start_bento_editor.cmd", "stop_bento_editor.cmd", "demo.bento.html"):
            shutil.copy2(ROOT / filename, repository / filename)
        self.prepare_default_files(repository)
        self.repositories.append(repository)
        return repository

    @staticmethod
    def prepare_default_files(repository: Path) -> None:
        output = repository / "output"
        diagnostics = output / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repository / "demo.bento.html", output / "presentation.generated.bento.html")
        (diagnostics / "merged-registry.json").write_text("{}\n", encoding="utf-8")

    @staticmethod
    def free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    @staticmethod
    def file_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def run_powershell(self, script: Path, *arguments: str, timeout: int = 40) -> subprocess.CompletedProcess[str]:
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *map(str, arguments)]
        return self.run_without_pipes(command, timeout=timeout)

    def run_cmd(self, script: Path, *arguments: str, timeout: int = 40) -> subprocess.CompletedProcess[str]:
        command = ["cmd.exe", "/d", "/c", str(script), *map(str, arguments)]
        return self.run_without_pipes(command, timeout=timeout)

    def run_without_pipes(self, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryFile() as capture:
            result = subprocess.run(
                command, cwd=self.root, stdout=capture, stderr=subprocess.STDOUT, timeout=timeout,
                env={**os.environ, "BENTO_EDITOR_NO_PAUSE": "1"},
            )
            capture.seek(0)
            output = capture.read().decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(result.args, result.returncode, output, "")

    @staticmethod
    def wait_for_status(port: int, *, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (OSError, URLError, json.JSONDecodeError):
                time.sleep(0.1)
        raise AssertionError(f"Work editor status did not become available on port {port}")

    @staticmethod
    def read_session(repository: Path) -> dict:
        return json.loads((repository / "output" / "work-editor-session.json").read_text(encoding="utf-8-sig"))

    def test_cmd_start_duplicate_stop_and_existing_final_protection(self) -> None:
        repository = self.copy_repository("Bento Slide")
        port = self.free_port()
        source = repository / "output" / "presentation.generated.bento.html"
        target = repository / "output" / "presentation.final.bento.html"
        html = load_html(source)
        document = extract_bento_doc(html)
        next(element for slide in document["slides"] for element in slide["elements"] if element["type"] == "shape")["x"] += 9
        target.write_text(embed_bento_doc(html, document), encoding="utf-8")
        source_hash = self.file_hash(source)
        target_hash = self.file_hash(target)

        started = self.run_cmd(repository / "start_bento_editor.cmd", "-Port", str(port), "-NoClipboard")
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        status = self.wait_for_status(port)
        session = self.read_session(repository)
        first_pid = session["pid"]
        self.assertEqual(session["format"], "bento/work-editor-session/v1")
        self.assertEqual(session["url"], f"http://127.0.0.1:{port}/")
        self.assertEqual(status["target"], "output/presentation.final.bento.html")
        self.assertTrue(status["revision"].startswith("sha256:"))
        self.assertIn(status["validation"], {"pass", "fail"})

        duplicate = self.run_cmd(repository / "start_bento_editor.cmd", "-Port", str(port), "-NoClipboard")
        self.assertEqual(duplicate.returncode, 0, duplicate.stdout + duplicate.stderr)
        self.assertEqual(self.read_session(repository)["pid"], first_pid)
        self.assertEqual(self.file_hash(source), source_hash)
        self.assertEqual(self.file_hash(target), target_hash)
        log = (repository / "output" / "work-editor.log").read_text(encoding="utf-8-sig")
        self.assertNotIn("--reset-final", log)
        self.assertNotIn("--allow-content-edit", log)

        stopped = self.run_cmd(repository / "stop_bento_editor.cmd")
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertFalse((repository / "output" / "work-editor.pid").exists())
        self.assertFalse((repository / "output" / "work-editor-session.json").exists())
        with self.assertRaises((OSError, URLError)):
            urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1)
        self.assertEqual(self.file_hash(source), source_hash)
        self.assertEqual(self.file_hash(target), target_hash)

        stopped_again = self.run_cmd(repository / "stop_bento_editor.cmd")
        self.assertEqual(stopped_again.returncode, 0, stopped_again.stdout + stopped_again.stderr)

    def test_japanese_repository_path_and_custom_relative_paths(self) -> None:
        repository = self.copy_repository("論文スライド")
        port = self.free_port()
        custom = repository / "素材" / "章 一"
        custom.mkdir(parents=True)
        source = custom / "論文.generated.bento.html"
        target = custom / "論文.final.bento.html"
        registry = custom / "統合 registry.json"
        shutil.copy2(repository / "demo.bento.html", source)
        registry.write_text("{}\n", encoding="utf-8")
        source_hash = self.file_hash(source)

        started = self.run_powershell(
            repository / "scripts" / "start_bento_editor.ps1",
            "-Port", str(port), "-Source", str(source.relative_to(repository)),
            "-Target", str(target.relative_to(repository)), "-Registry", str(registry.relative_to(repository)),
            "-NoClipboard",
        )
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertEqual(self.wait_for_status(port)["target"], "素材/章 一/論文.final.bento.html")
        session = self.read_session(repository)
        self.assertEqual(Path(session["source"]), source.resolve())
        self.assertEqual(Path(session["target"]), target.resolve())
        self.assertEqual(Path(session["registry"]), registry.resolve())

        stopped = self.run_powershell(repository / "scripts" / "stop_bento_editor.ps1")
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        with self.assertRaises((OSError, URLError)):
            urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1)
        self.assertEqual(self.file_hash(source), source_hash)
        self.assertTrue(target.is_file())

    def test_unrelated_port_owner_is_not_stopped(self) -> None:
        repository = self.copy_repository("BentoSlide0")
        port = self.free_port()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = self.run_powershell(
                repository / "scripts" / "start_bento_editor.ps1", "-Port", str(port), "-NoClipboard",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("-Port 8766", result.stdout + result.stderr)
            self.assertTrue(thread.is_alive())
            with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                self.assertEqual(response.status, 200)
            self.assertFalse((repository / "output" / "work-editor-session.json").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_repository_mutex_rejects_concurrent_launcher(self) -> None:
        repository = self.copy_repository("Bento concurrent start")
        common = repository / "scripts" / "bento_editor_launcher.common.ps1"
        command = (
            f". '{common}'; "
            f"$handle = Enter-BentoLauncherMutex -Repository '{repository}'; "
            "if (-not $handle.Acquired) { exit 2 }; "
            "[Console]::Out.WriteLine('ready'); [Console]::Out.Flush(); "
            "try { Start-Sleep -Seconds 30 } finally { Exit-BentoLauncherMutex -Handle $handle }"
        )
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        holder = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
            cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            ready = False
            for _ in range(10):
                if b"ready" in holder.stdout.readline():
                    ready = True
                    break
            self.assertTrue(ready, "mutex holder did not report readiness")
            started_at = time.monotonic()
            result = self.run_powershell(
                repository / "scripts" / "start_bento_editor.ps1",
                "-Port", str(self.free_port()), "-NoClipboard",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertLess(time.monotonic() - started_at, 5)
            self.assertFalse((repository / "output" / "work-editor-session.json").exists())
        finally:
            holder.terminate()
            holder.wait(timeout=10)
            holder.stdout.close()

    def test_stale_and_reused_pid_are_handled_safely(self) -> None:
        repository = self.copy_repository("Bento stale session")
        state = repository / "output"
        port = self.free_port()
        session_path = state / "work-editor-session.json"
        pid_path = state / "work-editor.pid"

        actual_start = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", f"(Get-Process -Id {os.getpid()}).StartTime.ToUniversalTime().ToString('o')"],
            text=True, capture_output=True, encoding="utf-8", errors="replace", check=True,
        ).stdout.strip()
        session = {
            "format": "bento/work-editor-session/v1", "pid": os.getpid(),
            "startedAt": "2026-01-01T00:00:00Z", "processStartTimeUtc": actual_start,
            "repository": str(repository.resolve()),
            "source": str((state / "presentation.generated.bento.html").resolve()),
            "target": str((state / "presentation.final.bento.html").resolve()),
            "registry": str((state / "diagnostics" / "merged-registry.json").resolve()),
            "host": "127.0.0.1", "port": port, "url": f"http://127.0.0.1:{port}/",
        }
        session_path.write_text(json.dumps(session), encoding="utf-8")
        pid_path.write_text(str(os.getpid()), encoding="ascii")
        command_mismatch = self.run_powershell(repository / "scripts" / "stop_bento_editor.ps1")
        self.assertNotEqual(command_mismatch.returncode, 0)
        self.assertTrue(session_path.exists())

        session["processStartTimeUtc"] = "2000-01-01T00:00:00Z"
        session_path.write_text(json.dumps(session), encoding="utf-8")
        start_mismatch = self.run_powershell(repository / "scripts" / "stop_bento_editor.ps1")
        self.assertNotEqual(start_mismatch.returncode, 0)
        self.assertTrue(session_path.exists())

        missing_pid = 2_000_000_000
        session["pid"] = missing_pid
        session_path.write_text(json.dumps(session), encoding="utf-8")
        pid_path.write_text(str(missing_pid), encoding="ascii")
        nonexistent = self.run_powershell(repository / "scripts" / "stop_bento_editor.ps1")
        self.assertEqual(nonexistent.returncode, 0, nonexistent.stdout + nonexistent.stderr)
        self.assertFalse(session_path.exists())
        self.assertFalse(pid_path.exists())


if __name__ == "__main__":
    unittest.main()
