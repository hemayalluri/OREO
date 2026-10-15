"""
Speech-to-text, run locally with faster-whisper (free, offline, audio never leaves the Mac).

Text-to-speech happens in the face page with the device's built-in voices
(free, offline on macOS and iPadOS). Piper can replace it later for a custom voice.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("oreo.speech")


class SpeechUnavailable(Exception):
    pass


class WhisperSTT:
    """Lazy-loads the Whisper model on first use (the first load downloads it, ~150 MB for base.en)."""

    def __init__(self, model: str = "base.en", language: str = "en", compute_type: str = "int8"):
        self.model_name = model
        self.language = language
        self.compute_type = compute_type
        self._model: Any = None
        self._lock = asyncio.Lock()

    def _load(self) -> Any:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise SpeechUnavailable("faster-whisper is not installed (pip install -r requirements.txt)") from exc
            log.info("Loading Whisper model %s (first run downloads it)", self.model_name)
            self._model = WhisperModel(self.model_name, device="cpu", compute_type=self.compute_type)
        return self._model

    def _transcribe_file(self, path: str) -> str:
        model = self._load()
        segments, _info = model.transcribe(path, language=self.language, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, audio: bytes, suffix: str = ".webm") -> str:
        """Turn recorded audio (webm/opus from Chrome, mp4/aac from Safari, or wav) into text."""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio)
            path = f.name
        try:
            async with self._lock:  # one transcription at a time keeps memory use low on 8 GB
                return await asyncio.to_thread(self._transcribe_file, path)
        finally:
            Path(path).unlink(missing_ok=True)
