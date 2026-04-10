import pytest
import os
from xibi.skills.sample.email import handler

def test_list_emails_simple():
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("XIBI_TEST_REALISTIC_INBOX", "0")
        res = handler.list_emails({"max_results": 2})
        assert len(res["emails"]) == 2
        assert res["emails"][0]["sender"] == "boss@work.com"

def test_list_emails_realistic():
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("XIBI_TEST_REALISTIC_INBOX", "1")
        res = handler.list_emails({"max_results": 2})
        assert len(res["emails"]) == 2
        assert res["emails"][0]["sender"] == "sarah.chen@acme.com"

def test_triage_email_simple():
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("XIBI_TEST_REALISTIC_INBOX", "0")
        res = handler.triage_email({})
        assert "urgent" in res
        assert res["urgent"][0]["sender"] == "boss@work.com"

def test_triage_email_realistic():
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("XIBI_TEST_REALISTIC_INBOX", "1")
        res = handler.triage_email({})
        assert "emails" in res
        assert res["emails"][0]["sender"] == "sarah.chen@acme.com"

def test_send_email():
    res = handler.send_email({"to": "test@example.com", "subject": "Hello"})
    assert res["status"] == "ok"
    assert "test@example.com" in res["message"]
