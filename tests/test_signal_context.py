import pytest
from xibi.heartbeat.context_assembly import SignalContext, EmailContext, assemble_signal_context, assemble_email_context

def test_signal_context_fields():
    ctx = SignalContext(
        signal_ref_id="sig123",
        sender_id="user@example.com",
        sender_name="User",
        headline="Hello",
        source_channel="slack"
    )
    assert ctx.signal_ref_id == "sig123"
    assert ctx.sender_id == "user@example.com"
    assert ctx.sender_name == "User"
    assert ctx.headline == "Hello"
    assert ctx.source_channel == "slack"

def test_email_context_alias():
    ctx = EmailContext(
        signal_ref_id="sig123",
        sender_id="user@example.com",
        sender_name="User",
        headline="Hello"
    )
    assert isinstance(ctx, SignalContext)

def test_old_field_names_via_property():
    ctx = SignalContext(
        signal_ref_id="sig123",
        sender_id="user@example.com",
        sender_name="User",
        headline="Hello"
    )
    assert ctx.email_id == "sig123"
    assert ctx.sender_addr == "user@example.com"
    assert ctx.subject == "Hello"

def test_assemble_signal_context(tmp_path):
    email = {"id": "e1", "subject": "Test"}
    db = tmp_path / "empty.db"
    # Just verify it returns SignalContext
    ctx = assemble_signal_context(email, db)
    assert isinstance(ctx, SignalContext)
    assert ctx.headline == "Test"

def test_assemble_email_context_alias(tmp_path):
    email = {"id": "e1", "subject": "Test"}
    db = tmp_path / "empty.db"
    ctx = assemble_email_context(email, db)
    assert isinstance(ctx, SignalContext)

def test_source_channel_field():
    ctx = SignalContext(
        signal_ref_id="1", sender_id="2", sender_name="3", headline="4", source_channel="calendar"
    )
    assert ctx.source_channel == "calendar"
