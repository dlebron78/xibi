"""Topic normalization helpers."""


def normalize_topic(topic: str | None) -> str | None:
    """Consolidates fragmented topics (e.g. scheduling -> schedule)."""
    if not topic:
        return None

    # 1. Lowercase + cleanup
    t = topic.lower().replace("_", " ").strip()

    # 2. Stopwords
    stopwords = {"my", "the", "a", "an", "this", "our", "your", "on", "for"}
    words = [w for w in t.split() if w not in stopwords]
    if not words:
        return t  # fallback to raw if we stripped everything

    t = " ".join(words)

    # 3. Simple Stemming (suffix stripping)
    suffixes = ["ing", "s"]
    for suffix in suffixes:
        if t.endswith(suffix) and len(t) > len(suffix) + 2:
            t = t[: -len(suffix)]
            break

    # 4. Synonym Mapping
    synonyms = {
        "calendar": "schedule",
        "schedul": "schedule",
        "schedular": "schedule",
        "mail": "email",
        "inbox": "email",
        "message": "chat",
        "presentation_deck": "presentation deck",
        "deck": "presentation deck",
    }
    return synonyms.get(t, t)
