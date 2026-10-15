"""
Spoken control phrases, matched on the (free, local) Whisper transcript.

- Wake: "Hey Oreo" (Whisper often spells the name a little differently, so a few
  near-misses count too). Anything said after it is treated as the first question.
- Sleep: "okay bye", "thank you bye", "bye Oreo", "go to sleep", ...
- Noise: words Whisper tends to "hear" in background noise; ignored, never sent to the brain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Whisper's usual near-misses for "Oreo".
_NAMES = ("oreo", "oreos", "orio", "oriel", "oreal", "orea", "oriole", "oro", "rio", "ariel", "aerial", "ario", "orayo", "aurea")
_NAME = "(?:" + "|".join(_NAMES) + ")"
WAKE = re.compile(rf"\b(?:hey|hi|hello|okay|ok|yo|a|hay)[\s,!.]*{_NAME}\b[\s,!.?]*", re.I)

# Words allowed before or after "bye" in a goodbye ("okay bye", "thank you so much, bye Oreo").
_BYE_EXTRAS = {"okay", "ok", "alright", "all", "right", "thank", "thanks", "you", "so", "much",
               "a", "lot", "then", "now", "for", "bye", "good", "goodbye", "see", "ya", "later", "nothing",
               "it's", "its", "that's", "it", "fine", "cool", "great", "perfect", "awesome", "got", "today",
               "have", "nice", "day", "night", "next", "time", "take", "care", "catch", "soon", *_NAMES}
_SLEEP_COMMANDS = re.compile(
    rf"^(?:(?:okay|ok|{_NAME})\s+)*(?:go to sleep|stop listening|that'?s all|that is all|you can sleep now)(?:\s+{_NAME})?$")
NOISE = re.compile(
    r"^(?:you|thank you|thanks|thanks for watching|thank you for watching|okay|ok|hmm|um|uh|so|oh|bye)?$")


@dataclass
class Heard:
    intent: str          # "wake" | "sleep" | "noise" | "question" | "ignore"
    text: str            # what was heard
    question: str = ""   # the question to answer (for "question", or "wake" with a question after it)


def _normalize(text: str) -> str:
    text = text.lower().replace("good bye", "goodbye").replace("bye-bye", "bye bye")
    return " ".join(re.sub(r"[^a-z' ]+", " ", text).split())


def is_goodbye(text: str) -> bool:
    norm = _normalize(text)
    if _SLEEP_COMMANDS.match(norm):
        return True
    words = norm.split()
    # A short phrase that contains bye/goodbye and nothing but polite extras, but not a lone
    # "bye" (Whisper sometimes invents that one from noise).
    return (0 < len(words) <= 12 and any(w in ("bye", "goodbye") for w in words)
            and all(w in _BYE_EXTRAS for w in words) and words != ["bye"])


def classify(text: str, mode: str = "question") -> Heard:
    """mode="wake": Oreo is asleep and only listens for "Hey Oreo". mode="question": Oreo is awake."""
    text = (text or "").strip()
    if mode == "wake":
        match = WAKE.search(text)
        if not match:
            return Heard("ignore", text)
        rest = text[match.end():].strip()
        question = rest if len(_normalize(rest).split()) >= 2 else ""
        return Heard("wake", text, question)
    if NOISE.match(_normalize(text)):
        return Heard("noise", text)
    if is_goodbye(text):
        return Heard("sleep", text)
    return Heard("question", text, text)
