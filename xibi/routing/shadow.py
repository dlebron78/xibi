from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ShadowMatch:
    tool: str  # matched tool name
    skill: str  # skill that owns the tool
    phrase: str  # corpus phrase that matched
    score: float  # normalised BM25 confidence 0.0–1.0
    tier: str  # "direct" (>=0.85) | "hint" (0.65–0.85) | "none" (<0.65)
    tool_input: dict[str, str] = field(default_factory=dict)  # populated by extract_tool_input for direct matches


class ShadowMatcher:
    DIRECT_THRESHOLD = 0.85  # skip ReAct, call tool directly
    HINT_THRESHOLD = 0.65  # inject as hint into ReAct context

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.documents: list[tuple[str, str, str]] = []
        self.term_freqs: list[dict[str, int]] = []
        self.doc_lengths: list[int] = []
        self.avg_doc_length: float = 0.0
        self.idf: dict[str, float] = {}
        self.doc_max_scores: list[float] = []

    def _tokenize(self, text: str) -> list[str]:
        return re.findall(r"\b\w+\b", text.lower())

    def build_corpus(self, documents: list[tuple[str, str, str]]) -> None:
        """
        Input: list of (skill_name, tool_name, example_phrase) triples
        """
        self.documents = documents
        self.term_freqs = []
        self.doc_lengths = []

        n = len(documents)
        if n == 0:
            self.avg_doc_length = 0.0
            self.idf = {}
            self.doc_max_scores = []
            return

        term_doc_counts: dict[str, int] = {}
        for _, _, phrase in documents:
            tokens = self._tokenize(phrase)
            self.doc_lengths.append(len(tokens))

            freqs: dict[str, int] = {}
            for token in tokens:
                freqs[token] = freqs.get(token, 0) + 1
            self.term_freqs.append(freqs)

            for token in freqs:
                term_doc_counts[token] = term_doc_counts.get(token, 0) + 1

        self.avg_doc_length = sum(self.doc_lengths) / n

        self.idf = {}
        for term, f_t in term_doc_counts.items():
            # Standard BM25 IDF: log(1 + (N - f_t + 0.5) / (f_t + 0.5))
            self.idf[term] = math.log(1 + (n - f_t + 0.5) / (f_t + 0.5))
            if self.idf[term] < 0:
                self.idf[term] = 0.0

        self.doc_max_scores = []
        for i in range(n):
            doc_tokens = list(self.term_freqs[i].keys())
            self.doc_max_scores.append(self._score_tokens(doc_tokens, i))

    def _score_tokens(self, tokens: list[str], doc_idx: int) -> float:
        score = 0.0
        doc_len = self.doc_lengths[doc_idx]
        freqs = self.term_freqs[doc_idx]
        for token in tokens:
            if token in freqs:
                f_td = freqs[token]
                idf = self.idf.get(token, 0.0)
                # BM25 term score
                num = f_td * (self.k1 + 1)
                den = f_td + self.k1 * (1 - self.b + self.b * (doc_len / self.avg_doc_length))
                score += idf * (num / den)
        return score

    def load_manifests(self, skills_dir: str | Path) -> None:
        skills_path = Path(skills_dir)
        corpus: list[tuple[str, str, str]] = []

        if not skills_path.exists():
            self.build_corpus([])
            return

        for manifest_file in skills_path.glob("*/manifest.json"):
            try:
                with open(manifest_file) as f:
                    manifest = json.load(f)
                    skill_name = manifest.get("name", manifest_file.parent.name)
                    for tool in manifest.get("tools", []):
                        tool_name = tool.get("name")
                        for example in tool.get("examples", []):
                            # strip after -> or —
                            phrase = re.split(r"->|—", example)[0].strip()
                            if phrase:
                                corpus.append((skill_name, tool_name, phrase))
            except Exception as e:
                logging.warning(f"[ShadowMatcher] Error loading {manifest_file}: {e}")

        self.build_corpus(corpus)

    def match(self, query: str) -> ShadowMatch | None:
        if not self.documents:
            return None

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return None

        best_score = -1.0
        best_idx = -1

        for i in range(len(self.documents)):
            score = self._score_tokens(query_tokens, i)
            if score > best_score:
                best_score = score
                best_idx = i

        if best_idx == -1:
            return None

        max_possible = self.doc_max_scores[best_idx]
        # Avoid div by 0 and handle cases where common terms result in 0 or negative score
        if max_possible <= 0.0001:
            return None

        normalized_score = best_score / max_possible

        if normalized_score >= self.DIRECT_THRESHOLD:
            tier = "direct"
        elif normalized_score >= self.HINT_THRESHOLD:
            tier = "hint"
        else:
            return None

        skill, tool, phrase = self.documents[best_idx]
        match = ShadowMatch(tool=tool, skill=skill, phrase=phrase, score=round(normalized_score, 3), tier=tier)

        if tier == "direct":
            match.tool_input = extract_tool_input(query, match)

        return match


def extract_tool_input(query: str, match: ShadowMatch) -> dict[str, str]:
    """
    Given a query and a direct ShadowMatch, extract the most likely tool input parameters
    by parsing the query relative to the matched phrase.
    """
    phrase_tokens = re.findall(r"\b\w+\b", match.phrase.lower())
    remainder = query

    for token in phrase_tokens:
        # Case-insensitive, whole-word only removal
        remainder = re.sub(rf"\b{re.escape(token)}\b", "", remainder, flags=re.IGNORECASE, count=1)

    remainder = remainder.strip().strip(".,?!;:")
    if remainder:
        return {"input": remainder}
    return {}
