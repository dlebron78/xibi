import pytest
from xibi.handles import HandleStore, ToolHandle, _is_large_collection
from xibi.errors import XibiError, ErrorCategory

def test_create_returns_short_id():
    store = HandleStore()
    handle = store.create("test_tool", {"foo": "bar"})
    assert handle.handle_id.startswith("h_")
    assert len(handle.handle_id) in (6, 8) # h_ + 4 or 6 hex chars

def test_get_returns_original_payload():
    store = HandleStore()
    payload = {"data": [i for i in range(100)]}
    handle = store.create("test_tool", payload)
    assert store.get(handle.handle_id) == payload

def test_max_handles_evicts_oldest():
    store = HandleStore(max_handles=2)
    h1 = store.create("tool1", "p1")
    h2 = store.create("tool2", "p2")
    h3 = store.create("tool3", "p3")

    # h1 should be gone
    with pytest.raises(XibiError) as excinfo:
        store.get(h1.handle_id)
    assert excinfo.value.category == ErrorCategory.VALIDATION
    assert "evicted" in excinfo.value.message

    assert store.get(h2.handle_id) == "p2"
    assert store.get(h3.handle_id) == "p3"

def test_max_bytes_evicts_oldest():
    # Each char is 1 byte in JSON string
    store = HandleStore(max_total_bytes=10)
    h1 = store.create("tool1", "1234") # "1234" -> 6 bytes in JSON
    h2 = store.create("tool2", "5678") # "5678" -> 6 bytes in JSON, total 12 > 10

    # h1 should be evicted
    with pytest.raises(XibiError):
        store.get(h1.handle_id)
    assert store.get(h2.handle_id) == "5678"

def test_get_evicted_raises_validation_error():
    store = HandleStore(max_handles=1)
    h1 = store.create("tool1", "p1")
    store.create("tool2", "p2")

    with pytest.raises(XibiError) as excinfo:
        store.get(h1.handle_id)
    assert excinfo.value.category == ErrorCategory.VALIDATION
    assert "evicted" in excinfo.value.message

def test_handle_id_does_not_leak_payload():
    store = HandleStore()
    payload = "SECRET_DATA_12345"
    handle = store.create("test_tool", payload)
    assert handle.handle_id not in payload
    # Also check if part of ID is in payload (unlikely but good to check randomness)
    suffix = handle.handle_id[2:]
    assert suffix not in payload

def test_is_large_collection():
    assert _is_large_collection([i for i in range(20)]) is True
    assert _is_large_collection([i for i in range(19)]) is False
    assert _is_large_collection({"jobs": [i for i in range(20)]}) is True
    assert _is_large_collection({"data": [i for i in range(19)]}) is False
    assert _is_large_collection({"foo": "bar"}) is False
