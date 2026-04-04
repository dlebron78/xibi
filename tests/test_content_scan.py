from __future__ import annotations

import pytest
from xibi.security.content_scan import has_sensitive_content

def test_sensitive_content_detected():
    # input with "password" triggers scan
    assert has_sensitive_content({"msg": "Your password is 123"})
    assert has_sensitive_content({"key": "api_key", "value": "xyz"})
    assert has_sensitive_content({"body": "My bank account is 12345"})

def test_benign_content_passes():
    # normal email content passes scan
    assert not has_sensitive_content({"subject": "Hello", "body": "How are you?"})
    assert not has_sensitive_content({"query": "What is the capital of France?"})

def test_scan_checks_all_values():
    # sensitive content in any field value is caught
    assert has_sensitive_content({"from": "me", "to": "them", "body": "secret project info"})
    assert has_sensitive_content({"token": "abcde"})
