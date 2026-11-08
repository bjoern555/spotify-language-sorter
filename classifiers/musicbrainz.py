"""MusicBrainz client: ISRC -> recording -> work -> language."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from classifiers._http import RateLimiter, get_json
from languages import (
    MB_MULTIPLE,
    MB_NO_LINGUISTIC_CONTENT,
    MB_UNDETERMINED,
    normalize,
)

log = logging.getLogger(__name__)
BASE_URL = "https://musicbrainz.org/ws/2"
_NON_LANGUAGES = {MB_NO_LINGUISTIC_CONTENT, MB_MULTIPLE, MB_UNDETERMINED}


@dataclass(frozen=True, slots=True)
class MusicBrainzResult:
    recording_id: str
    languages: tuple[str, ...]  # normalised ISO 639-3, may include zxx / mul / und
    work_titles: tuple[str, ...] = ()

    @property
    def instrumental(self) -> bool:
        return bool(self.languages) and set(self.languages) == {MB_NO_LINGUISTIC_CONTENT}

    @property
    def spoken(self) -> tuple[str, ...]:
        """Real languages only (no zxx / mul / und)."""
        return tuple(c for c in self.languages if c not in _NON_LANGUAGES)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recording_id": self.recording_id,
            "languages": list(self.languages),
            "work_titles": list(self.work_titles),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MusicBrainzResult":
        return cls(d["recording_id"], tuple(d["languages"]), tuple(d.get("work_titles", ())))


class MusicBrainzClient:
    def __init__(
        self, client: httpx.AsyncClient, *, min_interval: float = 1.1
    ) -> None:
        # The httpx client must already carry the required User-Agent header.
        self._client = client
        self._limiter = RateLimiter(min_interval)

    async def lookup_isrc(
        self, isrc: str, duration_ms: int | None = None
    ) -> MusicBrainzResult | None:
        """Return the language data of the Work linked to this ISRC, or None.

        Uses ``/isrc/<ISRC>?inc=work-rels`` - one request returns the recordings *and*
        their work relationships. (The search endpoint ``/recording?query=isrc:...``
        ignores ``inc``, so it would need a second call per recording.)

        Raises LookupFailed on network/server trouble.
        """
        data = await get_json(
            self._client,
            f"{BASE_URL}/isrc/{isrc}",
            params={"inc": "work-rels", "fmt": "json"},
            limiter=self._limiter,
            label=f"musicbrainz isrc {isrc}",
        )
        if not data:
            return None
        recordings: list[dict[str, Any]] = list(data.get("recordings") or [])
        if duration_ms:
            recordings.sort(key=lambda r: abs((r.get("length") or duration_ms) - duration_ms))

        for rec in recordings[:3]:
            works = [
                rel["work"]
                for rel in rec.get("relations", [])
                if rel.get("target-type") == "work" and rel.get("work")
            ]
            if not works:
                continue
            codes: list[str] = []
            for work in works:
                codes.extend(await self._work_languages(work))
            codes = list(dict.fromkeys(codes))  # de-duplicate, keep order
            if codes:
                return MusicBrainzResult(
                    recording_id=rec.get("id", ""),
                    languages=tuple(codes),
                    work_titles=tuple(w.get("title", "") for w in works),
                )
        return None

    async def _work_languages(self, work: dict[str, Any]) -> list[str]:
        raw: list[str] = list(work.get("languages") or [])
        if not raw and work.get("language"):
            raw = [work["language"]]
        # Relationship stubs normally carry the language keys. If neither key is present
        # at all, fetch the work itself (one extra rate-limited call).
        if not raw and "languages" not in work and "language" not in work and work.get("id"):
            full = await get_json(
                self._client,
                f"{BASE_URL}/work/{work['id']}",
                params={"fmt": "json"},
                limiter=self._limiter,
                label=f"musicbrainz work {work['id']}",
            )
            if full:
                raw = list(full.get("languages") or [])
                if not raw and full.get("language"):
                    raw = [full["language"]]
        return [c for c in (normalize(x) for x in raw) if c]
