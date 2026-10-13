from __future__ import annotations

import os
import re
from pathlib import Path

_RUN_CHART_RE = re.compile(r"^run_\d+\.png$")


def data_root() -> Path:
    return Path(os.getenv("DATA_DIR", "data"))


def charts_dir() -> Path:
    path = data_root() / "media" / "charts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def chart_filename_for_run(run_id: int) -> str:
    return f"run_{int(run_id)}.png"


def chart_path_for_run(run_id: int) -> Path:
    return charts_dir() / chart_filename_for_run(run_id)


def resolve_chart_path(filename: str) -> Path | None:
    if not _RUN_CHART_RE.match(filename):
        return None
    path = charts_dir() / filename
    if not path.is_file():
        return None
    try:
        path.resolve().relative_to(charts_dir().resolve())
    except ValueError:
        return None
    return path


def chart_artifact_meta(run_id: int, *, error: str | None = None) -> dict:
    meta: dict = {
        "filename": chart_filename_for_run(run_id),
        "media_key": chart_filename_for_run(run_id),
    }
    if error:
        meta["error"] = error[:500]
    return meta
