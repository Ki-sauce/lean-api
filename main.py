import os
import subprocess
import tempfile

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class CheckRequest(BaseModel):
    source: str


@app.post("/check")
def check(req: CheckRequest):
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".lean",
        delete=False,
    ) as f:
        f.write(req.source)
        path = f.name

    try:
        result = subprocess.run(
            ["lean", path],
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
            "error": "Lean timed out",
        }

    finally:
        os.unlink(path)