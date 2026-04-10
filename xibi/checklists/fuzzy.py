from __future__ import annotations

import sqlite3


def fuzzy_match_item(db_path: str, instance_id: str, label_hint: str) -> dict | None:
    """
    Fuzzy-match a label hint against items in an instance.

    Returns the highest-scoring item if it's meaningfully ahead of second place.
    Returns None if no good match or ambiguous.
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        items = conn.execute("SELECT * FROM checklist_instance_items WHERE instance_id = ?", (instance_id,)).fetchall()

    if not items:
        return None

    # 1. Normalize both hint and candidate labels
    def normalize(text: str) -> set[str]:
        """Lowercase, tokenize, drop stopwords, return token set."""
        stopwords = {
            "the",
            "a",
            "an",
            "is",
            "are",
            "was",
            "were",
            "be",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "and",
            "or",
            "but",
            "if",
            "to",
            "of",
            "in",
            "on",
            "at",
            "by",
            "for",
            "with",
            "from",
        }
        tokens = text.lower().split()
        return {t.strip(",.!?;:") for t in tokens if t.lower() not in stopwords and len(t) > 0}

    hint_tokens = normalize(label_hint)

    scores = []
    for item in items:
        item_dict = dict(item)
        candidate_tokens = normalize(item_dict["label"])

        # 2. Compute overlap (token intersection)
        overlap = hint_tokens & candidate_tokens
        overlap_count = len(overlap)

        # 3. Bonus: substring match (if hint is a substring of label, add 2 points)
        # ONLY apply bonus if hint_tokens is not empty to avoid matching on stopwords
        substring_bonus = 0
        if hint_tokens and label_hint.lower() in item_dict["label"].lower():
            substring_bonus = 2

        # 4. Final score
        score = overlap_count + substring_bonus
        scores.append((score, item_dict))

    if not scores:
        return None

    # 3. Rank and check confidence
    scores.sort(key=lambda x: x[0], reverse=True)
    top_score, top_item = scores[0]

    if top_score == 0:
        return None

    if len(scores) > 1:
        second_score = scores[1][0]
        # Require top to be at least 1.5x the second, OR have an absolute gap of 2+
        confidence_threshold_ratio = 1.5
        confidence_threshold_abs = 2

        if (
            top_score < second_score * confidence_threshold_ratio
            and top_score - second_score < confidence_threshold_abs
        ):
            # Ambiguous
            return None

    return top_item
