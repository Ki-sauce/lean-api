import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LEAN_PROJECT = Path("/app/leanverify")
LEAN_TOOLCHAIN = LEAN_PROJECT / "lean-toolchain"

CHECK_TIMEOUT = 90.0


def lean_env() -> dict[str, str]:
    env = os.environ.copy()

    paths = [
        str(LEAN_PROJECT / ".lake" / "build" / "lib" / "lean"),
    ]

    packages = LEAN_PROJECT / ".lake" / "packages"
    if packages.exists():
        for package in packages.iterdir():
            lib = package / ".lake" / "build" / "lib" / "lean"
            if lib.exists():
                paths.append(str(lib))

    old = env.get("LEAN_PATH")
    if old:
        paths.append(old)

    env["LEAN_PATH"] = os.pathsep.join(paths)
    return env


# ---------------------------------------------------------------------------
# LSP transport
# ---------------------------------------------------------------------------

def encode_message(message: dict[str, Any]) -> bytes:
    body = json.dumps(
        message,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    return (
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode("ascii") + body


def read_message(stream) -> dict[str, Any] | None:
    content_length = None

    while True:
        line = stream.readline()

        if not line:
            return None

        line = line.strip()

        if not line:
            break

        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())

    if content_length is None:
        raise RuntimeError("LSP message has no Content-Length")

    body = stream.read(content_length)

    if len(body) != content_length:
        raise RuntimeError(
            f"Short LSP message: expected {content_length}, got {len(body)}"
        )

    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# Persistent Lean server
# ---------------------------------------------------------------------------

class LeanServer:
    def __init__(self):
        self.process: subprocess.Popen | None = None

        self.write_lock = threading.Lock()
        self.check_lock = threading.Lock()

        self.response_lock = threading.Lock()
        self.responses: dict[Any, dict[str, Any]] = {}

        self.diagnostics_lock = threading.Lock()
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}
        self.diagnostic_events: dict[str, threading.Event] = {}

        self.reader_thread: threading.Thread | None = None

        self.next_id = 1

        self.started = False
        self.initialized = False

        self.reader_error: str | None = None

    # ------------------------------------------------------------------
    # Process
    # ------------------------------------------------------------------

    def alive(self) -> bool:
        return (
            self.process is not None
            and self.process.poll() is None
        )

    def start(self):
        if self.alive() and self.initialized:
            return

        self.stop()

        self.reader_error = None

        print("[lean] starting server", flush=True)

        self.process = subprocess.Popen(
            [
                "lake",
                "env",
                "lean",
                "--server",
            ],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        print(
            f"[lean] pid={self.process.pid}",
            flush=True,
        )

        self.started = True

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

    def stop(self):
        process = self.process

        if process is None:
            return

        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        self.process = None
        self.started = False
        self.initialized = False

    # ------------------------------------------------------------------
    # LSP sending
    # ------------------------------------------------------------------

    def send(self, message: dict[str, Any]):
        if not self.process or not self.process.stdin:
            raise RuntimeError("Lean server is not running")

        data = encode_message(message)

        with self.write_lock:
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:

        request_id = self.next_id
        self.next_id += 1

        event = threading.Event()

        with self.response_lock:
            self.responses[request_id] = {
                "event": event,
                "response": None,
            }

        self.send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })

        if not event.wait(timeout):
            with self.response_lock:
                self.responses.pop(request_id, None)

            raise TimeoutError(
                f"timeout waiting for {method}"
            )

        with self.response_lock:
            entry = self.responses.pop(request_id)

        response = entry["response"]

        if response is None:
            raise RuntimeError(
                f"Lean returned no response for {method}"
            )

        if "error" in response:
            raise RuntimeError(
                f"Lean LSP error for {method}: "
                f"{response['error']}"
            )

        return response

    def notify(
        self,
        method: str,
        params: dict[str, Any],
    ):
        self.send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _initialize(self):
        root_uri = LEAN_PROJECT.resolve().as_uri()

        print("[lean] sending initialize", flush=True)

        self.request(
            "initialize",
            {
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
                        "workspaceFolders": True,
                    },
                    "textDocument": {
                        "publishDiagnostics": {},
                    },
                },
                "initializationOptions": {},
            },
            timeout=30,
        )

        self.notify(
            "initialized",
            {},
        )

        self.initialized = True

        print("[lean] initialized", flush=True)

    # ------------------------------------------------------------------
    # Reader
    # ------------------------------------------------------------------

    def _reader_loop(self):
        assert self.process is not None
        assert self.process.stdout is not None

        try:
            while True:
                message = read_message(self.process.stdout)

                if message is None:
                    break

                self._handle_message(message)

        except Exception as exc:
            self.reader_error = repr(exc)
            print(
                f"[lean] reader error: {self.reader_error}",
                flush=True,
            )

    def _stderr_loop(self):
        if not self.process or not self.process.stderr:
            return

        try:
            for raw in self.process.stderr:
                line = raw.decode(
                    "utf-8",
                    errors="replace",
                ).rstrip()

                if line:
                    print(
                        f"[lean stderr] {line}",
                        flush=True,
                    )

        except Exception:
            pass

    def _handle_message(
        self,
        message: dict[str, Any],
    ):
        # --------------------------------------------------------------
        # Response to one of our requests
        # --------------------------------------------------------------

        if "id" in message and (
            "result" in message or "error" in message
        ):
            request_id = message["id"]

            with self.response_lock:
                entry = self.responses.get(request_id)

                if entry is not None:
                    entry["response"] = message
                    entry["event"].set()

            return

        # --------------------------------------------------------------
        # Server -> client request
        #
        # Lean sends things like:
        #
        # client/registerCapability
        # workspace/inlayHint/refresh
        # workspace/semanticTokens/refresh
        #
        # These are requests from Lean to us, so we MUST respond.
        # --------------------------------------------------------------

        if "id" in message and "method" in message:
            request_id = message["id"]
            method = message["method"]

            print(
                f"[LSP <- request] {method}",
                flush=True,
            )

            self.send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": None,
            })

            return

        # --------------------------------------------------------------
        # Notifications
        # --------------------------------------------------------------

        method = message.get("method")

        if method == "textDocument/publishDiagnostics":
            params = message.get("params", {})

            uri = params.get("uri")
            diagnostics = params.get("diagnostics", [])

            if uri:
                with self.diagnostics_lock:
                    self.diagnostics[uri] = diagnostics

                    event = self.diagnostic_events.get(uri)

                    if event:
                        event.set()

            print(
                f"[lean] diagnostics uri={uri} "
                f"count={len(diagnostics)}",
                flush=True,
            )

            return

        if method == "$/lean/fileProgress":
            # We deliberately don't use this as the completion condition.
            #
            # publishDiagnostics is the authoritative result for a
            # document verification request.
            return

        # Other notifications can safely be ignored.


# One Lean process for this FastAPI process.
lean_server = LeanServer()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

class CheckRequest(BaseModel):
    source: str


def diagnostics_are_errors(
    diagnostics: list[dict[str, Any]],
) -> bool:
    """
    LSP Diagnostic.severity:
      1 = Error
      2 = Warning
      3 = Information
      4 = Hint
    """

    return any(
        diagnostic.get("severity", 1) == 1
        for diagnostic in diagnostics
    )


def check_source(
    source: str,
    timeout: float = CHECK_TIMEOUT,
) -> dict[str, Any]:

    # A single Lean server currently owns one mutable document environment.
    # Serialize verification until we deliberately implement a worker pool.
    with lean_server.check_lock:

        if not lean_server.alive() or not lean_server.initialized:
            lean_server.start()

        # Every request gets a completely different URI.
        # This prevents stale diagnostics from a previous request being
        # associated with the current request.
        request_id = uuid.uuid4().hex

        filename = f"_verify_{request_id}.lean"
        path = LEAN_PROJECT / filename
        uri = path.resolve().as_uri()

        diagnostic_event = threading.Event()

        with lean_server.diagnostics_lock:
            lean_server.diagnostics.pop(uri, None)
            lean_server.diagnostic_events[uri] = diagnostic_event

        started = time.monotonic()

        try:
            path.write_text(
                source,
                encoding="utf-8",
            )

            print(
                f"[check] opening {filename}",
                flush=True,
            )

            lean_server.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": "lean",
                        "version": 1,
                        "text": source,
                    }
                },
            )

            # ----------------------------------------------------------
            # Wait ONLY for publishDiagnostics.
            #
            # We do not issue hover.
            # We do not issue arbitrary requests to determine whether
            # Lean has finished.
            # ----------------------------------------------------------

            if not diagnostic_event.wait(timeout):
                raise TimeoutError(
                    "timeout waiting for textDocument/publishDiagnostics"
                )

            with lean_server.diagnostics_lock:
                diagnostics = list(
                    lean_server.diagnostics.get(uri, [])
                )

            elapsed = time.monotonic() - started

            success = not diagnostics_are_errors(
                diagnostics
            )

            return {
                "success": success,
                "elapsed_seconds": round(elapsed, 3),
                "diagnostics": diagnostics,
            }

        finally:
            try:
                if lean_server.alive():
                    lean_server.notify(
                        "textDocument/didClose",
                        {
                            "textDocument": {
                                "uri": uri,
                            }
                        },
                    )
            except Exception:
                pass

            with lean_server.diagnostics_lock:
                lean_server.diagnostics.pop(uri, None)
                lean_server.diagnostic_events.pop(uri, None)

            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except Exception as exc:
                print(
                    f"[check] failed to delete {path}: {exc}",
                    flush=True,
                )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.post("/check")
def check(request: CheckRequest):
    try:
        return check_source(request.source)

    except Exception as exc:
        return {
            "success": False,
            "error": repr(exc),
            "server_alive": lean_server.alive(),
            "reader_error": lean_server.reader_error,
        }


@app.get("/health")
def health():
    result: dict[str, Any] = {
        "status": "ok",
        "lean": {
            "ok": False,
        },
        "lake": {
            "ok": False,
        },
        "mathlib": {
            "exists": False,
        },
        "server": {
            "created": lean_server.process is not None,
            "alive": lean_server.alive(),
            "pid": (
                lean_server.process.pid
                if lean_server.process
                else None
            ),
            "initialized": lean_server.initialized,
            "reader_error": lean_server.reader_error,
        },
    }

    # --------------------------------------------------------------
    # Lean executable
    # --------------------------------------------------------------

    try:
        proc = subprocess.run(
            ["lean", "--version"],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )

        result["lean"] = {
            "ok": proc.returncode == 0,
            "version": proc.stdout.strip(),
            "error": proc.stderr.strip() or None,
        }

    except Exception as exc:
        result["lean"] = {
            "ok": False,
            "error": repr(exc),
        }

    # --------------------------------------------------------------
    # Lake
    # --------------------------------------------------------------

    try:
        proc = subprocess.run(
            ["lake", "--version"],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )

        result["lake"] = {
            "ok": proc.returncode == 0,
            "version": proc.stdout.strip(),
            "error": proc.stderr.strip() or None,
        }

    except Exception as exc:
        result["lake"] = {
            "ok": False,
            "error": repr(exc),
        }

    # --------------------------------------------------------------
    # Mathlib
    # --------------------------------------------------------------

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
        "size_mb": (
            round(mathlib_olean.stat().st_size / 1024 / 1024, 2)
            if mathlib_olean.exists()
            else 0
        ),
    }

    # --------------------------------------------------------------
    # Actual end-to-end Lean verification
    # --------------------------------------------------------------

    health_source = """\
import Mathlib

theorem leanverify_health : 1 + 1 = 2 := by
  norm_num
"""

    try:
        verification = check_source(
            health_source,
            timeout=30,
        )

        result["server"]["created"] = True
        result["server"]["alive"] = lean_server.alive()
        result["server"]["pid"] = (
            lean_server.process.pid
            if lean_server.process
            else None
        )
        result["server"]["initialized"] = lean_server.initialized

        result["verification"] = verification

        if not verification.get("success", False):
            result["status"] = "degraded"

    except Exception as exc:
        result["status"] = "degraded"

        result["verification"] = {
            "success": False,
            "error": repr(exc),
        }

    return result


@app.on_event("shutdown")
def shutdown():
    lean_server.stop()