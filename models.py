"""Plain data objects shared across modules."""
from __future__ import annotations

from dataclasses import dataclass

STATUS_AUTO = "auto"
STATUS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class Track:
    spotify_id: str
    name: str
    primary_artist: str
    artists: str
    album: str | None
    duration_ms: int | None
    isrc: str | None = None
    added_at: str | None = None

    @property
    def duration_s(self) -> int | None:
        return round(self.duration_ms / 1000) if self.duration_ms else None

    @property
    def label(self) -> str:
        return f"{self.artists} – {self.name}"


@dataclass(frozen=True, slots=True)
class Classification:
    spotify_id: str
    language: str  # ISO 639-3, "instrumental" or "unknown"
    confidence: float
    source: str  # musicbrainz | lrclib | lingua | none | error
    status: str  # STATUS_AUTO | STATUS_REVIEW
    suggested: str | None = None
    detail: str = ""
