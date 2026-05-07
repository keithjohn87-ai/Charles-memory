"""Text → speech → Telegram-compatible voice note.

Two-tier engine:
  1. Primary: mlx-audio Kokoro (neural, Apple Silicon native, voice configurable
     via CHARLES_VOICE — default am_onyx, deeper/authoritative American male).
  2. Fallback: macOS `say` (built-in, robotic, last-resort if Kokoro fails).

Output is always .ogg/Opus — Telegram voice-note format.

Phase 2 (TODO when John provides a reference clip): swap to mlx-audio Chatterbox
or F5-TTS for true voice cloning matching the character spec (Southern Black
male, sophisticated/blue-collar, whiskey-and-cigarettes warmth). Reference clip
should be 10-30 seconds of clean speech.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

log = logging.getLogger("charles.speak")

DEFAULT_VOICE = os.environ.get("CHARLES_VOICE", "am_onyx")
KOKORO_MODEL = os.environ.get("CHARLES_KOKORO_MODEL", "prince-canuma/Kokoro-82M")
SPEAK_RATE = float(os.environ.get("CHARLES_SPEAK_RATE", "1.0"))  # speed multiplier for Kokoro

# macOS-say fallback only
_SAY_VOICE = os.environ.get("CHARLES_SAY_VOICE", "Daniel")
_SAY_RATE = int(os.environ.get("CHARLES_SAY_RATE", "180"))


def speak_to_ogg(text: str, out_dir: str | Path = "/tmp", voice: str | None = None) -> Path:
    """Synthesize speech and return path to a .ogg file Telegram will accept.

    Tries Kokoro first; falls back to macOS `say` on any error.
    Caller is responsible for deleting the file when done.
    """
    if not text or not text.strip():
        raise ValueError("empty text")
    voice = voice or DEFAULT_VOICE
    out_dir = Path(out_dir)
    stem = f"charles_speak_{uuid.uuid4().hex[:8]}"

    # Try the neural path first
    try:
        return _kokoro_to_ogg(text, voice, out_dir, stem)
    except Exception as e:  # noqa: BLE001
        log.warning("kokoro failed (%s: %s) — falling back to macOS say", type(e).__name__, e)
        return _say_to_ogg(text, out_dir, stem)


def _kokoro_to_ogg(text: str, voice: str, out_dir: Path, stem: str) -> Path:
    """Kokoro path: neural .wav → ffmpeg .ogg."""
    from mlx_audio.tts.generate import generate_audio  # late import — heavy

    log.info("kokoro voice=%s rate=%.2f chars=%d", voice, SPEAK_RATE, len(text))

    # mlx-audio writes <prefix>_000.wav in cwd
    cwd_before = Path.cwd()
    try:
        os.chdir(out_dir)
        generate_audio(
            text=text,
            model=KOKORO_MODEL,
            voice=voice,
            speed=SPEAK_RATE,
            file_prefix=stem,
            save=True,
            verbose=False,
        )
    finally:
        os.chdir(cwd_before)

    wav_path = out_dir / f"{stem}_000.wav"
    if not wav_path.exists():
        raise FileNotFoundError(f"kokoro produced no wav: {wav_path}")

    ogg_path = out_dir / f"{stem}.ogg"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(wav_path),
                "-c:a", "libopus", "-b:a", "32k", "-application", "voip",
                str(ogg_path),
            ],
            check=True, capture_output=True, text=True,
        )
    finally:
        if wav_path.exists():
            wav_path.unlink()

    return ogg_path


def _say_to_ogg(text: str, out_dir: Path, stem: str) -> Path:
    """Fallback path: macOS `say` → aiff → ffmpeg .ogg."""
    aiff_path = out_dir / f"{stem}.aiff"
    ogg_path = out_dir / f"{stem}.ogg"

    log.info("say fallback voice=%s rate=%d chars=%d", _SAY_VOICE, _SAY_RATE, len(text))
    try:
        subprocess.run(
            ["say", "-v", _SAY_VOICE, "-r", str(_SAY_RATE), "-o", str(aiff_path), text],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(aiff_path),
                "-c:a", "libopus", "-b:a", "32k", "-application", "voip",
                str(ogg_path),
            ],
            check=True, capture_output=True, text=True,
        )
    finally:
        if aiff_path.exists():
            aiff_path.unlink()
    return ogg_path
