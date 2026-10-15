"""
Speech-to-text, run locally with faster-whisper (free, offline, audio never leaves the Mac).

Text-to-speech: on a Mac, MacSayTTS renders answers with the system voices (including
the downloaded Premium/Enhanced ones that browsers often don't expose) and the face page
plays the audio. Elsewhere the page falls back to the browser's own voices. Both are
free and offline. Piper can replace this later for a custom voice.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
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

    def _transcribe_file(self, path: str, hotwords: str | None = None) -> str:
        model = self._load()
        segments, _info = model.transcribe(path, language=self.language, beam_size=1, vad_filter=True,
                                           hotwords=hotwords)
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, audio: bytes, suffix: str = ".webm", hotwords: str | None = None) -> str:
        """Turn recorded audio (webm/opus from Chrome, mp4/aac from Safari, or wav) into text."""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio)
            path = f.name
        try:
            async with self._lock:  # one transcription at a time keeps memory use low on 8 GB
                return await asyncio.to_thread(self._transcribe_file, path, hotwords)
        finally:
            Path(path).unlink(missing_ok=True)


class MacSayTTS:
    """Speaks with the macOS `say` command and returns WAV audio for the face page to play."""

    def __init__(self, preferred: str = "", rate: float = 1.0):
        self.rate_wpm = int(round(180 * (rate or 1.0)))  # `say` speaks ~180 words/minute by default
        self.voice = self._pick_voice([v.strip() for v in preferred.split(",") if v.strip()])

    @staticmethod
    def available() -> bool:
        return shutil.which("say") is not None

    @staticmethod
    def _installed_voices() -> list[str]:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
        # Lines look like "Ava (Premium)       en_US    # Hello! My name is Ava."
        return [m.group(1).strip() for m in re.finditer(r"^(.+?)\s{2,}[a-z]{2}_[A-Z]{2}", out, re.M)]

    def _pick_voice(self, preferred: list[str]) -> str | None:
        if not self.available():
            return None
        installed = self._installed_voices()
        for want in preferred:
            for name in installed:
                if name.lower() == want.lower():
                    log.info("Oreo's voice: %s", name)
                    return name
        return None  # nothing from the list: let the browser choose

    async def synthesize(self, text: str) -> bytes:
        if not self.voice:
            raise SpeechUnavailable("No preferred Mac voice is installed.")
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "text.txt", Path(tmp) / "speech.wav"
            src.write_text(text)
            proc = await asyncio.create_subprocess_exec(
                "say", "-v", self.voice, "-r", str(self.rate_wpm), "-f", str(src), "-o", str(out),
                "--file-format=WAVE", "--data-format=LEI16@24000",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0 or not out.exists():
                raise SpeechUnavailable(f"say failed: {err.decode(errors='replace').strip()}")
            return out.read_bytes()
