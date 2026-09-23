import glob
import json
import os
import subprocess
import threading
import time
import urllib.parse

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
        self.lock = threading.Lock()
        self.responses = {}
        self.diagnostics = {}

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
                "rootUri": self._uri(LEAN_PROJECT),
                "capabilities": {},
            },
            timeout=120,
        )

        self._notify("initialized", {})

    def _uri(self, path):
        return "file://" + urllib.parse.quote(
            os.path.abspath(path)
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

        self.process.stdin.write(header + body)
        self.process.stdin.flush()

    def _notify(self, method, params):
        self._send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })

    def _request(self, request_id, method, params, timeout=120):
        with self.lock:
            self._send({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            })

            deadline = time.time() + timeout

            while time.time() < deadline:
                if request_id in self.responses:
                    return self.responses.pop(request_id)

                time.sleep(0.01)

        raise TimeoutError(
            f"Lean server request timed out: {method}"
        )

    def _read_loop(self):
        while True:
            try:
                headers = {}

                while True:
                    line = self.process.stdout.readline()

                    if not line:
                        return

                    line = line.decode("ascii").strip()

                    if not line:
                        break

                    key, value = line.split(":", 1)
                    headers[key.lower()] = value.strip()

                length = int(headers["content-length"])
                body = self.process.stdout.read(length)

                if not body:
                    return

                message = json.loads(body.decode("utf-8"))

                if "id" in message and (
                    "result" in message or "error" in message
                ):
                    self.responses[message["id"]] = message

                elif message.get("method") == "textDocument/publishDiagnostics":
                    params = message.get("params", {})
                    uri = params.get("uri")

                    self.diagnostics[uri] = params.get(
                        "diagnostics",
                        [],
                    )

            except Exception:
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

    def check(self, source):
        filename = os.path.join(
            LEAN_PROJECT,
            f"_check_{time.time_ns()}.lean",
        )

        uri = self._uri(filename)

        try:
            with open(filename, "w") as f:
                f.write(source)

            version = int(time.time_ns() % 2_000_000_000)

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

            request_id = int(time.time_ns() % 2_000_000_000)

            result = self._request(
                request_id,
                "textDocument/waitForDiagnostics",
                {
                    "uri": uri,
                    "version": version,
                },
                timeout=120,
            )

            diagnostics = self.diagnostics.get(uri, [])

            errors = [
                d for d in diagnostics
                if d.get("severity") == 1
            ]

            warnings = [
                d for d in diagnostics
                if d.get("severity") != 1
            ]

            return {
                "success": len(errors) == 0,
                "diagnostics": diagnostics,
                "errors": errors,
                "warnings": warnings,
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