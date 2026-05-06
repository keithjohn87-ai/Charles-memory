"""Voice → text via mlx-whisper.

Local, fast, no cloud. First call downloads the model into the HF cache (~75MB
for base.en); subsequent calls reuse it. Runs entirely on Apple Silicon — the
Mac Studio M1 Ultra hits ~30x realtime on base.en.

Usage:
    text = transcribe("/tmp/voice.oga")
"""
from __future__ import annotations

import logging

import mlx_whisper

log = logging.getLogger("charles.transcribe")

# base.en is the sweet spot for English-only voice notes on M1 Ultra.
# Bump to "small.en" if accuracy on accented or noisy audio matters more.
DEFAULT_MODEL = "mlx-community/whisper-base.en-mlx-q4"


def transcribe(audio_path: str, model: str = DEFAULT_MODEL) -> str:
    log.info("transcribing %s with %s", audio_path, model)
    result = mlx_whisper.transcribe(audio_path, path_or_hf_repo=model)
    text = (result.get("text") or "").strip()
    log.info("transcribed %d chars", len(text))
    return text
