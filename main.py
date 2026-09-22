import glob
import os
import subprocess
import tempfile

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

LEAN_PROJECT = "/app/leanverify"


class CheckRequest(BaseModel):
    source: str


def lean_env():
    env = os.environ.copy()

    paths = [
        f"{LEAN_PROJECT}/.lake/build/lib/lean"
    ]

    # Mathlib and its dependencies
    paths += glob.glob(
        f"{LEAN_PROJECT}/.lake/packages/*/.lake/build/lib/lean"
    )

    # Preserve anything already supplied by the environment.
    existing = env.get("LEAN_PATH")
    if existing:
        paths.append(existing)

    env["LEAN_PATH"] = ":".join(paths)

    return env


@app.get("/health")
def health():
    checks = {}

    commands = [
        ("lean", ["lean", "--version"]),
        ("lake", ["lake", "--version"]),
    ]

    for name, command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=LEAN_PROJECT,
                env=lean_env(),
                capture_output=True,
                text=True,
                timeout=10,
            )

            checks[name] = {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }

        except subprocess.TimeoutExpired:
            checks[name] = {
                "timeout": True,
            }

    mathlib_olean = (
        f"{LEAN_PROJECT}/.lake/packages/mathlib/"
        ".lake/build/lib/lean/Mathlib.olean"
    )

    checks["mathlib"] = {
        "Mathlib.olean_exists": os.path.exists(mathlib_olean),
        "path": mathlib_olean,
    }

    checks["lean_path"] = lean_env().get("LEAN_PATH", "").split(":")

    return checks


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

        # IMPORTANT:
        # Invoke Lean directly. Do not invoke lake.
        result = subprocess.run(
            ["lean", path],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=30,
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