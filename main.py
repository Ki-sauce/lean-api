import json
import os
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel


# ============================================================================
# Configuration
# ============================================================================

LEAN_PROJECT = Path("/app/leanverify")
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

    existing = env.get("LEAN_PATH")

    if existing:
        paths.append(existing)

    env["LEAN_PATH"] = os.pathsep.join(paths)

    return env


# ============================================================================
# LSP framing
# ============================================================================

def encode_message(message: dict[str, Any]) -> bytes:
    body = json.dumps(
        message,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    header = (
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode("ascii")

    return header + body


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
            content_length = int(
                line.split(b":", 1)[1].strip()
            )

    if content_length is None:
        raise RuntimeError(
            "LSP message missing Content-Length"
        )

    body = stream.read(content_length)

    if len(body) != content_length:
        raise RuntimeError(
            f"Short LSP message: expected "
            f"{content_length} bytes, got {len(body)}"
        )

    return json.loads(
        body.decode("utf-8")
    )


# ============================================================================
# Persistent Lean server
# ============================================================================

class LeanServer:

    def __init__(self):
        self.process: subprocess.Popen | None = None

        self.write_lock = threading.Lock()
        self.check_lock = threading.Lock()

        self.response_lock = threading.Lock()
        self.responses: dict[int, dict[str, Any]] = {}

        self.diagnostics_lock = threading.Lock()
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}
        self.diagnostic_events: dict[str, threading.Event] = {}

        self.next_id = 1

        self.reader_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None

        self.initialized = False
        self.reader_error: str | None = None

    def alive(self) -> bool:
        return (
            self.process is not None
            and self.process.poll() is None
        )

    def start(self) -> None:
        if self.alive() and self.initialized:
            return

        self.stop()

        self.reader_error = None
        self.initialized = False

        print(
            "[lean] starting server",
            flush=True,
        )

        process = subprocess.Popen(
            [
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

        self.process = process

        print(
            f"[lean] pid={process.pid}",
            flush=True,
        )

        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            name="lean-lsp-reader",
            daemon=True,
        )

        self.reader_thread.start()

        self.stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name="lean-stderr-reader",
            daemon=True,
        )

        self.stderr_thread.start()

        try:
            self._initialize()

        except Exception:
            print(
                "[lean] initialization failed",
                flush=True,
            )

            self.stop()

            raise

    def stop(self) -> None:
        process = self.process

        if process is None:
            self.initialized = False
            return

        print(
            f"[lean] stopping pid={process.pid}",
            flush=True,
        )

        if process.poll() is None:

            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:
                pass

            try:
                process.terminate()
                process.wait(timeout=3)

            except Exception:

                try:
                    process.kill()
                except Exception:
                    pass

        self.process = None
        self.initialized = False

    def send(self, message: dict[str, Any]) -> None:
        process = self.process

        if (
            process is None
            or process.poll() is not None
            or process.stdin is None
        ):
            raise RuntimeError(
                "Lean server is not running"
            )

        data = encode_message(message)

        with self.write_lock:
            process.stdin.write(data)
            process.stdin.flush()

    def notify(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:

        print(
            f"[LSP -> notification] {method}",
            flush=True,
        )

        self.send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 120.0,
    ) -> dict[str, Any]:

        request_id = self.next_id
        self.next_id += 1

        event = threading.Event()

        with self.response_lock:
            self.responses[request_id] = {
                "event": event,
                "response": None,
            }

        print(
            f"[LSP -> request] "
            f"id={request_id} method={method}",
            flush=True,
        )

        try:
            self.send({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            })

            if not event.wait(timeout):
                raise TimeoutError(
                    f"timeout waiting for {method}"
                )

            with self.response_lock:
                entry = self.responses.get(request_id)

                if entry is None:
                    raise RuntimeError(
                        f"response for {method} disappeared"
                    )

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

            print(
                f"[LSP <- response] "
                f"id={request_id} method={method}",
                flush=True,
            )

            return response

        finally:
            with self.response_lock:
                self.responses.pop(
                    request_id,
                    None,
                )

    def _initialize(self) -> None:

        root_uri = (
            LEAN_PROJECT
            .resolve()
            .as_uri()
        )

        print(
            "[lean] sending initialize",
            flush=True,
        )

        response = self.request(
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

            timeout=120,
        )

        print(
            "[lean] initialize response received",
            flush=True,
        )

        print(
            json.dumps(
                response.get(
                    "result",
                    {},
                ),
                separators=(",", ":"),
            )[:1000],
            flush=True,
        )

        self.notify(
            "initialized",
            {},
        )

        self.initialized = True

        print(
            "[lean] initialized",
            flush=True,
        )

    def _reader_loop(self) -> None:

        process = self.process

        if (
            process is None
            or process.stdout is None
        ):
            return

        try:

            while True:

                message = read_message(
                    process.stdout
                )

                if message is None:
                    print(
                        "[lean] stdout closed",
                        flush=True,
                    )
                    break

                print(
                    "[LSP <-] "
                    + json.dumps(
                        message,
                        separators=(",", ":"),
                    )[:2000],
                    flush=True,
                )

                self._handle_message(
                    message
                )

        except Exception as exc:

            self.reader_error = repr(exc)

            print(
                f"[lean] reader error: "
                f"{self.reader_error}",
                flush=True,
            )

    def _stderr_loop(self) -> None:

        process = self.process

        if (
            process is None
            or process.stderr is None
        ):
            return

        try:

            for raw_line in process.stderr:

                line = raw_line.decode(
                    "utf-8",
                    errors="replace",
                ).rstrip()

                if line:
                    print(
                        f"[lean stderr] {line}",
                        flush=True,
                    )

        except Exception as exc:

            print(
                f"[lean] stderr reader error: "
                f"{exc}",
                flush=True,
            )

    def _handle_message(
        self,
        message: dict[str, Any],
    ) -> None:

        # Response to a request we sent.
        if (
            "id" in message
            and (
                "result" in message
                or "error" in message
            )
        ):

            request_id = message["id"]

            with self.response_lock:

                entry = self.responses.get(
                    request_id
                )

                if entry is not None:

                    entry["response"] = message
                    entry["event"].set()

            return

        # Request originating from Lean.
        if (
            "id" in message
            and "method" in message
        ):

            request_id = message["id"]
            method = message["method"]

            print(
                f"[Lean -> client] "
                f"id={request_id} "
                f"method={method}",
                flush=True,
            )

            self.send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": None,
            })

            return

        # Notifications.
        method = message.get("method")

        if method == "textDocument/publishDiagnostics":

            params = message.get(
                "params",
                {},
            )

            uri = params.get("uri")

            diagnostics = params.get(
                "diagnostics",
                [],
            )

            if uri:

                with self.diagnostics_lock:

                    self.diagnostics[uri] = (
                        diagnostics
                    )

                    event = (
                        self.diagnostic_events.get(
                            uri
                        )
                    )

                    if event is not None:
                        event.set()

                print(
                    "[lean] diagnostics "
                    f"count={len(diagnostics)}",
                    flush=True,
                )

            return

        if method == "$/lean/fileProgress":
            return


# ============================================================================
# Global Lean server
# ============================================================================

lean_server = LeanServer()


# ============================================================================
# FastAPI lifespan
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

    print(
        "[app] shutting down Lean server",
        flush=True,
    )

    lean_server.stop()


app = FastAPI(
    lifespan=lifespan,
)


# ============================================================================
# API models
# ============================================================================

class CheckRequest(BaseModel):
    source: str


# ============================================================================
# Verification
# ============================================================================

def has_errors(
    diagnostics: list[dict[str, Any]],
) -> bool:

    return any(
        diagnostic.get("severity", 1) == 1
        for diagnostic in diagnostics
    )


def check_source(
    source: str,
    timeout: float = CHECK_TIMEOUT,
) -> dict[str, Any]:

    with lean_server.check_lock:

        if (
            not lean_server.alive()
            or not lean_server.initialized
        ):
            lean_server.start()

        request_id = uuid.uuid4().hex

        filename = (
            f"_verify_{request_id}.lean"
        )

        path = LEAN_PROJECT / filename
        uri = path.resolve().as_uri()

        diagnostic_event = threading.Event()

        with lean_server.diagnostics_lock:
            lean_server.diagnostics.pop(
                uri,
                None,
            )

            lean_server.diagnostic_events[
                uri
            ] = diagnostic_event

        started = time.monotonic()

        try:

            path.write_text(
                source,
                encoding="utf-8",
            )

            print(
                f"[check] didOpen {filename}",
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

            if not diagnostic_event.wait(timeout):
                raise TimeoutError(
                    "timeout waiting for "
                    "textDocument/publishDiagnostics"
                )

            with lean_server.diagnostics_lock:
                diagnostics = list(
                    lean_server.diagnostics.get(
                        uri,
                        [],
                    )
                )

            elapsed = (
                time.monotonic()
                - started
            )

            success = not has_errors(
                diagnostics
            )

            return {
                "success": success,
                "elapsed_seconds": round(
                    elapsed,
                    3,
                ),
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

                lean_server.diagnostics.pop(
                    uri,
                    None,
                )

                lean_server.diagnostic_events.pop(
                    uri,
                    None,
                )

            try:
                path.unlink()

            except FileNotFoundError:
                pass

            except Exception as exc:

                print(
                    f"[check] failed to delete "
                    f"{path}: {exc}",
                    flush=True,
                )


# ============================================================================
# /check
# ============================================================================

@app.post("/check")
def check(request: CheckRequest):

    try:

        return check_source(
            request.source
        )

    except Exception as exc:

        return {
            "success": False,
            "error": repr(exc),
            "server_alive": (
                lean_server.alive()
            ),
            "server_initialized": (
                lean_server.initialized
            ),
            "reader_error": (
                lean_server.reader_error
            ),
        }


# ============================================================================
# /health
# ============================================================================

@app.get("/health")
def health():

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

    lean_version = None
    lean_error = None

    try:

        proc = subprocess.run(
            ["lean", "--version"],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )

        lean_version = proc.stdout.strip()

        if proc.returncode != 0:
            lean_error = proc.stderr.strip()

    except Exception as exc:
        lean_error = repr(exc)

    return {
        "status": "ok",

        "lean": {
            "ok": lean_error is None,
            "version": lean_version,
            "error": lean_error,
        },

        "lake": {
            "ok": True,
        },

        "mathlib": {
            "exists": mathlib_olean.exists(),
            "size_mb": (
                round(
                    mathlib_olean.stat().st_size
                    / 1024
                    / 1024,
                    2,
                )
                if mathlib_olean.exists()
                else 0
            ),
        },

        "server": {
            "created": (
                lean_server.process is not None
            ),
            "alive": lean_server.alive(),
            "initialized": (
                lean_server.initialized
            ),
            "pid": (
                lean_server.process.pid
                if lean_server.process
                else None
            ),
            "reader_error": (
                lean_server.reader_error
            ),
        },
    }