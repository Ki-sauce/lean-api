import os
import subprocess
import tempfile

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

LEAN_PROJECT = "/app/leanverify"


class CheckRequest(BaseModel):
    source: str


@app.get("/health")
def health():
    try:
        result = subprocess.run(
            ["lean", "--version"],
            cwd=LEAN_PROJECT,
            capture_output=True,
            text=True,
            timeout=10,
        )

        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "lean --version timed out",
        }


@app.post("/check")
def check(req: CheckRequest):
    path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".lean",
            delete=False,
            dir=LEAN_PROJECT,
        ) as f:
            f.write(req.source)
            path = f.name

        result = subprocess.run(
            ["lake", "lean", path],
            cwd=LEAN_PROJECT,
            capture_output=True,
            text=True,
            timeout=60,
        )

        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Lean timed out",
        }

    finally:
        if path and os.path.exists(path):
            os.unlink(path)