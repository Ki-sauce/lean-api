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
                "capabilities": {
                    "textDocument": {
                        "publishDiagnostics": {
                            "relatedInformation": True
                        }
                    }
                },
            },
            timeout=120,
        )

        self._notify("initialized", {})

    # ------------------------------------------------------------
    # LSP transport
    # ------------------------------------------------------------

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

    def _request(self, request_id, method, params, timeout=120):
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

                self.response_condition.wait(
                    timeout=remaining
                )

            return self.responses.pop(request_id)

    # ------------------------------------------------------------
    # LSP reader
    # ------------------------------------------------------------

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

        length = int(headers["content-length"])

        body = self.process.stdout.read(length)

        if not body:
            return None

        return json.loads(body.decode("utf-8"))

    def _read_loop(self):
        while True:
            try:
                message = self._read_message()

                if message is None:
                    return

                # Response to one of our requests.
                if "id" in message and (
                    "result" in message
                    or "error" in message
                ):
                    with self.response_condition:
                        self.responses[message["id"]] = message
                        self.response_condition.notify_all()

                    continue

                # Request originating from Lean.
                #
                # Lean can ask the client to register capabilities,
                # refresh things, etc. We don't need those capabilities,
                # but we must answer the JSON-RPC request.
                if (
                    "id" in message
                    and "method" in message
                ):
                    self._send({
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": None,
                    })

                    continue

                # Diagnostics notification.
                if (
                    message.get("method")
                    == "textDocument/publishDiagnostics"
                ):
                    params = message.get("params", {})
                    uri = params.get("uri")

                    diagnostics = params.get(
                        "diagnostics",
                        [],
                    )

                    self.diagnostics[uri] = diagnostics

                    continue

            except Exception as e:
                print(
                    "[lean-reader]",
                    repr(e),
                    flush=True,
                )
                return

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

    # ------------------------------------------------------------
    # Lean checking
    # ------------------------------------------------------------

    def check(self, source):
        filename = os.path.join(
            LEAN_PROJECT,
            f"_check_{time.time_ns()}.lean",
        )

        uri = Path(filename).resolve().as_uri()

        version = 1

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
                        "version": version,
                        "text": source,
                    }
                },
            )

            request_id = time.time_ns()

            self._request(
                request_id,
                "textDocument/waitForDiagnostics",
                {
                    "uri": uri,
                    "version": version,
                },
                timeout=120,
            )

            diagnostics = self.diagnostics.get(
                uri,
                [],
            )

            errors = [
                d
                for d in diagnostics
                if d.get("severity") == 1
            ]

            warnings = [
                d
                for d in diagnostics
                if d.get("severity") != 1
            ]

            return {
                "success": len(errors) == 0,
                "diagnostics": diagnostics,
                "errors": errors,
                "warnings": warnings,
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
    lean_server = LeanServer()


@app.get("/health")
def health():
    result = {}

    try:
        lean = subprocess.run(
            ["lean", "--version"],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )

        result["lean"] = {
            "returncode": lean.returncode,
            "stdout": lean.stdout,
            "stderr": lean.stderr,
        }

    except Exception as e:
        result["lean"] = {
            "error": str(e),
        }

    result["server_alive"] = (
        lean_server is not None
        and lean_server.process.poll() is None
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
        return lean_server.check(req.source)

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