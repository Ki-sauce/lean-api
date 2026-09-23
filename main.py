import os
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel


# ============================================================================
# Configuration
# ============================================================================

LEAN_PROJECT = Path("/app/leanverify")
CHECK_TIMEOUT = 90


def lean_env() -> dict[str, str]:
    env = os.environ.copy()

    paths = [
        str(
            LEAN_PROJECT
            / ".lake"
            / "build"
            / "lib"
            / "lean"
        )
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
# FastAPI
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(
    lifespan=lifespan,
)


# ============================================================================
# API
# ============================================================================

class CheckRequest(BaseModel):
    source: str


# ============================================================================
# Lean verification
# ============================================================================

def check_source(
    source: str,
    timeout: int = CHECK_TIMEOUT,
) -> dict[str, Any]:

    started = time.monotonic()

    # Keep temporary files inside the Lean project so that:
    #
    #   import Mathlib
    #
    # and the project's LEAN_PATH/environment behave exactly as they
    # do for an ordinary Lean source file.
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
            [
                "lean",
                str(path),
            ],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        elapsed = (
            time.monotonic()
            - started
        )

        output = process.stdout.strip()
        error = process.stderr.strip()

        # Lean normally puts diagnostics on stdout/stderr depending
        # on the diagnostic/configuration path, so preserve both.
        diagnostics = "\n".join(
            x
            for x in [output, error]
            if x
        )

        return {
            "success": process.returncode == 0,
            "exit_code": process.returncode,
            "elapsed_seconds": round(
                elapsed,
                3,
            ),
            "diagnostics": diagnostics,
        }

    except subprocess.TimeoutExpired as exc:

        elapsed = (
            time.monotonic()
            - started
        )

        stdout = (
            exc.stdout.decode(
                "utf-8",
                errors="replace",
            )
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )

        stderr = (
            exc.stderr.decode(
                "utf-8",
                errors="replace",
            )
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )

        return {
            "success": False,
            "exit_code": None,
            "elapsed_seconds": round(
                elapsed,
                3,
            ),
            "error": "Lean process timed out",
            "diagnostics": "\n".join(
                x.strip()
                for x in [stdout, stderr]
                if x and x.strip()
            ),
        }

    finally:

        try:
            path.unlink()
        except FileNotFoundError:
            pass


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
        }


# ============================================================================
# /health
# ============================================================================

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
    }

    # ------------------------------------------------------------------------
    # Lean
    # ------------------------------------------------------------------------

    try:

        process = subprocess.run(
            [
                "lean",
                "--version",
            ],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )

        result["lean"] = {
            "ok": process.returncode == 0,
            "version": process.stdout.strip(),
            "error": process.stderr.strip() or None,
        }

    except Exception as exc:

        result["lean"] = {
            "ok": False,
            "error": repr(exc),
        }

    # ------------------------------------------------------------------------
    # Lake
    # ------------------------------------------------------------------------

    try:

        process = subprocess.run(
            [
                "lake",
                "--version",
            ],
            cwd=LEAN_PROJECT,
            env=lean_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )

        result["lake"] = {
            "ok": process.returncode == 0,
            "version": process.stdout.strip(),
            "error": process.stderr.strip() or None,
        }

    except Exception as exc:

        result["lake"] = {
            "ok": False,
            "error": repr(exc),
        }

    # ------------------------------------------------------------------------
    # Mathlib
    # ------------------------------------------------------------------------

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
            round(
                mathlib_olean.stat().st_size
                / 1024
                / 1024,
                2,
            )
            if mathlib_olean.exists()
            else 0
        ),
    }

    # ------------------------------------------------------------------------
    # Real verification test
    #
    # This is deliberately FALSE.
    #
    # If this returns success=True, the verifier is broken.
    # ------------------------------------------------------------------------

    test_source = """
theorem leanverify_health_test : (8 : Nat) + 1 = 2 := rfl
"""

    try:

        verification = check_source(
            test_source,
            timeout=30,
        )

        result["verification_test"] = verification

        if verification["success"]:
            result["status"] = "degraded"

    except Exception as exc:

        result["status"] = "degraded"

        result["verification_test"] = {
            "success": False,
            "error": repr(exc),
        }

    return result