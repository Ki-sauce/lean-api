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

lean_server = None
lean_server_lock = threading.Lock()


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
        self.response_condition = threading.Condition()

        self.write_lock = threading.Lock()

        self.diagnostics = {}

        self.reader_error = None

        self._start()

    def _start(self):
        print("[lean] starting server", flush=True)

        self.process = subprocess.Popen(
            ["lean", "--server"],
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

        threading.Thread(
            target=self._read_loop,
            daemon=True,
        ).start()

        threading.Thread(
            target=self._stderr_loop,
            daemon=True,
        ).start()

        print(
            "[lean] sending initialize",
            flush=True,
        )

        result = self._request(
            1,
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {
                    "name": "lean-api",
                    "version": "1.0",
                },
                "rootUri": Path(
                    LEAN_PROJECT
                ).resolve().as_uri(),
                "capabilities": {},
            },
            timeout=30,
        )

        print(
            "[lean] initialize response:",
            json.dumps(result),
            flush=True,
        )

        self._notify(
            "initialized",
            {},
        )

        print(
            "[lean] initialized",
            flush=True,
        )

    def _send(self, message):
        body = json.dumps(
            message,
            separators=(",", ":"),
        ).encode("utf-8")

        header = (
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode("ascii")

        print(
            "[LSP ->]",
            json.dumps(message),
            flush=True,
        )

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

    def _request(
        self,
        request_id,
        method,
        params,
        timeout=30,
    ):
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        self._send(message)

        deadline = time.monotonic() + timeout

        with self.response_condition:

            while request_id not in self.responses:

                remaining = (
                    deadline - time.monotonic()
                )

                if remaining <= 0:
                    raise TimeoutError(
                        f"timeout waiting for {method}"
                    )

                self.response_condition.wait(
                    remaining
                )

            return self.responses.pop(
                request_id
            )

    def _read_message(self):
        headers = {}

        while True:
            line = self.process.stdout.readline()

            if not line:
                return None

            line = line.decode(
                "ascii",
                errors="replace",
            ).rstrip("\r\n")

            if not line:
                break

            key, value = line.split(
                ":",
                1,
            )

            headers[key.lower()] = value.strip()

        if "content-length" not in headers:
            raise RuntimeError(
                f"missing Content-Length: {headers}"
            )

        length = int(
            headers["content-length"]
        )

        body = self.process.stdout.read(
            length
        )

        if not body:
            return None

        return json.loads(
            body.decode("utf-8")
        )

    def _read_loop(self):
        try:
            while True:

                message = self._read_message()

                if message is None:
                    print(
                        "[LSP <-] EOF",
                        flush=True,
                    )
                    return

                print(
                    "[LSP <-]",
                    json.dumps(message),
                    flush=True,
                )

                # Response to one of our requests.
                if (
                    "id" in message
                    and (
                        "result" in message
                        or "error" in message
                    )
                ):

                    with self.response_condition:
                        self.responses[
                            message["id"]
                        ] = message

                        self.response_condition.notify_all()

                    continue

                # Server -> client request.
                if (
                    "id" in message
                    and "method" in message
                ):

                    request_id = message["id"]
                    method = message["method"]

                    print(
                        "[LSP] server request:",
                        method,
                        flush=True,
                    )

                    # For now, acknowledge it.
                    self._send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": None,
                    })

                    continue

                # Diagnostics notification.
                if (
                    message.get("method")
                    == "textDocument/publishDiagnostics"
                ):

                    params = message.get(
                        "params",
                        {},
                    )

                    uri = params.get(
                        "uri"
                    )

                    self.diagnostics[uri] = (
                        params.get(
                            "diagnostics",
                            [],
                        )
                    )

                    print(
                        "[LSP] diagnostics:",
                        len(
                            self.diagnostics[uri]
                        ),
                        flush=True,
                    )

                    continue

        except Exception as e:

            self.reader_error = repr(e)

            print(
                "[LSP reader error]",
                repr(e),
                flush=True,
            )

    def _stderr_loop(self):
        while True:

            line = self.process.stderr.readline()

            if not line:
                return

            print(
                "[lean stderr]",
                line.decode(
                    errors="replace"
                ).rstrip(),
                flush=True,
            )

    def debug_hover(self):
        source = """theorem test : 1 + 1 = 2 := by
  norm_num
"""

        filename = os.path.join(
            LEAN_PROJECT,
            "_debug.lean",
        )

        uri = Path(
            filename
        ).resolve().as_uri()

        with open(filename, "w") as f:
            f.write(source)

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

        # Ask for hover over "test".
        request_id = 100

        result = self._request(
            request_id,
            "textDocument/hover",
            {
                "textDocument": {
                    "uri": uri,
                },
                "position": {
                    "line": 0,
                    "character": 9,
                },
            },
            timeout=30,
        )

        return result


def get_lean_server():
    global lean_server

    if lean_server is not None:
        return lean_server

    with lean_server_lock:

        if lean_server is None:
            lean_server = LeanServer()

    return lean_server


@app.get("/health")
def health():

    mathlib = Path(
        f"{LEAN_PROJECT}/.lake/packages/mathlib/"
        ".lake/build/lib/lean/Mathlib.olean"
    )

    return {
        "status": "ok",

        "lean": {
            "ok": subprocess.run(
                ["lean", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            ).returncode == 0,
        },

        "lake": {
            "ok": subprocess.run(
                ["lake", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            ).returncode == 0,
        },

        "mathlib": {
            "exists": mathlib.exists(),
            "size_mb": (
                round(
                    mathlib.stat().st_size
                    / 1024
                    / 1024,
                    2,
                )
                if mathlib.exists()
                else None
            ),
        },

        "server": {
            "created": lean_server is not None,

            "alive": (
                lean_server is not None
                and lean_server.process is not None
                and lean_server.process.poll() is None
            ),

            "pid": (
                lean_server.process.pid
                if (
                    lean_server is not None
                    and lean_server.process is not None
                )
                else None
            ),

            "reader_error": (
                lean_server.reader_error
                if lean_server is not None
                else None
            ),
        },
    }


@app.get("/debug-lsp")
def debug_lsp():

    server = get_lean_server()

    start = time.monotonic()

    try:

        result = server.debug_hover()

        return {
            "ok": True,
            "elapsed_seconds": (
                time.monotonic() - start
            ),
            "result": result,
        }

    except Exception as e:

        return {
            "ok": False,
            "elapsed_seconds": (
                time.monotonic() - start
            ),
            "error": repr(e),
            "reader_error": server.reader_error,
            "server_alive": (
                server.process is not None
                and server.process.poll() is None
            ),
        }


@app.post("/check")
def check(request: CheckRequest):

    server = get_lean_server()

    try:

        return server.debug_hover()

    except Exception as e:

        return {
            "success": False,
            "error": repr(e),
            "reader_error": server.reader_error,
            "server_alive": (
                server.process is not None
                and server.process.poll() is None
            ),
        }