"""Tests for JAV code extraction (filesystem-free)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_extract_code_basic():
    from app.exists import extract_code
    assert extract_code("SSIS-001 The Best Title") == "SSIS-001"
    assert extract_code("[somesite]SSIS-001-A 4K HDR") == "SSIS-001-A"
    assert extract_code("Old release SSIS001 no dash") == "SSIS001"


def test_extract_code_fc2():
    from app.exists import extract_code
    for inp in ("FC2-PPV-1234567", "FC2PPV1234567", "FC2 PPV 1234567"):
        assert extract_code(inp) == "FC2-PPV-1234567"


def test_extract_code_heyzo():
    from app.exists import extract_code
    assert extract_code("HEYZO-1234") == "HEYZO-1234"
    assert extract_code("heyzo 1234 untouched") == "HEYZO-1234"


def test_extract_code_numeric_series():
    from app.exists import extract_code
    assert extract_code("121319_001 1pondo title") == "121319_001"


def test_extract_code_no_match():
    from app.exists import extract_code
    assert extract_code("Big Buck Bunny 2008") is None
    assert extract_code("Random text without codes") is None
    assert extract_code("") is None


def test_normalise():
    from app.exists import _normalise
    assert _normalise("SSIS-001") == _normalise("SSIS 001") == _normalise("SSIS_001") == "SSIS001"
    assert _normalise("Hello.World") == "HELLOWORLD"
