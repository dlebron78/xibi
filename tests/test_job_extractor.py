from __future__ import annotations

from xibi.heartbeat.extractors import SignalExtractorRegistry, _normalize_company


def test_normalize_company():
    assert _normalize_company("Stripe, Inc.") == "Stripe"
    assert _normalize_company("Apple Inc.") == "Apple"
    assert _normalize_company("Google LLC") == "Google"
    assert _normalize_company("Microsoft Corp.") == "Microsoft"
    assert _normalize_company("OpenAI Co.") == "OpenAI"
    assert _normalize_company("Siemens AG") == "Siemens"
    assert _normalize_company("SAP SE") == "SAP"
    assert _normalize_company("BP PLC") == "BP"
    assert _normalize_company("Generic Company") == "Generic Company"


def test_jobs_extractor_structured():
    data = {
        "status": "ok",
        "structured": {
            "jobs": [
                {
                    "id": "job-123",
                    "title": "Product Manager",
                    "company": "Stripe, Inc.",
                    "location": "Miami, FL",
                    "salary_min": 130000,
                    "salary_max": 160000,
                    "url": "https://stripe.com/jobs/123",
                    "posted_at": "2026-04-03",
                }
            ]
        }
    }
    signals = SignalExtractorRegistry.extract("jobs", "jobspy_source", data, {})
    assert len(signals) == 1
    sig = signals[0]
    assert sig["source"] == "jobspy_source"
    assert sig["type"] == "job_listing"
    assert sig["entity_text"] == "Stripe"
    assert sig["entity_type"] == "company"
    assert sig["topic_hint"] == "Product Manager at Stripe"
    assert sig["content_preview"] == "Product Manager | Stripe | Miami, FL | $130,000–$160,000"
    assert sig["ref_id"] == "job-123"
    assert sig["ref_source"] == "jobspy"
    assert sig["metadata"]["url"] == "https://stripe.com/jobs/123"


def test_jobs_extractor_unstructured_fallback():
    data = {"status": "ok", "result": "Found 1 job: Software Engineer at Acme"}
    signals = SignalExtractorRegistry.extract("jobs", "jobspy_source", data, {})
    assert len(signals) == 1
    sig = signals[0]
    assert sig["type"] == "job_batch"
    assert sig["needs_llm_extraction"] is True
    assert sig["extractor_hint"] == "jobs"
    assert sig["raw"] == "Found 1 job: Software Engineer at Acme"


def test_jobs_extractor_empty_jobs_list():
    data = {"status": "ok", "structured": {"jobs": []}}
    signals = SignalExtractorRegistry.extract("jobs", "jobspy_source", data, {})
    assert len(signals) == 0


def test_jobs_extractor_salary_range_handling():
    # Only min
    job_min = {"id": "1", "company": "A", "title": "T", "salary_min": 100}
    data = {"structured": {"jobs": [job_min]}}
    signals = SignalExtractorRegistry.extract("jobs", "s", data, {})
    assert "$100" not in signals[0]["content_preview"]  # Needs both per implementation

    # Both
    job_both = {"id": "1", "company": "A", "title": "T", "salary_min": 100, "salary_max": 200}
    data = {"structured": {"jobs": [job_both]}}
    signals = SignalExtractorRegistry.extract("jobs", "s", data, {})
    assert "$100–$200" in signals[0]["content_preview"]


def test_jobs_extractor_missing_fields_handled():
    # Minimal job info
    job = {"id": "job-999"}
    data = {"structured": {"jobs": [job]}}
    signals = SignalExtractorRegistry.extract("jobs", "s", data, {})
    assert len(signals) == 1
    sig = signals[0]
    assert sig["entity_text"] == "Unknown Company"
    assert sig["topic_hint"] == "Unknown Role at Unknown Company"
    assert sig["ref_id"] == "job-999"
    assert sig["metadata"]["url"] == ""
