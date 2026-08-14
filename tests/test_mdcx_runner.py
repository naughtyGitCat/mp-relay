"""Tests for MDCx subprocess output parsing and diagnostics."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_parse_mdcx_stdout_handles_nested_failed_items() -> None:
    from app.mdcx_runner import _parse_mdcx_stdout

    summary = {
        "total": 1,
        "success": 0,
        "failed": 1,
        "failed_items": [
            {
                "path": r"E:\Jav_failed\CEAD-357.mp4",
                "reason": "不在官网番号前缀列表中",
            }
        ],
    }
    stdout = "🎉 All finished!!!\n" + json.dumps(summary, ensure_ascii=False, indent=2) + "\n"

    assert _parse_mdcx_stdout(stdout) == summary


def test_parse_mdcx_stdout_rejects_trailing_non_whitespace() -> None:
    from app.mdcx_runner import _parse_mdcx_stdout

    assert _parse_mdcx_stdout('prefix {"total": 1} trailing') is None


def test_diagnostic_tail_keeps_actual_error_after_warning() -> None:
    from app.mdcx_runner import _diagnostic_tail

    warning = "pkg_resources is deprecated. " * 30
    diagnostic = warning + "不在官网番号前缀列表中"

    result = _diagnostic_tail(diagnostic, limit=80)

    assert result.endswith("不在官网番号前缀列表中")
    assert len(result) <= 80
