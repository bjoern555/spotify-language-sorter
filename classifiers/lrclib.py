"""LRCLIB client: open lyrics database with a native `instrumental` flag."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from classifiers._http import get_json

log = logging.getLogger(__name__)
BASE_URL = "https://lrclib.net/api"
DURATION_TOLERANCE_S = 3

_SUFFIX = re.compile(
    r"\s*[-–—]\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|live|mono|stereo|single version|"
    r"radio edit|album version|deluxe|bonus track|acoustic version).*$",
    re.IGNORECASE,
)
_FEAT = re.compile(r"\s*[\(\[]\s*(?:feat|ft|with)\.?\s[^\)\]]*[\)\]]", re.IGNORECASE)
_TIMESTAMP = re.compile(r"^\s*\[\d+:\d+(?:[.:]\d+)?\]\s*", re.MULTILINE)


def clean_title(title: str) -> str:
    """Strip '- 2011 Remaster' / '(feat. X)' noise that breaks exact matching."""
    out = _FEAT.sub("", title)
    out = _SUFFIX.sub("", out)
    return out.strip() or title.strip()


@dataclass(frozen=True, slots=True)
class LrclibResult:
    instrumental: bool
    lyrics: str | None
    via: str  # "get" | "search"

    def to_dict(self) -> dict[str, Any]:
        return {"instrumental": self.instrumental, "lyrics": self.lyrics, "via": self.via}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LrclibResult":
        return cls(bool(d["instrumental"]), d.get("lyrics"), d.get("via", "get"))


def _parse_entry(entry: dict[str, Any], via: str) -> LrclibResult | None:
    instrumental = bool(entry.get("instrumental"))
    lyrics = (entry.get("plainLyrics") or "").strip()
    if not lyrics and entry.get("syncedLyrics"):
        lyrics = _TIMESTAMP.sub("", entry["syncedLyrics"]).strip()
    if not instrumental and not lyrics:
        return None  # entry exists but carries no usable information
    return LrclibResult(instrumental=instrumental, lyrics=lyrics or None, via=via)


class LrclibClient:
    def __init__(self, client: httpx.AsyncClient, *, concurrency: int = 5) -> None:
        self._client = client
        self._gate = asyncio.Semaphore(concurrency)

    async def fetch(
        self, *, title: str, artist: str, album: str | None, duration_s: int | None
    ) -> LrclibResult | None:
        """Exact signature lookup first, then a fuzzy search fallback.

        Returns None when LRCLIB has nothing usable. Raises LookupFailed on network trouble.
        """
        name = clean_title(title)
        params: dict[str, Any] = {"track_name": name, "artist_name": artist}
        if album:
            params["album_name"] = album
        if duration_s:
            params["duration"] = duration_s

        # /api/get needs all four fields; without album/duration go straight to search.
        if album and duration_s:
            entry = await get_json(
                self._client, f"{BASE_URL}/get", params=params,
                gate=self._gate, label=f"lrclib get {artist} - {name}",
            )
            if isinstance(entry, dict):
                parsed = _parse_entry(entry, "get")
                if parsed:
                    return parsed

        results = await get_json(
            self._client, f"{BASE_URL}/search",
            params={"track_name": name, "artist_name": artist},
            gate=self._gate, label=f"lrclib search {artist} - {name}",
        )
        if not isinstance(results, list):
            return None
        candidates = [
            r for r in results
            if isinstance(r, dict)
            and (duration_s is None or r.get("duration") is None
                 or abs(float(r["duration"]) - duration_s) <= DURATION_TOLERANCE_S)
        ]
        if duration_s:
            candidates.sort(key=lambda r: abs(float(r.get("duration") or duration_s) - duration_s))
        for entry in candidates[:5]:
            parsed = _parse_entry(entry, "search")
            if parsed:
                return parsed
        return None
