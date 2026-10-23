"""Persist each report's image and overlay legend as one immutable artifact."""
from __future__ import annotations

import base64
import fcntl
import json
import os
import tempfile

from app.services.chart_media import data_root
from app.services.daily_outlook_image import chart_snapshot, render_report_image


def saved_report_image(payload: dict) -> dict:
    run_id = int(payload["id"])
    if run_id <= 0:
        raise ValueError("invalid_report_id")
    folder = data_root() / "media" / "daily-outlook"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"run_{run_id}.json"
    # Separate processes and concurrent requests must share the same first render.
    with (folder / f"run_{run_id}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            return json.loads(destination.read_text(encoding="utf-8"))
        overlays = chart_snapshot(payload["context"], payload["report"])
        png = render_report_image(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        artifact = {
            "data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            "filename": f"daily-outlook-{payload['date']}-{run_id}.png",
            "nearby": overlays["nearby"], "channel": overlays["channel"],
        }
        # One atomic file keeps the image and legend consistent across restarts.
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                             prefix=f"run_{run_id}.", suffix=".tmp",
                                             delete=False) as target:
                temp_path = target.name
                json.dump(artifact, target, ensure_ascii=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, destination)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        return artifact
