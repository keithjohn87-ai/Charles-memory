"""Sentiment / tone analysis — local, lightweight.

Uses HuggingFace transformers with `cardiffnlp/twitter-roberta-base-sentiment-latest`
per the Mac Migration Checklist spec. Model is ~500MB, downloaded once on
first call. Returns label (negative / neutral / positive) plus confidence.

Why Charles needs this: the migration's Sunday Test Protocol calls out tone
differentiation (calm vs urgent) and sarcasm detection as core capabilities.
This is the foundation. v0 gives a label; later versions can layer urgency,
sarcasm, and energy-level detection on top.
"""
from __future__ import annotations

import logging

from core.tools import tool

log = logging.getLogger("charles.sentiment")

_MODEL_NAME = "cardiffnlp/twitter-roberta-base-sentiment-latest"
_pipeline = None  # lazy-loaded; first call downloads the model


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        log.info("loading sentiment model %s", _MODEL_NAME)
        from transformers import pipeline
        _pipeline = pipeline("sentiment-analysis", model=_MODEL_NAME, top_k=None)
    return _pipeline


@tool(
    name="analyze_sentiment",
    summary="Classify the sentiment of a text snippet (positive / neutral / negative) with confidence scores. Useful for reading John's mood from his messages or evaluating draft replies before sending.",
    triggers=("sentiment", "tone", "mood", "how does this sound", "is this hostile"),
    schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to analyze. Works best on short messages (1-3 sentences).",
            },
        },
        "required": ["text"],
    },
)
def analyze_sentiment(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "[error] empty text"
    try:
        results = _get_pipeline()(text)
    except Exception as e:  # noqa: BLE001
        return f"[error] {type(e).__name__}: {e}"

    # transformers returns [[{label, score}, ...]] when top_k=None
    items = results[0] if isinstance(results[0], list) else results
    items_sorted = sorted(items, key=lambda d: d["score"], reverse=True)

    top = items_sorted[0]
    breakdown = " | ".join(f"{d['label']} {d['score']:.2f}" for d in items_sorted)
    return f"{top['label'].upper()} ({top['score']:.2f}) — full: {breakdown}"
