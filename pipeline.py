"""Orchestrator: cache -> MusicBrainz -> LRCLIB -> Lingua -> confidence scoring."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace

from classifiers._http import LookupFailed
from classifiers.detector import Detection, LyricsLanguageDetector
from classifiers.lrclib import LrclibClient, LrclibResult
from classifiers.musicbrainz import MusicBrainzClient, MusicBrainzResult
from config import Settings
from db import Database
from languages import INSTRUMENTAL, UNKNOWN, display
from models import STATUS_AUTO, STATUS_REVIEW, Classification, Track

log = logging.getLogger(__name__)

MUSICBRAINZ_CONFIDENCE = 0.95  # human-curated data
LRCLIB_INSTRUMENTAL_CONFIDENCE = 0.90  # crowd-sourced flag

ResultCallback = Callable[[Track, Classification], None]


def lrclib_key(track: Track) -> str:
    return track.isrc or f"sp:{track.spotify_id}"


# --------------------------------------------------------------------------- decisions
def decide_from_musicbrainz(track: Track, mb: MusicBrainzResult | None) -> Classification | None:
    """Stage 1: a decisive MusicBrainz answer short-circuits everything else."""
    if mb is None:
        return None
    if mb.instrumental:
        return Classification(track.spotify_id, INSTRUMENTAL, MUSICBRAINZ_CONFIDENCE,
                              "musicbrainz", STATUS_AUTO, detail="work language is zxx")
    if len(mb.spoken) == 1:
        return Classification(track.spotify_id, mb.spoken[0], MUSICBRAINZ_CONFIDENCE,
                              "musicbrainz", STATUS_AUTO, detail="work language")
    return None  # several / undetermined languages: let the lyrics decide


def decide_final(
    track: Track,
    mb: MusicBrainzResult | None,
    lrc: LrclibResult | None,
    detection: Detection | None,
    threshold: float,
    errors: Sequence[str] = (),
) -> Classification:
    """Stages 2 + 3: LRCLIB flag, lyrics detection, otherwise Needs_Review."""
    if lrc and lrc.instrumental and not lrc.lyrics:
        return Classification(track.spotify_id, INSTRUMENTAL, LRCLIB_INSTRUMENTAL_CONFIDENCE,
                              "lrclib", STATUS_AUTO, detail="LRCLIB instrumental flag")

    if detection and detection.confidence >= threshold:
        return Classification(
            track.spotify_id, detection.language, detection.confidence, "lingua", STATUS_AUTO,
            detail=f"{detection.words} unique lyric words",
        )

    suggested: str | None = None
    reasons: list[str] = []
    if detection:
        suggested = detection.language
        runner = (f", runner-up {display(detection.runner_up)} {detection.runner_up_confidence:.2f}"
                  if detection.runner_up else "")
        reasons.append(f"low confidence {detection.confidence:.2f} on {detection.words} words{runner}")
    elif lrc is None:
        reasons.append("no lyrics found")
    if mb and mb.spoken:
        suggested = suggested or mb.spoken[0]
        reasons.append("MusicBrainz lists: " + ", ".join(mb.spoken))
    reasons += list(errors)
    source = "error" if errors else ("lingua" if detection else "none")
    return Classification(
        track.spotify_id, UNKNOWN, detection.confidence if detection else 0.0, source,
        STATUS_REVIEW, suggested=suggested, detail="; ".join(reasons),
    )


# ---------------------------------------------------------------------------- pipeline
class Pipeline:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        musicbrainz: MusicBrainzClient,
        lrclib: LrclibClient,
        detector: LyricsLanguageDetector,
        run_id: int | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.mb = musicbrainz
        self.lrclib = lrclib
        self.detector = detector
        self.run_id = run_id

    # -- cached lookups ---------------------------------------------------------
    async def _musicbrainz(self, track: Track) -> MusicBrainzResult | None:
        assert track.isrc
        s = self.settings
        cached = self.db.get_lookup("musicbrainz", track.isrc, s.lookup_hit_ttl_days,
                                    s.lookup_miss_ttl_days)
        if cached is not None:
            return MusicBrainzResult.from_dict(cached.payload) if cached.payload else None
        result = await self.mb.lookup_isrc(track.isrc, track.duration_ms)  # may raise
        self.db.put_lookup("musicbrainz", track.isrc, result.to_dict() if result else None)
        return result

    async def _lrclib(self, track: Track) -> LrclibResult | None:
        s = self.settings
        key = lrclib_key(track)
        cached = self.db.get_lookup("lrclib", key, s.lookup_hit_ttl_days, s.lookup_miss_ttl_days)
        if cached is not None:
            return LrclibResult.from_dict(cached.payload) if cached.payload else None
        result = await self.lrclib.fetch(
            title=track.name, artist=track.primary_artist,
            album=track.album, duration_s=track.duration_s,
        )  # may raise
        self.db.put_lookup("lrclib", key, result.to_dict() if result else None)
        return result

    # -- one track ----------------------------------------------------------------
    async def classify_track(self, track: Track) -> Classification:
        # LRCLIB is fast and unmetered, MusicBrainz is capped at 1 req/s. Start LRCLIB
        # speculatively so its latency hides behind the MusicBrainz queue. If MusicBrainz
        # is decisive the speculative call is cancelled.
        lrc_task = asyncio.create_task(self._lrclib(track))
        try:
            errors: list[str] = []
            mb: MusicBrainzResult | None = None
            if track.isrc:
                try:
                    mb = await self._musicbrainz(track)
                except LookupFailed as exc:
                    errors.append(f"musicbrainz: {exc}")

            early = decide_from_musicbrainz(track, mb)
            if early:
                return early

            lrc: LrclibResult | None = None
            try:
                lrc = await lrc_task
            except LookupFailed as exc:
                errors.append(f"lrclib: {exc}")

            detection = self.detector.detect(lrc.lyrics) if lrc and lrc.lyrics else None
            return decide_final(track, mb, lrc, detection,
                                self.settings.confidence_threshold, errors)
        finally:
            if not lrc_task.done():
                lrc_task.cancel()
            await asyncio.gather(lrc_task, return_exceptions=True)  # reap; never leaks

    # -- many tracks -----------------------------------------------------------------
    async def run(
        self, tracks: Sequence[Track], on_result: ResultCallback | None = None
    ) -> Counter[str]:
        """Classify ``tracks`` with ``settings.workers`` concurrent workers.

        Tracks sharing an ISRC (re-releases, compilations) are classified once and the
        verdict is copied. Results are committed per track, so Ctrl-C loses nothing.
        """
        groups: dict[str, list[Track]] = {}
        for t in tracks:
            groups.setdefault(t.isrc or f"sp:{t.spotify_id}", []).append(t)

        queue: asyncio.Queue[list[Track]] = asyncio.Queue()
        for group in groups.values():
            queue.put_nowait(group)

        stats: Counter[str] = Counter()

        async def worker() -> None:
            while True:
                try:
                    group = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                primary = group[0]
                try:
                    verdict = await self.classify_track(primary)
                except Exception as exc:  # one bad track must not kill the scan
                    log.exception("unexpected failure for %s", primary.label)
                    verdict = Classification(primary.spotify_id, UNKNOWN, 0.0, "error",
                                             STATUS_REVIEW, detail=f"unexpected error: {exc}")
                for t in group:
                    final = replace(verdict, spotify_id=t.spotify_id)
                    self.db.save_classification(final, self.run_id)
                    stats[final.status] += 1
                    if on_result:
                        on_result(t, final)

        n_workers = max(1, min(self.settings.workers, len(groups)))
        await asyncio.gather(*(worker() for _ in range(n_workers)))
        return stats
