"""Resolve repository root for tests (workspace, Docker /app with mounted scripts)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


@lru_cache
def repo_root() -> Path:
    env = os.environ.get("TRADEONBU_REPO_ROOT")
    if env:
        root = Path(env).resolve()
        if (root / "scripts" / "verify_helpers.py").is_file():
            return root
    here = Path(__file__).resolve()
    for candidate in (here.parents[2], here.parents[1].parent, *here.parents):
        if (candidate / "scripts" / "verify_helpers.py").is_file():
            return candidate.resolve()
    return here.parents[2].resolve()


def require_repo_file(relative: str) -> Path:
    path = repo_root() / relative
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing {relative} under repo root {repo_root()}. "
            "Mount scripts/ and docker-compose.yml at /app for Docker offline regression."
        )
    return path
