"""Prometheus metrics for mp-relay.

Scope:
- Submission flow visibility (what input kinds come in, accept/duplicate/error)
- JAV search (sukebei) visibility
- Pipeline step outcomes + durations (triage / merge / remux / qc / mdcx / post-cleanup)
- Final pipeline-run terminal state (scraped / qc_failed_exhausted / scrape_failed / ...)
- File-ops counters (deletions by category, extras moved, parts merged)
- Currently in-flight task counts by state (gauge, refreshed by watcher tick)

Design notes:
- All metrics live in this module; callers ``import app.metrics as m`` and call.
- Metric names start with ``mp_relay_`` to avoid colliding in shared Prometheus.
- Histogram buckets are tuned for JAV-pipeline timescales (sub-second probe ops
  through 1-hour mdcx scrapes). Don't blindly use prometheus_client defaults.
- The async context manager ``timed_step`` wraps a code block and records
  duration into the per-step histogram, even on exception.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Use the default global registry so prometheus_client's generate_latest()
# picks everything up without us threading a registry around.
# (If we ever want test isolation, we can swap to a private CollectorRegistry.)


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

SUBMIT_TOTAL: Counter = Counter(
    "mp_relay_submit_total",
    "Inputs submitted to /submit, /api/jav-add, /api/bulk-subscribe etc.",
    ["kind", "result"],   # kind=jav_code|jav_magnet|magnet|media_name|id_ref|jav_torrent|torrent
                          # result=accepted|duplicate|error
)


# ---------------------------------------------------------------------------
# JAV search (sukebei)
# ---------------------------------------------------------------------------

JAV_SEARCH_TOTAL: Counter = Counter(
    "mp_relay_jav_search_total",
    "JAV code searches against sukebei.nyaa.si",
    ["result"],  # cached | hit | empty | error
)
JAV_SEARCH_DURATION: Histogram = Histogram(
    "mp_relay_jav_search_duration_seconds",
    "Time spent fetching+parsing sukebei (cache miss only)",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
)


# ---------------------------------------------------------------------------
# Pipeline (per-completed-torrent, runs in watcher._process_done)
# ---------------------------------------------------------------------------

PIPELINE_STEP_TOTAL: Counter = Counter(
    "mp_relay_pipeline_step_total",
    "Pipeline step outcomes per torrent",
    ["step", "outcome"],
    # step=triage|execute|relocate_extras|remux_disc|merge_parts|qc|mdcx|post_cleanup
    # outcome=ok|fail|skip
)
PIPELINE_STEP_DURATION: Histogram = Histogram(
    "mp_relay_pipeline_step_duration_seconds",
    "Pipeline step duration",
    ["step"],
    buckets=(0.5, 2, 10, 30, 60, 300, 600, 1800, 3600, 7200),
)
PIPELINE_RUN_TOTAL: Counter = Counter(
    "mp_relay_pipeline_run_total",
    "Pipeline runs by final terminal state",
    ["terminal_state"],
    # terminal_state=scraped|scrape_failed|qc_failed_retried|qc_failed_exhausted
    #                |qc_failed_no_alt|qc_failed_no_code|pre_mdcx_failed
)


# ---------------------------------------------------------------------------
# QC + retry chain
# ---------------------------------------------------------------------------

QC_RESULT: Counter = Counter(
    "mp_relay_qc_total",
    "QC pass/fail count",
    ["result", "reason_class"],
    # result=pass|fail
    # reason_class=ok|duration|size|no_video|ffprobe_unavailable
)
QC_RETRY: Counter = Counter(
    "mp_relay_qc_retry_total",
    "QC retry chain outcomes",
    ["outcome"],   # swapped|exhausted|no_alt|no_code
)


# ---------------------------------------------------------------------------
# File ops (cleanup + merger)
# ---------------------------------------------------------------------------

FILES_DELETED: Counter = Counter(
    "mp_relay_files_deleted_total",
    "Files deleted by cleanup",
    ["category"],  # junk|dupe|sample|post_mdcx
)
EXTRAS_RELOCATED: Counter = Counter(
    "mp_relay_extras_relocated_total",
    "Extras files moved to Extras/ subfolder",
)
MULTIPART_MERGED: Counter = Counter(
    "mp_relay_multipart_merged_total",
    "Multi-part merge outcomes",
    ["outcome"],   # concat_copy|fallback_rename|skip|fail
)
DISC_REMUX: Counter = Counter(
    "mp_relay_disc_remux_total",
    "Disc archive remux outcomes",
    ["kind", "outcome"],
    # kind=bdmv|dvd
    # outcome=success|fail
)


# ---------------------------------------------------------------------------
# mdcx
# ---------------------------------------------------------------------------

MDCX_RUN: Counter = Counter(
    "mp_relay_mdcx_total",
    "mdcx scrape outcomes",
    ["result"],   # ok|fail|timeout|skipped
)


# ---------------------------------------------------------------------------
# Inflight gauge — refreshed by watcher tick from store.list_recent()
# ---------------------------------------------------------------------------

INFLIGHT: Gauge = Gauge(
    "mp_relay_inflight",
    "Currently in-flight tasks by state",
    ["state"],
)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

@asynccontextmanager
async def timed_step(step: str) -> AsyncIterator[None]:
    """Async context manager that records a step's duration into the
    PIPELINE_STEP_DURATION histogram, even if the body raises.

    Usage:
        async with timed_step("merge_parts"):
            await merger.merge_parts(parts)
    """
    start = time.monotonic()
    try:
        yield
    finally:
        PIPELINE_STEP_DURATION.labels(step=step).observe(time.monotonic() - start)


def refresh_inflight_gauge(tasks: list[dict]) -> None:
    """Recompute INFLIGHT from a snapshot of recent tasks.

    Called periodically by the watcher tick. Resets all known states each call
    so removed states don't stick around (Prometheus gauges otherwise persist).
    """
    counts: dict[str, int] = {}
    for t in tasks:
        st = t.get("state") or "unknown"
        counts[st] = counts.get(st, 0) + 1
    # Reset every label that ever had a value, then re-set from snapshot.
    # Easiest: clear and repopulate via _metrics dict (private but stable since 0.x).
    INFLIGHT.clear()
    for state, n in counts.items():
        INFLIGHT.labels(state=state).set(n)


def classify_qc_reason(reason: str) -> str:
    """Bucket a QcResult.reason string into a small set of reason classes for
    metric labels (avoids cardinality explosion from free-text reasons)."""
    r = (reason or "").lower()
    if r.startswith("ok"):
        return "ok"
    if "no video file" in r:
        return "no_video"
    if "ffprobe unavailable" in r:
        return "ffprobe_unavailable"
    if "duration" in r:
        return "duration"
    if "mib" in r or "size" in r:
        return "size"
    return "other"
