"""Offline checks: all network traffic is served by httpx.MockTransport."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import languages as L
from classifiers._http import LookupFailed, RateLimiter, get_json
from classifiers.detector import LyricsLanguageDetector, clean_lyrics
from classifiers.lrclib import LrclibClient, clean_title
from classifiers.musicbrainz import MusicBrainzClient
from config import Settings
from db import Database
from models import STATUS_AUTO, STATUS_REVIEW, Track
from pipeline import Pipeline, decide_final
from spotify_client import item_to_track

GERMAN = "\n".join([
    "Ich geh allein durch die Stadt am Abend",
    "Der Regen fällt auf die leeren Straßen",
    "Ich denke an dich und an die Zeit die wir hatten",
    "Und niemand weiß wohin ich gehen werde",
    "Die Lichter brennen noch in den alten Häusern",
])
ENGLISH = "\n".join([
    "I walk alone through the city tonight",
    "The rain is falling on the empty streets",
    "I think of you and all the time we had",
    "And nobody knows where I am going now",
    "The lights are still burning in the old houses",
])


def make_settings(**over) -> Settings:
    base = dict(
        spotify_client_id="x", spotify_client_secret="y",
        spotify_redirect_uri="http://127.0.0.1:8888/callback", contact="t@example.com",
        db_path=":memory:", token_cache_path=".t", confidence_threshold=0.75, workers=4,
        mb_interval=0.01, lrclib_concurrency=3, http_timeout=2.0,
        lookup_hit_ttl_days=365, lookup_miss_ttl_days=30, min_words=15,
    )
    base.update(over)
    return Settings(**base)


def track(sid: str, isrc: str | None, name="Song", artist="Artist") -> Track:
    return Track(sid, name, artist, artist, "Album", 200_000, isrc, "2026-01-01T00:00:00Z")


# ---------------------------------------------------------------------- pure helpers
def test_language_resolution():
    assert L.resolve("German") == "deu" and L.resolve("de") == "deu"
    assert L.resolve("Swiss German") == "gsw" and L.resolve("inst") == L.INSTRUMENTAL
    assert L.resolve("klingon") is None
    assert L.matches("gsw", "deu") and not L.matches("gsw", "deu", include_dialects=False)


def test_clean_title():
    assert clean_title("Song - 2011 Remaster") == "Song"
    assert clean_title("Song (feat. Someone)") == "Song"


def test_clean_lyrics_dedupes_chorus():
    assert clean_lyrics("[Chorus]\nla la hey\nLa la hey!\nverse").splitlines() == ["la la hey", "verse"]


def test_item_to_track_skips_local_and_missing_ids():
    assert item_to_track({"track": None}) is None
    assert item_to_track({"track": {"id": None, "is_local": True}}) is None
    t = item_to_track({"added_at": "x", "track": {
        "id": "abc", "name": "N", "artists": [{"name": "Tyler, The Creator"}],
        "album": {"name": "A"}, "duration_ms": 1000, "external_ids": {"isrc": "us-abc-12-34567"}}})
    assert t and t.primary_artist == "Tyler, The Creator" and t.isrc == "USABC1234567"


def test_rate_limiter_enforces_spacing():
    async def go():
        lim = RateLimiter(0.05)
        t0 = time.monotonic()
        await asyncio.gather(*(lim.wait() for _ in range(4)))
        return time.monotonic() - t0
    assert asyncio.run(go()) >= 0.15


def test_get_json_retries_then_fails_cleanly():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            with pytest.raises(LookupFailed):
                await get_json(c, "https://x.test", max_attempts=2, label="t")
    asyncio.run(go())
    assert calls["n"] == 2


# ---------------------------------------------------------------------- decision logic
def test_low_confidence_goes_to_review_with_suggestion():
    det = LyricsLanguageDetector(min_words=15).detect("la la la oh oh")
    verdict = decide_final(track("1", None), None, None, det, 0.75)
    assert verdict.status == STATUS_REVIEW and verdict.confidence < 0.75


def test_error_marks_source_error_so_scan_retries():
    verdict = decide_final(track("1", None), None, None, None, 0.75, ["lrclib: boom"])
    assert verdict.status == STATUS_REVIEW and verdict.source == "error"


def test_lingua_distinguishes_german_and_english():
    d = LyricsLanguageDetector(min_words=15)
    assert d.detect(GERMAN).language == "deu"
    assert d.detect(ENGLISH).language == "eng"


# ---------------------------------------------------------------------- end to end
def test_pipeline_end_to_end():
    mb_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "musicbrainz.org/ws/2/isrc/" in url:
            isrc = req.url.path.rsplit("/", 1)[-1]
            mb_calls.append(isrc)
            if isrc == "INSTR0000001":  # Work with language zxx
                return httpx.Response(200, json={"recordings": [{"id": "r1", "length": 200000, "relations": [
                    {"target-type": "work", "work": {"id": "w1", "title": "Veridis", "languages": ["zxx"], "language": "zxx"}}]}]})
            if isrc == "SWISS0000001":
                return httpx.Response(200, json={"recordings": [{"id": "r2", "relations": [
                    {"target-type": "work", "work": {"id": "w2", "title": "Losloh", "languages": ["gsw"]}}]}]})
            return httpx.Response(404)
        if "lrclib.net/api/get" in url:
            name = req.url.params["track_name"]
            if name == "German":
                return httpx.Response(200, json={"instrumental": False, "plainLyrics": GERMAN})
            if name == "Instr":
                return httpx.Response(200, json={"instrumental": True, "plainLyrics": None})
            return httpx.Response(404)
        if "lrclib.net/api/search" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(500)

    tracks = [
        track("t1", "INSTR0000001", "Veridis Quo", "Daft Punk"),
        track("t2", "INSTR0000001", "Veridis Quo (Remaster)", "Daft Punk"),  # same ISRC -> one lookup
        track("t3", "SWISS0000001", "Losloh", "Riana"),
        track("t4", "DEUTSCH00001", "German"),
        track("t5", "NOTHING00001", "Mystery"),
        track("t6", None, "Instr"),
    ]

    async def go():
        db = Database(":memory:")
        s = make_settings()
        for t in tracks:
            db.upsert_track(t, db.start_run("t"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            p = Pipeline(db, s, MusicBrainzClient(client, min_interval=0.01),
                         LrclibClient(client, concurrency=3), LyricsLanguageDetector(15), run_id=1)
            await p.run(tracks)
            first = {r["spotify_id"]: r for r in db.effective_rows()}
            calls_after_first = len(mb_calls)
            await p.run(tracks)  # second pass: everything must come from cache
            return first, calls_after_first, len(mb_calls)

    rows, first_calls, second_calls = asyncio.run(go())
    assert rows["t1"]["language"] == L.INSTRUMENTAL and rows["t2"]["language"] == L.INSTRUMENTAL
    assert rows["t3"]["language"] == "gsw"
    assert rows["t4"]["language"] == "deu" and rows["t4"]["status"] == STATUS_AUTO
    assert rows["t5"]["status"] == STATUS_REVIEW
    assert rows["t6"]["language"] == L.INSTRUMENTAL
    assert mb_calls.count("INSTR0000001") == 1  # ISRC de-duplication
    assert second_calls == first_calls  # cache works, incl. negative results


def test_override_survives_reclassification():
    db = Database(":memory:")
    run = db.start_run("t")
    t = track("t1", None)
    db.upsert_track(t, run)
    db.set_override("t1", "fra")
    assert db.tracks_to_classify(include_needs_review=True) == []
    assert db.effective_rows()[0]["language"] == "fra"
