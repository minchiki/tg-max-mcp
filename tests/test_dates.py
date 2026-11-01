"""Разбор дат для инструментов истории: python -m pytest tests (из корня репозитория)."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from dates import parse_when  # noqa: E402

TZ = timezone(timedelta(hours=3))


@pytest.mark.parametrize("s,end,want", [
    ("2025-11-01", False, datetime(2025, 11, 1, 0, 0, 0, tzinfo=TZ)),
    ("2025-11-01", True, datetime(2025, 11, 1, 23, 59, 59, tzinfo=TZ)),
    ("31.10.2025", True, datetime(2025, 10, 31, 23, 59, 59, tzinfo=TZ)),
    ("2025-03", False, datetime(2025, 3, 1, tzinfo=TZ)),
    ("2025-03", True, datetime(2025, 3, 31, 23, 59, 59, tzinfo=TZ)),
    ("2024-02", True, datetime(2024, 2, 29, 23, 59, 59, tzinfo=TZ)),
    ("2025", True, datetime(2025, 12, 31, 23, 59, 59, tzinfo=TZ)),
    ("2025-11-01 14:30", True, datetime(2025, 11, 1, 14, 30, tzinfo=TZ)),
])
def test_absolute(s, end, want):
    assert parse_when(s, TZ, end) == want


@pytest.mark.parametrize("s,days", [("год назад", 365), ("1y", 365), ("2 года назад", 730), ("6 мес", 180), ("30д", 30), ("2w", 14), ("полгода назад", 182)])
def test_relative(s, days):
    got = datetime.now(TZ) - parse_when(s, TZ)
    assert abs(got - timedelta(days=days)) < timedelta(seconds=5)


def test_empty_and_bad():
    assert parse_when("", TZ) is None
    with pytest.raises(ValueError):
        parse_when("ерунда", TZ)
