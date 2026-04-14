from xibi.heartbeat.classification import VALID_TIERS, parse_classification_response


def test_parse_tier_with_reasoning():
    tier, reasoning = parse_classification_response("CRITICAL: Established contact about today's deadline.")
    assert tier == "CRITICAL"
    assert reasoning == "Established contact about today's deadline."


def test_parse_tier_only():
    tier, reasoning = parse_classification_response("HIGH")
    assert tier == "HIGH"
    assert reasoning is None


def test_parse_legacy_urgent():
    tier, reasoning = parse_classification_response("URGENT")
    assert tier == "CRITICAL"
    assert reasoning is None


def test_parse_legacy_digest():
    tier, reasoning = parse_classification_response("DIGEST")
    assert tier == "MEDIUM"
    assert reasoning is None


def test_parse_garbage():
    tier, reasoning = parse_classification_response("I think this is important")
    assert tier == "MEDIUM"
    assert reasoning is None


def test_parse_lowercase():
    tier, reasoning = parse_classification_response("critical: some reasoning")
    assert tier == "CRITICAL"
    assert reasoning == "some reasoning"


def test_valid_tiers_set():
    assert {"CRITICAL", "HIGH", "MEDIUM", "LOW", "NOISE"} == VALID_TIERS


def test_parse_empty():
    tier, reasoning = parse_classification_response("")
    assert tier == "MEDIUM"
    assert reasoning is None
