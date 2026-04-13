"""Scoring engine for benchmark evaluation."""

import re


def score_response(question, response_text):
    """Score a response against ground truth using keyword matching.

    Returns: (score, explanation)
        score: 0 = wrong, 1 = partial, 2 = correct
    """
    if not response_text or not response_text.strip():
        return 0, "Empty response"

    text_lower = response_text.lower()

    # Check required keywords (all must be present for full score)
    required = question.get("required_keywords", [])
    required_hits = sum(1 for kw in required if kw.lower() in text_lower)

    if required and required_hits == len(required):
        return 2, f"All required keywords found: {required}"

    # Check partial keywords
    partial = question.get("partial_keywords", [])
    partial_hits = sum(1 for kw in partial if kw.lower() in text_lower)

    if partial_hits >= 2:
        return 1, f"Partial match: {partial_hits}/{len(partial)} keywords"

    if required_hits > 0 or partial_hits > 0:
        return 1, f"Weak match: {required_hits} required, {partial_hits} partial"

    return 0, "No relevant keywords found"
