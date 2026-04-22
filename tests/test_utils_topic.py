"""Direct unit tests for xibi.utils.topic.normalize_topic.

Ports were byte-identical from bregger_utils.normalize_topic. Quirks
preserved: whitespace-only returns empty string (not None), `"running"`
stems to `"runn"`, `"scheduling"` → `"schedul"` → synonym → `"schedule"`.
"""

import pytest

from xibi.utils.topic import normalize_topic


def test_none_input_returns_none():
    assert normalize_topic(None) is None


def test_empty_string_returns_none():
    # Falsy short-circuit at the top of normalize_topic.
    assert normalize_topic("") is None


def test_whitespace_only_returns_empty_string():
    # Documented quirk: whitespace is truthy, so the function proceeds,
    # strips to "", finds no words, and returns the raw stripped string.
    assert normalize_topic("   ") == ""


def test_mixed_case_and_surrounding_whitespace():
    assert normalize_topic("  Running/weekly 10k  ") == "running/weekly 10k"


def test_underscore_becomes_space():
    # Underscores are normalized to spaces before stopword + stemming logic.
    # "my_topic" → lowercase → "my topic" → stopword "my" stripped → "topic"
    # → no suffix match → returns "topic".
    assert normalize_topic("my_topic") == "topic"


def test_stopword_stripping():
    # "the project" → filter "the" → "project" → suffix "s"? no → "project"
    assert normalize_topic("the project") == "project"


def test_stopwords_only_falls_back_to_raw():
    # All tokens are stopwords → fall back to the pre-filter string.
    assert normalize_topic("the my a") == "the my a"


def test_suffix_stemming_ing():
    # "running" → suffix "ing" stripped → "runn" (quirk: preserved verbatim).
    assert normalize_topic("running") == "runn"


def test_synonym_scheduling_to_schedule():
    # "scheduling" → strip "ing" → "schedul" → synonym → "schedule"
    assert normalize_topic("scheduling") == "schedule"


def test_synonym_calendar_to_schedule():
    assert normalize_topic("calendar") == "schedule"


def test_synonym_calendars_plural_to_schedule():
    # "calendars" → strip "s" → "calendar" → synonym → "schedule"
    assert normalize_topic("calendars") == "schedule"


def test_synonym_mail_to_email():
    assert normalize_topic("mail") == "email"


def test_synonym_deck_to_presentation_deck():
    assert normalize_topic("deck") == "presentation deck"


@pytest.mark.parametrize(
    "raw",
    [
        "scheduling",
        "calendar",
        "the project",
        "running",
        "mail",
        "deck",
        "hello world",
        "my_topic",
    ],
)
def test_idempotence(raw):
    # f(f(x)) == f(x) for valid string inputs.
    once = normalize_topic(raw)
    twice = normalize_topic(once)
    assert once == twice
