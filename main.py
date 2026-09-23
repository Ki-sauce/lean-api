import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel


# ============================================================
# Configuration
# ============================================================

LEAN_PROJECT = Path("/app/leanverify")

LEAN_PATH = [
    str(LEAN_PROJECT / ".lake" / "build" / "lib" / "lean"),
]

packages_dir = LEAN_PROJECT / ".lake" / "packages"

if packages_dir.exists():
    for package in packages_dir.iterdir():
        package_lib = package / ".lake" / "build" / "lib" / "lean"
        if package_lib.exists():
            LEAN_PATH.append(str(package_lib))


def lean_env() -> dict[str, str]:
    env = os.environ.copy()

    existing = env.get("LEAN_PATH")

    paths = LEAN_PATH.copy()

    if existing:
        paths.append(existing)

    env["LEAN_PATH"] = os.pathsep.join(paths)

    return env


# ============================================================
# JSON-RPC / LSP transport
# ============================================================

class LeanServerError(Exception):
    pass


class LeanServer:
    def __init__(self):
        self.process: subprocess.Popen[str] | None = None

        self.reader_thread: threading.Thread | None = None
        self.reader_error: str | None = None

        self.write_lock = threading.Lock()

        self.pending: dict[Any, Queue] = {}
        self.pending_lock = threading.Lock()

        self.notification_queue: Queue = Queue()

        self.next_id = 1

        self.started = False

    # --------------------------------------------------------
    # Process lifecycle
    # --------------------------------------------------------

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return

        self.stop()

        self.reader_error = None

        self.process = subprocess.Popen(
            ["lean", "--server"],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )

        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
        )
        self.reader_thread.start()

        threading.Thread(
            target=self._stderr_loop,
            daemon=True,
        ).start()

        self._initialize()

        self.started = True

    def stop(self):
        process = self.process

        self.process = None
        self.started = False

        if process is None:
            return

        try:
            if process.poll() is None:
                process.terminate()

                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        except Exception:
            pass

    def alive(self) -> bool:
        return (
            self.process is not None
            and self.process.poll() is None
        )

    # --------------------------------------------------------
    # LSP framing
    # --------------------------------------------------------

    def _send_raw(self, message: dict):
        if self.process is None or self.process.stdin is None:
            raise LeanServerError("Lean server is not running")

        body = json.dumps(
            message,
            separators=(",", ":"),
        ).encode("utf-8")

        header = (
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode("ascii")

        with self.write_lock:
            self.process.stdin.write(header + body)
            self.process.stdin.flush()

    def _send_request(self, method: str, params: dict | None = None):
        request_id = self.next_id
        self.next_id += 1

        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }

        if params is not None:
            message["params"] = params

        queue = Queue(maxsize=1)

        with self.pending_lock:
            self.pending[request_id] = queue

        try:
            self._send_raw(message)
        except Exception:
            with self.pending_lock:
                self.pending.pop(request_id, None)
            raise

        return request_id, queue

    def _send_notification(
        self,
        method: str,
        params: dict | None = None,
    ):
        message = {
            "jsonrpc": "2.0",
            "method": method,
        }

        if params is not None:
            message["params"] = params

        self._send_raw(message)

    def _send_response(
        self,
        request_id,
        result=None,
        error=None,
    ):
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
        }

        if error is not None:
            message["error"] = error
        else:
            message["result"] = result

        self._send_raw(message)

    # --------------------------------------------------------
    # Reader
    # --------------------------------------------------------

    def _read_message(self):
        if self.process is None or self.process.stdout is None:
            raise LeanServerError("Lean stdout is unavailable")

        headers: dict[str, str] = {}

        while True:
            line = self.process.stdout.readline()

            if not line:
                raise EOFError("Lean server stdout closed")

            line = line.decode("ascii").strip()

            if not line:
                break

            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        content_length = headers.get("content-length")

        if content_length is None:
            raise LeanServerError(
                f"Missing Content-Length header: {headers}"
            )

        length = int(content_length)

        body = self.process.stdout.read(length)

        if len(body) != length:
            raise EOFError("Incomplete LSP message")

        return json.loads(body.decode("utf-8"))

    def _reader_loop(self):
        try:
            while True:
                message = self._read_message()

                # Response to one of our requests.
                if "id" in message and (
                    "result" in message or "error" in message
                ):
                    request_id = message["id"]

                    with self.pending_lock:
                        queue = self.pending.get(request_id)

                    if queue is not None:
                        queue.put(message)

                    continue

                # Server request.
                if (
                    "id" in message
                    and "method" in message
                ):
                    self._handle_server_request(message)
                    continue

                # Server notification.
                if "method" in message:
                    self.notification_queue.put(message)

        except Exception as exc:
            self.reader_error = repr(exc)

    def _stderr_loop(self):
        process = self.process

        if process is None or process.stderr is None:
            return

        try:
            while True:
                line = process.stderr.readline()

                if not line:
                    break

                # Keep stderr drained.
                # Lean can emit useful diagnostics here, but the
                # authoritative verification result comes from LSP
                # diagnostics below.
        except Exception:
            pass

    # --------------------------------------------------------
    # Server requests
    # --------------------------------------------------------

    def _handle_server_request(self, message: dict):
        request_id = message["id"]
        method = message["method"]

        # Lean asks the client to register file watchers.
        if method == "client/registerCapability":
            self._send_response(request_id, None)
            return

        # Workspace configuration.
        if method == "workspace/configuration":
            items = message.get("params", {}).get("items", [])
            self._send_response(
                request_id,
                [None for _ in items],
            )
            return

        # Lean can ask the client to apply edits.
        if method == "workspace/applyEdit":
            self._send_response(
                request_id,
                {"applied": False},
            )
            return

        # Unknown server request: return null rather than hanging
        # the Lean server.
        self._send_response(request_id, None)

    # --------------------------------------------------------
    # Initialization
    # --------------------------------------------------------

    def _initialize(self):
        if self.process is None:
            raise LeanServerError("Server not started")

        root_uri = (
            "file://"
            + str(LEAN_PROJECT.resolve()).replace(" ", "%20")
        )

        params = {
            "processId": os.getpid(),
            "clientInfo": {
                "name": "leanverify",
                "version": "1.0",
            },
            "rootUri": root_uri,
            "workspaceFolders": [
                {
                    "uri": root_uri,
                    "name": LEAN_PROJECT.name,
                }
            ],
            "capabilities": {
                "workspace": {
                    "configuration": True,
                    "workspaceFolders": True,
                },
                "textDocument": {
                    "publishDiagnostics": {
                        "versionSupport": True,
                    }
                },
            },
            "initializationOptions": {},
        }

        request_id, queue = self._send_request(
            "initialize",
            params,
        )

        try:
            response = queue.get(timeout=30)
        except Empty:
            with self.pending_lock:
                self.pending.pop(request_id, None)

            raise LeanServerError(
                "Timed out waiting for Lean initialize"
            )

        with self.pending_lock:
            self.pending.pop(request_id, None)

        if "error" in response:
            raise LeanServerError(
                f"Lean initialize failed: {response['error']}"
            )

        self._send_notification(
            "initialized",
            {},
        )

    # --------------------------------------------------------
    # Document verification
    # --------------------------------------------------------

    def check(
        self,
        source: str,
        timeout: float = 120,
    ) -> dict:
        self.start()

        # Drain stale notifications from previous checks.
        while True:
            try:
                self.notification_queue.get_nowait()
            except Empty:
                break

        # Use a unique URI so each request is an independent document.
        document_id = uuid.uuid4().hex

        uri = (
            "file://"
            + str(
                LEAN_PROJECT / f"_verify_{document_id}.lean"
            )
        )

        version = 1

        diagnostics: list[dict] = []

        start = time.perf_counter()

        self._send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "lean",
                    "version": version,
                    "text": source,
                }
            },
        )

        deadline = time.monotonic() + timeout

        finished = False
        fatal_progress = False

        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()

                try:
                    message = self.notification_queue.get(
                        timeout=min(remaining, 1.0)
                    )
                except Empty:
                    if not self.alive():
                        raise LeanServerError(
                            "Lean server exited during verification"
                        )

                    continue

                method = message.get("method")
                params = message.get("params", {})

                # ------------------------------------------------
                # Diagnostics
                # ------------------------------------------------

                if method == "textDocument/publishDiagnostics":
                    document = params.get(
                        "uri"
                    )

                    if document != uri:
                        continue

                    incoming = params.get(
                        "diagnostics",
                        [],
                    )

                    # Lean can publish diagnostics incrementally.
                    # If isIncremental is false/absent, this is a
                    # replacement set.
                    if params.get(
                        "isIncremental",
                        False,
                    ):
                        diagnostics.extend(incoming)
                    else:
                        diagnostics = incoming

                    continue

                # ------------------------------------------------
                # Lean processing state
                # ------------------------------------------------

                if method == "$/lean/fileProgress":
                    document = (
                        params
                        .get("textDocument", {})
                        .get("uri")
                    )

                    if document != uri:
                        continue

                    processing = params.get(
                        "processing",
                        [],
                    )

                    if processing:
                        # Still elaborating.
                        continue

                    # Empty processing array means Lean has finished
                    # processing this document.
                    finished = True
                    break

        finally:
            try:
                self._send_notification(
                    "textDocument/didClose",
                    {
                        "textDocument": {
                            "uri": uri,
                        }
                    },
                )
            except Exception:
                pass

        elapsed = time.perf_counter() - start

        if not finished:
            return {
                "success": False,
                "exit_code": None,
                "elapsed_seconds": round(elapsed, 3),
                "error": "Lean server timed out",
                "diagnostics": format_diagnostics(
                    diagnostics
                ),
            }

        errors = [
            d
            for d in diagnostics
            if d.get("severity") == 1
        ]

        success = (
            len(errors) == 0
            and not fatal_progress
        )

        return {
            "success": success,
            "exit_code": 0 if success else 1,
            "elapsed_seconds": round(elapsed, 3),
            "diagnostics": format_diagnostics(
                diagnostics
            ),
        }


# ============================================================
# Diagnostics
# ============================================================

def format_diagnostics(
    diagnostics: list[dict],
) -> str:
    if not diagnostics:
        return ""

    lines: list[str] = []

    severity_names = {
        1: "error",
        2: "warning",
        3: "info",
        4: "hint",
    }

    for diagnostic in diagnostics:
        severity = severity_names.get(
            diagnostic.get("severity"),
            "diagnostic",
        )

        message = diagnostic.get(
            "message",
            "",
        )

        range_info = diagnostic.get(
            "range",
            {},
        )

        start = range_info.get(
            "start",
            {},
        )

        line = start.get(
            "line",
            0,
        ) + 1

        character = start.get(
            "character",
            0,
        ) + 1

        lines.append(
            f"{severity}:"
            f"{line}:{character}: "
            f"{message}"
        )

    return "\n".join(lines)


# ============================================================
# Direct Lean fallback
# ============================================================

def verify_direct(
    source: str,
    timeout: int = 120,
) -> dict:
    start = time.perf_counter()

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".lean",
        prefix="_verify_",
        dir=LEAN_PROJECT,
        delete=False,
        encoding="utf-8",
    ) as f:
        path = Path(f.name)
        f.write(source)

    try:
        process = subprocess.run(
            ["lean", str(path)],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        diagnostics = "\n".join(
            x
            for x in [
                process.stdout,
                process.stderr,
            ]
            if x
        ).strip()

        return {
            "success": process.returncode == 0,
            "exit_code": process.returncode,
            "elapsed_seconds": round(
                time.perf_counter() - start,
                3,
            ),
            "diagnostics": diagnostics,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "exit_code": None,
            "elapsed_seconds": round(
                time.perf_counter() - start,
                3,
            ),
            "error": "Lean process timed out",
            "diagnostics": "",
        }

    finally:
        path.unlink(missing_ok=True)


# ============================================================
# Global server
# ============================================================

lean_server = LeanServer()


# ============================================================
# FastAPI
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    lean_server.stop()


app = FastAPI(
    title="Lean Verifier",
    lifespan=lifespan,
)


class CheckRequest(BaseModel):
    source: str


# ============================================================
# /check
# ============================================================

@app.post("/check")
def check(request: CheckRequest):
    try:
        return lean_server.check(
            request.source,
            timeout=120,
        )

    except Exception as exc:
        # Kill the persistent server so the next request gets a
        # completely fresh Lean process.
        lean_server.stop()

        return {
            "success": False,
            "exit_code": None,
            "elapsed_seconds": 0,
            "error": repr(exc),
            "diagnostics": "",
        }


# ============================================================
# /health
# ============================================================

@app.get("/health")
def health():
    result: dict[str, Any] = {
        "status": "ok",
        "lean": {},
        "lake": {},
        "mathlib": {},
        "server": {},
    }

    # --------------------------------------------------------
    # Lean version
    # --------------------------------------------------------

    try:
        process = subprocess.run(
            ["lean", "--version"],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=15,
        )

        result["lean"] = {
            "ok": process.returncode == 0,
            "version": process.stdout.strip(),
            "error": (
                process.stderr.strip()
                if process.returncode != 0
                else None
            ),
        }

    except Exception as exc:
        result["lean"] = {
            "ok": False,
            "version": None,
            "error": repr(exc),
        }

    # --------------------------------------------------------
    # Lake version
    # --------------------------------------------------------

    try:
        process = subprocess.run(
            ["lake", "--version"],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=15,
        )

        result["lake"] = {
            "ok": process.returncode == 0,
            "version": process.stdout.strip(),
            "error": (
                process.stderr.strip()
                if process.returncode != 0
                else None
            ),
        }

    except Exception as exc:
        result["lake"] = {
            "ok": False,
            "version": None,
            "error": repr(exc),
        }

    # --------------------------------------------------------
    # Mathlib
    # --------------------------------------------------------

    mathlib_olean = (
        LEAN_PROJECT
        / ".lake"
        / "packages"
        / "mathlib"
        / ".lake"
        / "build"
        / "lib"
        / "lean"
        / "Mathlib.olean"
    )

    result["mathlib"] = {
        "exists": mathlib_olean.exists(),
        "size_mb": round(
            mathlib_olean.stat().st_size / 1024 / 1024,
            2,
        )
        if mathlib_olean.exists()
        else 0,
    }

    # --------------------------------------------------------
    # Persistent server
    # --------------------------------------------------------

    try:
        lean_server.start()

        result["server"] = {
            "created": True,
            "alive": lean_server.alive(),
            "pid": (
                lean_server.process.pid
                if lean_server.process
                else None
            ),
            "reader_error": lean_server.reader_error,
        }

    except Exception as exc:
        result["server"] = {
            "created": False,
            "alive": False,
            "pid": None,
            "reader_error": repr(exc),
        }

        result["status"] = "degraded"

    return result


# ============================================================
# /benchmark
# ============================================================

@app.get("/benchmark")
def benchmark():
    tests = {
        "bare": """
theorem test : 1 + 1 = 2 := rfl
""",

        "real_basic": """
import Mathlib.Data.Real.Basic

theorem test : (1 : ℝ) + 1 = 2 := by
  norm_num
""",

        "mathlib": """
import Mathlib

theorem test : (1 : ℝ) + 1 = 2 := by
  norm_num
""",

        "invalid_bare": """
theorem test : 8 + 1 = 2 := rfl
""",

        "invalid_real": """
import Mathlib.Data.Real.Basic

theorem test : (1 : ℝ) + 3 = 0 := by
  norm_num
""",
    }

    results = {}

    for name, source in tests.items():
        start = time.perf_counter()

        try:
            result = lean_server.check(
                source,
                timeout=120,
            )

        except Exception as exc:
            lean_server.stop()

            result = {
                "success": False,
                "exit_code": None,
                "elapsed_seconds": round(
                    time.perf_counter() - start,
                    3,
                ),
                "error": repr(exc),
                "diagnostics": "",
            }

        results[name] = result

    return results