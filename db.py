"""SQLite persistence: tracks, runs, cached external lookups, classifications, overrides."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models import Classification, Track

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    mode          TEXT NOT NULL,
    tracks_seen   INTEGER NOT NULL DEFAULT 0,
    tracks_done   INTEGER NOT NULL DEFAULT 0
);

-- One row per Spotify track in the Liked Songs library (metadata only).
CREATE TABLE IF NOT EXISTS tracks (
    spotify_id     TEXT PRIMARY KEY,
    isrc           TEXT,
    name           TEXT NOT NULL,
    primary_artist TEXT NOT NULL,
    artists        TEXT NOT NULL,
    album          TEXT,
    duration_ms    INTEGER,
    added_at       TEXT,
    last_seen_run  INTEGER REFERENCES runs(id),
    removed        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tracks_isrc ON tracks(isrc);

-- Raw results of external calls, keyed by (source, key). Negative results are cached
-- too (status='miss') so a 1,000-song rescan costs zero network calls.
CREATE TABLE IF NOT EXISTS lookups (
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('hit', 'miss')),
    payload    TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, key)
);

-- What the pipeline decided. Overwritten on every (re)classification.
CREATE TABLE IF NOT EXISTS classifications (
    spotify_id    TEXT PRIMARY KEY REFERENCES tracks(spotify_id) ON DELETE CASCADE,
    language      TEXT NOT NULL,
    confidence    REAL NOT NULL,
    source        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('auto', 'needs_review')),
    suggested     TEXT,
    detail        TEXT NOT NULL DEFAULT '',
    run_id        INTEGER REFERENCES runs(id),
    classified_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Human decisions. Kept in their own table so a rescan can never clobber them.
CREATE TABLE IF NOT EXISTS overrides (
    spotify_id TEXT PRIMARY KEY REFERENCES tracks(spotify_id) ON DELETE CASCADE,
    language   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Remember which Spotify playlist belongs to which logical name, so export syncs in place.
CREATE TABLE IF NOT EXISTS playlists (
    name               TEXT PRIMARY KEY,
    spotify_playlist_id TEXT NOT NULL,
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

DROP VIEW IF EXISTS effective_classification;
CREATE VIEW effective_classification AS
SELECT t.spotify_id, t.isrc, t.name, t.primary_artist, t.artists, t.album,
       t.duration_ms, t.added_at,
       COALESCE(o.language, c.language)                       AS language,
       COALESCE(CASE WHEN o.language IS NOT NULL THEN 'override' ELSE c.status END,
                'pending')                                    AS status,
       c.confidence, c.source, c.suggested, c.detail
FROM tracks t
LEFT JOIN classifications c ON c.spotify_id = t.spotify_id
LEFT JOIN overrides o       ON o.spotify_id = t.spotify_id
WHERE t.removed = 0;
"""


@dataclass(frozen=True, slots=True)
class CachedLookup:
    hit: bool
    payload: dict[str, Any] | None


def _row_to_track(row: sqlite3.Row) -> Track:
    return Track(
        spotify_id=row["spotify_id"],
        name=row["name"],
        primary_artist=row["primary_artist"],
        artists=row["artists"],
        album=row["album"],
        duration_ms=row["duration_ms"],
        isrc=row["isrc"],
        added_at=row["added_at"],
    )


class Database:
    def __init__(self, path: Path | str = "cache.db") -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- runs -------------------------------------------------------------
    def start_run(self, mode: str) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO runs(mode) VALUES (?)", (mode,))
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, seen: int, done: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at=datetime('now'), tracks_seen=?, tracks_done=? "
                "WHERE id=?",
                (seen, done, run_id),
            )

    # ---- tracks -----------------------------------------------------------
    def upsert_track(self, t: Track, run_id: int) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO tracks(spotify_id, isrc, name, primary_artist, artists, album,
                                   duration_ms, added_at, last_seen_run, removed)
                VALUES (?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(spotify_id) DO UPDATE SET
                    isrc=excluded.isrc, name=excluded.name,
                    primary_artist=excluded.primary_artist, artists=excluded.artists,
                    album=excluded.album, duration_ms=excluded.duration_ms,
                    added_at=excluded.added_at, last_seen_run=excluded.last_seen_run,
                    removed=0
                """,
                (t.spotify_id, t.isrc, t.name, t.primary_artist, t.artists, t.album,
                 t.duration_ms, t.added_at, run_id),
            )

    def known_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT spotify_id FROM tracks WHERE removed=0").fetchall()
        return {r["spotify_id"] for r in rows}

    def mark_unseen_as_removed(self, run_id: int) -> int:
        """After a *complete* scan: tracks the user un-liked drop out of every report."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE tracks SET removed=1 WHERE removed=0 AND last_seen_run IS NOT ?",
                (run_id,),
            )
        return cur.rowcount

    def tracks_to_classify(self, include_needs_review: bool = False) -> list[Track]:
        rows = self.conn.execute(
            """
            SELECT t.* FROM tracks t
            LEFT JOIN classifications c ON c.spotify_id = t.spotify_id
            LEFT JOIN overrides o       ON o.spotify_id = t.spotify_id
            WHERE t.removed = 0 AND o.spotify_id IS NULL
              AND (c.spotify_id IS NULL
                   OR c.source = 'error'
                   OR (? AND c.status = 'needs_review'))
            ORDER BY t.added_at DESC
            """,
            (int(include_needs_review),),
        ).fetchall()
        return [_row_to_track(r) for r in rows]

    def count_without_isrc(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM tracks WHERE removed=0 AND (isrc IS NULL OR isrc='')"
        ).fetchone()
        return int(row["n"])

    # ---- classifications & overrides -----------------------------------------
    def save_classification(self, c: Classification, run_id: int | None) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO classifications(spotify_id, language, confidence, source, status,
                                            suggested, detail, run_id, classified_at)
                VALUES (?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(spotify_id) DO UPDATE SET
                    language=excluded.language, confidence=excluded.confidence,
                    source=excluded.source, status=excluded.status,
                    suggested=excluded.suggested, detail=excluded.detail,
                    run_id=excluded.run_id, classified_at=excluded.classified_at
                """,
                (c.spotify_id, c.language, c.confidence, c.source, c.status,
                 c.suggested, c.detail, run_id),
            )

    def set_override(self, spotify_id: str, language: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO overrides(spotify_id, language) VALUES (?,?) "
                "ON CONFLICT(spotify_id) DO UPDATE SET language=excluded.language, "
                "created_at=datetime('now')",
                (spotify_id, language),
            )

    def clear_override(self, spotify_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM overrides WHERE spotify_id=?", (spotify_id,))

    # ---- cached lookups ---------------------------------------------------------
    def get_lookup(
        self, source: str, key: str, hit_ttl_days: int, miss_ttl_days: int
    ) -> CachedLookup | None:
        row = self.conn.execute(
            "SELECT status, payload, julianday('now') - julianday(fetched_at) AS age_days "
            "FROM lookups WHERE source=? AND key=?",
            (source, key),
        ).fetchone()
        if row is None:
            return None
        ttl = hit_ttl_days if row["status"] == "hit" else miss_ttl_days
        if row["age_days"] > ttl:
            return None
        payload = json.loads(row["payload"]) if row["payload"] else None
        return CachedLookup(hit=row["status"] == "hit", payload=payload)

    def put_lookup(self, source: str, key: str, payload: dict[str, Any] | None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO lookups(source, key, status, payload, fetched_at) "
                "VALUES (?,?,?,?,datetime('now')) "
                "ON CONFLICT(source, key) DO UPDATE SET status=excluded.status, "
                "payload=excluded.payload, fetched_at=excluded.fetched_at",
                (source, key, "hit" if payload else "miss",
                 json.dumps(payload, ensure_ascii=False) if payload else None),
            )

    # ---- reporting ----------------------------------------------------------------
    def effective_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM effective_classification ORDER BY added_at DESC"
        ).fetchall()

    def review_queue(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM effective_classification WHERE status='needs_review' "
            "ORDER BY confidence DESC, added_at DESC"
        ).fetchall()

    def summary_counts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT status, language, COUNT(*) AS n FROM effective_classification "
            "GROUP BY status, language ORDER BY n DESC"
        ).fetchall()

    def override_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM overrides").fetchone()["n"])

    # ---- playlists ------------------------------------------------------------------
    def get_playlist_id(self, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT spotify_playlist_id FROM playlists WHERE name=?", (name,)
        ).fetchone()
        return row["spotify_playlist_id"] if row else None

    def set_playlist_id(self, name: str, playlist_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO playlists(name, spotify_playlist_id) VALUES (?,?) "
                "ON CONFLICT(name) DO UPDATE SET spotify_playlist_id=excluded.spotify_playlist_id, "
                "updated_at=datetime('now')",
                (name, playlist_id),
            )


def chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
