"""Build identity for GET /api/version.

The version comes from the APP_VERSION build arg when present (release
builds), otherwise from version.txt at the repo root - the file
release-please (release-type 'simple') maintains with every release PR.
The commit is the GIT_SHA build arg; 'unknown' outside container builds.
"""

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def version() -> str:
    env = os.environ.get("APP_VERSION", "").strip()
    if env:
        return env
    try:
        return (_ROOT / "version.txt").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def commit() -> str:
    return os.environ.get("GIT_SHA", "").strip() or "unknown"


def info() -> dict:
    return {"version": version(), "commit": commit()}
