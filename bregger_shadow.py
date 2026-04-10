import collections
import json
import math
import re
from pathlib import Path


def tokenize(text: str) -> list[str]:
    """Basic lowercased alphanumeric tokenizer."""
    return re.findall(r"\b\w+\b", text.lower())


class ShadowMatcher:
    """Pure Python BM25 intent matcher for Shadow Tier 2 routing."""

    def __init__(self, k1: float = 1.5, b: float = 0.75, threshold: float = 0.65):
        self.k1 = k1
        self.b = b
        self.threshold = threshold

        # Corpus state
        self.documents: list[tuple[str, str, str]] = []  # list of (skill, tool, original_phrase)
        self.doc_lengths: list[int] = []
        self.avg_doc_length: float = 0.0
        self.term_freqs: list[collections.Counter] = []  # term frequencies per document
        self.idf: dict[str, float] = {}
        self.doc_max_scores: list[float] = []

    def load_manifests(self, skills_dir: str):
        """Build the corpus from all 'examples' arrays in skill manifests."""
        skills_path = Path(skills_dir)
        corpus = []

        if not skills_path.exists():
            return

        for manifest_file in skills_path.glob("*/manifest.json"):
            try:
                with open(manifest_file) as f:
                    manifest = json.load(f)
                    skill_name = manifest.get("name", manifest_file.parent.name)
                    for tool in manifest.get("tools", []):
                        tool_name = tool.get("name")
                        for example in tool.get("examples", []):
                            # In email/search manifests, sometimes examples have comments after "->"
                            phrase = example.split("->")[0].split("—")[0].strip()
                            if phrase:
                                corpus.append((skill_name, tool_name, phrase))
            except Exception as e:
                print(f"⚠️ [ShadowMatcher] Error loading {manifest_file}: {e}")

        self.build_corpus(corpus)

    def build_corpus(self, documents: list[tuple[str, str, str]]):
        """Compute BM25 IDF and term frequency structures."""
        self.documents = documents
        self.doc_lengths = []
        self.term_freqs = []
        doc_count = len(documents)

        if doc_count == 0:
            return

        term_doc_counts = collections.Counter()

        for _, _, phrase in documents:
            tokens = tokenize(phrase)
            self.doc_lengths.append(len(tokens))
            freq = collections.Counter(tokens)
            self.term_freqs.append(freq)
            # count documents containing term
            for term in freq:
                term_doc_counts[term] += 1

        self.avg_doc_length = sum(self.doc_lengths) / doc_count

        # Compute IDF
        self.idf = {}
        for term, f_t in term_doc_counts.items():
            # Standard BM25 IDF formula
            self.idf[term] = math.log(1 + (doc_count - f_t + 0.5) / (f_t + 0.5))

        # Precompute max possible score per document (document against itself)
        self.doc_max_scores = []
        for idx in range(doc_count):
            doc_len = self.doc_lengths[idx]
            freqs = self.term_freqs[idx]
            max_score = 0.0
            for term, f_td in freqs.items():
                idf = self.idf.get(term, 0.0)
                num = f_td * (self.k1 + 1)
                den = f_td + self.k1 * (1 - self.b + self.b * (doc_len / self.avg_doc_length))
                max_score += idf * (num / den)
            self.doc_max_scores.append(max_score)

    def match(self, query: str) -> dict | None:
        """Score the query against the corpus and return the top match above threshold."""
        if not self.documents:
            return None

        query_tokens = tokenize(query)
        best_score = 0.0
        best_match_idx = -1

        # Calculate BM25 score for each document
        for idx in range(len(self.documents)):
            score = 0.0
            doc_len = self.doc_lengths[idx]
            freqs = self.term_freqs[idx]

            for term in query_tokens:
                if term in freqs:
                    f_td = freqs[term]
                    idf = self.idf.get(term, 0.0)
                    # BM25 term score
                    num = f_td * (self.k1 + 1)
                    den = f_td + self.k1 * (1 - self.b + self.b * (doc_len / self.avg_doc_length))
                    score += idf * (num / den)

            if score > best_score:
                best_score = score
                best_match_idx = idx

        if best_match_idx == -1:
            return None

        # Normalize against the best matched document's theoretical max score
        max_possible_score = self.doc_max_scores[best_match_idx]

        # Avoid div by 0
        if max_possible_score <= 0.001:
            return None

        confidence = best_score / max_possible_score

        if confidence >= self.threshold and best_match_idx != -1:
            skill, tool, phrase = self.documents[best_match_idx]
            return {"predicted_tool": tool, "score": round(confidence, 3), "phrase_matched": phrase}

        return None
