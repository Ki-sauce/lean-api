import glob
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI()

LEAN_PROJECT = "/app/leanverify"


class CheckRequest(BaseModel):
    source: str


def lean_env():
    env = os.environ.copy()

    paths = [
        f"{LEAN_PROJECT}/.lake/build/lib/lean",
    ]

    paths += glob.glob(
        f"{LEAN_PROJECT}/.lake/packages/*/.lake/build/lib/lean"
    )

    existing = env.get("LEAN_PATH")
    if existing:
        paths.append(existing)

    env["LEAN_PATH"] = ":".join(paths)

    return env


class LeanServer:
    def __init__(self):
        self.process = None
        self.responses = {}
        self.diagnostics = {}

        self.response_condition = threading.Condition()
        self.write_lock = threading.Lock()

        self.reader_error = None

        self._start()

    def _start(self):
        self.process = subprocess.Popen(
            ["lean", "--server"],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        threading.Thread(
            target=self._read_loop,
            daemon=True,
        ).start()

        threading.Thread(
            target=self._stderr_loop,
            daemon=True,
        ).start()

        self._request(
            1,
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {
                    "name": "lean-api",
                    "version": "1.0",
                },
                "rootUri": Path(LEAN_PROJECT).resolve().as_uri(),
                "capabilities": {},
            },
            timeout=30,
        )

        self._notify("initialized", {})

    def _send(self, message):
        body = json.dumps(
            message,
            separators=(",", ":"),
        ).encode("utf-8")

        header = (
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
        ).encode("ascii")

        with self.write_lock:
            self.process.stdin.write(header)
            self.process.stdin.write(body)
            self.process.stdin.flush()

    def _notify(self, method, params):
        self._send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })

    def _request(self, request_id, method, params, timeout=30):
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })

        deadline = time.monotonic() + timeout

        with self.response_condition:
            while request_id not in self.responses:
                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    raise TimeoutError(
                        f"Lean server request timed out: {method}"
                    )

                self.response_condition.wait(remaining)

            return self.responses.pop(request_id)

    def _read_message(self):
        headers = {}

        while True:
            line = self.process.stdout.readline()

            if not line:
                return None

            line = line.decode("ascii").rstrip("\r\n")

            if not line:
                break

            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()

        if "content-length" not in headers:
            raise RuntimeError(
                f"Missing Content-Length: {headers}"
            )

        length = int(headers["content-length"])

        body = self.process.stdout.read(length)

        if not body:
            return None

        return json.loads(body.decode("utf-8"))

    def _read_loop(self):
        try:
            while True:
                message = self._read_message()

                if message is None:
                    return

                # Response to a request we sent.
                if "id" in message and (
                    "result" in message
                    or "error" in message
                ):
                    with self.response_condition:
                        self.responses[message["id"]] = message
                        self.response_condition.notify_all()

                    continue

                # Request originating from Lean.
                if (
                    "id" in message
                    and "method" in message
                ):
                    method = message["method"]

                    # We don't need any client-side capabilities
                    # for this verifier.
                    self._send({
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": None,
                    })

                    print(
                        f"[lean-server] handled request: {method}",
                        flush=True,
                    )

                    continue

                # Diagnostics notification.
                if (
                    message.get("method")
                    == "textDocument/publishDiagnostics"
                ):
                    params = message.get("params", {})
                    uri = params.get("uri")

                    self.diagnostics[uri] = params.get(
                        "diagnostics",
                        [],
                    )

                    continue

        except Exception as e:
            self.reader_error = repr(e)

            print(
                "[lean-reader-error]",
                repr(e),
                flush=True,
            )

    def _stderr_loop(self):
        while True:
            line = self.process.stderr.readline()

            if not line:
                return

            print(
                "[lean]",
                line.decode(errors="replace").rstrip(),
                flush=True,
            )

    def check(self, source, timeout=30):
        filename = os.path.join(
            LEAN_PROJECT,
            f"_health_{time.time_ns()}.lean",
        )

        uri = Path(filename).resolve().as_uri()

        try:
            with open(filename, "w") as f:
                f.write(source)

            self.diagnostics.pop(uri, None)

            self._notify(
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

            request_id = time.time_ns()

            started = time.monotonic()

            result = self._request(
                request_id,
                "textDocument/waitForDiagnostics",
                {
                    "uri": uri,
                    "version": 1,
                },
                timeout=timeout,
            )

            elapsed = time.monotonic() - started

            diagnostics = self.diagnostics.get(uri, [])

            errors = [
                d for d in diagnostics
                if d.get("severity") == 1
            ]

            return {
                "success": len(errors) == 0,
                "elapsed_seconds": round(elapsed, 3),
                "diagnostics": diagnostics,
                "server_response": result,
            }

        finally:
            try:
                self._notify(
                    "textDocument/didClose",
                    {
                        "textDocument": {
                            "uri": uri,
                        }
                    },
                )
            except Exception:
                pass

            try:
                os.remove(filename)
            except FileNotFoundError:
                pass


lean_server = None


@app.on_event("startup")
def startup():
    global lean_server

    try:
        lean_server = LeanServer()
    except Exception as e:
        print(
            "[startup] Lean server failed:",
            repr(e),
            flush=True,
        )
        lean_server = None


def run_health_check(name, fn):
    started = time.monotonic()

    try:
        result = fn()

        return {
            "ok": True,
            "elapsed_seconds": round(
                time.monotonic() - started,
                3,
            ),
            "result": result,
        }

    except Exception as e:
        return {
            "ok": False,
            "elapsed_seconds": round(
                time.monotonic() - started,
                3,
            ),
            "error": repr(e),
        }


@app.get("/health")
def health():
    result = {}

    # ------------------------------------------------------------
    # 1. Lean executable
    # ------------------------------------------------------------

    result["lean"] = run_health_check(
        "lean",
        lambda: subprocess.run(
            ["lean", "--version"],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip(),
    )

    # ------------------------------------------------------------
    # 2. Lake
    # ------------------------------------------------------------

    result["lake"] = run_health_check(
        "lake",
        lambda: subprocess.run(
            ["lake", "--version"],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip(),
    )

    # ------------------------------------------------------------
    # 3. Mathlib artifact
    # ------------------------------------------------------------

    mathlib_olean = (
        f"{LEAN_PROJECT}/.lake/packages/mathlib/"
        ".lake/build/lib/lean/Mathlib.olean"
    )

    result["mathlib"] = {
        "ok": os.path.exists(mathlib_olean),
        "path": mathlib_olean,
        "size_mb": (
            round(os.path.getsize(mathlib_olean) / 1024 / 1024, 2)
            if os.path.exists(mathlib_olean)
            else None
        ),
    }

    # ------------------------------------------------------------
    # 4. LEAN_PATH
    # ------------------------------------------------------------

    env = lean_env()

    result["lean_path"] = {
        "ok": bool(env.get("LEAN_PATH")),
        "entries": env.get("LEAN_PATH", "").split(":"),
    }

    # ------------------------------------------------------------
    # 5. Server process
    # ------------------------------------------------------------

    result["server"] = {
        "created": lean_server is not None,
        "alive": (
            lean_server is not None
            and lean_server.process.poll() is None
        ),
        "pid": (
            lean_server.process.pid
            if lean_server is not None
            else None
        ),
        "reader_error": (
            lean_server.reader_error
            if lean_server is not None
            else None
        ),
    }

    # ------------------------------------------------------------
    # 6. Actual server theorem test
    # ------------------------------------------------------------

    if lean_server is not None:
        result["server_trivial"] = run_health_check(
            "server_trivial",
            lambda: lean_server.check(
                "example : 1 + 1 = 2 := by rfl",
                timeout=30,
            ),
        )

        result["server_mathlib"] = run_health_check(
            "server_mathlib",
            lambda: lean_server.check(
                """import Mathlib.Data.Real.Basic

example : (1 : ℝ) + 1 = 2 := by
  norm_num
""",
                timeout=90,
            ),
        )

    return result


@app.post("/check")
def check(req: CheckRequest):
    if lean_server is None:
        return {
            "success": False,
            "error": "Lean server not initialized",
        }

    if lean_server.process.poll() is not None:
        return {
            "success": False,
            "error": "Lean server has exited",
        }

    try:
        return lean_server.check(
            req.source,
            timeout=120,
        )

    except TimeoutError as e:
        return {
            "success": False,
            "error": str(e),
        }

    except Exception as e:
        return {
            "success": False,
            "error": repr(e),
        }