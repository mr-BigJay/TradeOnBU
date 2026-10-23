from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from app.services import daily_outlook_artifact as artifact


@pytest.fixture
def rendering(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    render = Mock(return_value=b"\x89PNG\r\n\x1a\nexample")
    overlays = Mock(return_value={"nearby": {"support": [], "resistance": []},
                                  "channel": {"available": False}})
    monkeypatch.setattr(artifact, "render_report_image", render)
    monkeypatch.setattr(artifact, "chart_snapshot", overlays)
    return render, overlays


def payload(run_id=1):
    return {"id": run_id, "date": "2026-10-23", "context": {}, "report": {}}


def test_saved_image_survives_renderer_changes(rendering, monkeypatch):
    first = artifact.saved_report_image(payload())
    # No in-memory cache exists here: subsequent reads use the durable file,
    # even if the renderer/geometry has changed in a later deployment.
    rendering[0].side_effect = AssertionError("must not render again")
    rendering[1].side_effect = AssertionError("must not recompute legend")
    assert artifact.saved_report_image(payload()) == first
    assert rendering[0].call_count == 1
    assert rendering[1].call_count == 1


def test_concurrent_first_visits_render_once(rendering):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(artifact.saved_report_image, [payload()] * 4))
    assert all(result == results[0] for result in results)
    assert rendering[0].call_count == 1


def test_distinct_reports_have_distinct_artifacts(rendering):
    assert artifact.saved_report_image(payload(1))["filename"] != artifact.saved_report_image(payload(2))["filename"]
    assert rendering[0].call_count == 2


def test_failed_write_is_retryable(rendering, monkeypatch, tmp_path):
    original = artifact.os.replace
    monkeypatch.setattr(artifact.os, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        artifact.saved_report_image(payload())
    folder = tmp_path / "media" / "daily-outlook"
    assert not list(folder.glob("*.json"))
    assert not list(folder.glob("*.tmp"))
    monkeypatch.setattr(artifact.os, "replace", original)
    assert artifact.saved_report_image(payload())["data_url"].startswith("data:image/png;base64,")
