"""Tests for metrics.py — context manager + helpers + emission shape."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_classify_qc_reason_buckets():
    from app.metrics import classify_qc_reason
    assert classify_qc_reason("OK: 95.4min, 5023 MiB") == "ok"
    assert classify_qc_reason("no video file found under /foo") == "no_video"
    assert classify_qc_reason("ffprobe unavailable; duration check skipped") == "ffprobe_unavailable"
    assert classify_qc_reason("duration 12.3min < required 30min (file: x.mp4)") == "duration"
    assert classify_qc_reason("largest video x.mp4 is only 50 MiB (< 200)") == "size"
    assert classify_qc_reason("") == "other"   # falls through everything
    assert classify_qc_reason("something weird happened") == "other"


def _hist_count(hist, **labels) -> int:
    """Pull the total observation count for a labeled histogram out of its
    collected samples (more stable across prometheus_client versions than
    poking ``_count``)."""
    suffix = "_count"
    for metric in hist.collect():
        for sample in metric.samples:
            if not sample.name.endswith(suffix):
                continue
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return int(sample.value)
    return 0


def test_timed_step_records_duration():
    """timed_step should emit a histogram observation even on success."""
    from app.metrics import PIPELINE_STEP_DURATION, timed_step

    async def body():
        async with timed_step("test_step"):
            await asyncio.sleep(0.01)

    before = _hist_count(PIPELINE_STEP_DURATION, step="test_step")
    asyncio.run(body())
    after = _hist_count(PIPELINE_STEP_DURATION, step="test_step")
    assert after == before + 1


def test_timed_step_records_on_exception():
    """timed_step must observe duration even if the body raises."""
    from app.metrics import PIPELINE_STEP_DURATION, timed_step

    async def body():
        async with timed_step("test_step_err"):
            await asyncio.sleep(0.005)
            raise RuntimeError("boom")

    before = _hist_count(PIPELINE_STEP_DURATION, step="test_step_err")
    try:
        asyncio.run(body())
    except RuntimeError:
        pass
    after = _hist_count(PIPELINE_STEP_DURATION, step="test_step_err")
    assert after == before + 1


def _gauge_value(gauge, **labels) -> float:
    for metric in gauge.collect():
        for sample in metric.samples:
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0.0


def _gauge_labels_present(gauge) -> set[str]:
    """Return the set of `state` label values currently present on the gauge."""
    out: set[str] = set()
    for metric in gauge.collect():
        for sample in metric.samples:
            if "state" in sample.labels:
                out.add(sample.labels["state"])
    return out


def test_refresh_inflight_gauge():
    from app.metrics import INFLIGHT, refresh_inflight_gauge
    tasks = [
        {"state": "scraping"},
        {"state": "scraping"},
        {"state": "downloading"},
        {"state": "scraped"},
    ]
    refresh_inflight_gauge(tasks)
    assert _gauge_value(INFLIGHT, state="scraping") == 2
    assert _gauge_value(INFLIGHT, state="downloading") == 1
    assert _gauge_value(INFLIGHT, state="scraped") == 1

    # New snapshot — old labels should NOT carry stale counts.
    refresh_inflight_gauge([{"state": "scraped"}])
    present = _gauge_labels_present(INFLIGHT)
    assert "scraped" in present
    # downloading + scraping should be cleared (not present at all)
    assert "downloading" not in present
    assert "scraping" not in present


def test_metrics_endpoint_renders():
    """generate_latest() should emit our metric names."""
    from prometheus_client import generate_latest

    # Touch a few counters so they're registered with at least one observation
    from app.metrics import (
        SUBMIT_TOTAL, JAV_SEARCH_TOTAL, PIPELINE_RUN_TOTAL,
        QC_RESULT, MDCX_RUN, FILES_DELETED,
    )
    SUBMIT_TOTAL.labels(kind="jav_code", result="accepted").inc()
    JAV_SEARCH_TOTAL.labels(result="hit").inc()
    PIPELINE_RUN_TOTAL.labels(terminal_state="scraped").inc()
    QC_RESULT.labels(result="pass", reason_class="ok").inc()
    MDCX_RUN.labels(result="ok").inc()
    FILES_DELETED.labels(category="junk").inc(3)

    blob = generate_latest().decode("utf-8")
    for name in (
        "mp_relay_submit_total",
        "mp_relay_jav_search_total",
        "mp_relay_pipeline_run_total",
        "mp_relay_qc_total",
        "mp_relay_mdcx_total",
        "mp_relay_files_deleted_total",
    ):
        assert name in blob, f"{name} missing from /metrics output"

    # Check label values render properly
    assert 'kind="jav_code"' in blob
    assert 'result="accepted"' in blob
