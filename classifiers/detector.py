"""Lyrics language detection with lingua-language-detector."""
from __future__ import annotations

import re
from dataclasses import dataclass

_SECTION_HEADER = re.compile(r"\[[^\]]*\]")  # [Chorus], [Verse 1], [00:12.30]
_NON_LETTER = re.compile(r"[^\w\s']", re.UNICODE)


@dataclass(frozen=True, slots=True)
class Detection:
    language: str  # ISO 639-3
    confidence: float
    runner_up: str | None
    runner_up_confidence: float
    words: int


def clean_lyrics(raw: str) -> str:
    """Drop section headers/punctuation and repeated lines so a looped chorus cannot
    dominate the statistics."""
    seen: set[str] = set()
    kept: list[str] = []
    for line in raw.splitlines():
        line = _SECTION_HEADER.sub(" ", line)
        norm = _NON_LETTER.sub(" ", line).strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        kept.append(norm)
    return "\n".join(kept)


class LyricsLanguageDetector:
    """Thin wrapper around lingua.

    Notes
    -----
    * Lingua returns a probability distribution over *all* supported languages, so the
      top value is directly usable as a confidence.
    * Very short lyrics are down-weighted linearly below ``min_words`` - three words of
      "la la la" must never produce a confident verdict.
    * Lingua has no Swiss-German model; such lyrics come back as German. Use MusicBrainz
      data or a manual override (``review``) to label them ``gsw``.
    """

    def __init__(self, min_words: int = 15) -> None:
        from lingua import LanguageDetectorBuilder  # imported lazily: heavy

        self._min_words = max(1, min_words)
        self._detector = LanguageDetectorBuilder.from_all_languages().build()

    def detect(self, lyrics: str) -> Detection | None:
        text = clean_lyrics(lyrics)
        words = len(text.split())
        if words == 0:
            return None
        values = self._detector.compute_language_confidence_values(text)
        if not values:
            return None
        top = values[0]
        second = values[1] if len(values) > 1 else None
        confidence = float(top.value)
        if words < self._min_words:
            confidence *= words / self._min_words
        return Detection(
            language=top.language.iso_code_639_3.name.lower(),
            confidence=confidence,
            runner_up=second.language.iso_code_639_3.name.lower() if second else None,
            runner_up_confidence=float(second.value) if second else 0.0,
            words=words,
        )
