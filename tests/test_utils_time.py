"""Direct unit tests for xibi.utils.time.parse_semantic_datetime.

Before step-93 coverage was implicit through calendar skill integration tests.
These tests pin the contract: semantic tokens (today/tomorrow/weekday + HHMM),
ISO 8601 fallback, timezone handling, and the invalid-token failure path.
"""

import zoneinfo
from datetime import datetime, timedelta

import pytest

from xibi.utils.time import parse_semantic_datetime


def test_today_token_default_tz():
    result = parse_semantic_datetime("today_0900")
    assert isinstance(result, datetime)
    assert result.tzinfo is not None
    assert result.hour == 9
    assert result.minute == 0
    assert str(result.tzinfo) == "America/New_York"


def test_tomorrow_token_default_tz():
    now_et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    result = parse_semantic_datetime("tomorrow_1400")
    expected_date = (now_et + timedelta(days=1)).date()
    assert result.date() == expected_date
    assert result.hour == 14
    assert result.minute == 0


def test_weekday_token_friday():
    result = parse_semantic_datetime("friday_0930")
    assert result.weekday() == 4  # Friday
    assert result.hour == 9
    assert result.minute == 30
    assert result.tzinfo is not None


def test_explicit_utc_tz():
    result = parse_semantic_datetime("today_1200", ref_tz="UTC")
    assert str(result.tzinfo) == "UTC"
    assert result.hour == 12


def test_invalid_tz_falls_back_to_ny():
    result = parse_semantic_datetime("today_1000", ref_tz="Not/A_Real_TZ")
    assert str(result.tzinfo) == "America/New_York"


def test_iso_input_with_z_suffix():
    result = parse_semantic_datetime("2026-05-01T09:30:00Z")
    assert isinstance(result, datetime)
    assert result.tzinfo is not None
    # The Z is converted to +00:00 and then astimezone'd to default NY.
    assert str(result.tzinfo) == "America/New_York"


def test_iso_input_without_z():
    # Python's datetime.astimezone() on a naive datetime treats it as local
    # time, so naive ISO still parses and returns an aware datetime in ref_tz.
    result = parse_semantic_datetime("2026-05-01T09:30:00", ref_tz="UTC")
    assert isinstance(result, datetime)
    assert result.tzinfo is not None
    assert str(result.tzinfo) == "UTC"


def test_iso_input_with_offset():
    result = parse_semantic_datetime("2026-05-01T09:30:00+00:00", ref_tz="UTC")
    assert result.year == 2026
    assert result.month == 5
    assert result.day == 1
    assert result.hour == 9
    assert str(result.tzinfo) == "UTC"


def test_invalid_weekday_token_raises_value_error():
    with pytest.raises(ValueError):
        parse_semantic_datetime("banana_0900")


def test_invalid_token_shape_raises():
    # Not matching the semantic regex and not a valid ISO string.
    with pytest.raises(ValueError):
        parse_semantic_datetime("totally not a date")


def test_output_is_timezone_aware_for_all_valid_inputs():
    cases = [
        ("today_0800", None),
        ("tomorrow_2359", None),
        ("monday_0000", None),
        ("2026-05-01T09:30:00+00:00", "UTC"),
    ]
    for token, tz in cases:
        kwargs = {"ref_tz": tz} if tz else {}
        result = parse_semantic_datetime(token, **kwargs)
        assert result.tzinfo is not None, f"naive datetime from {token}"
