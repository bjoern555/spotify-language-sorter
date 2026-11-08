"""Spotify Language Sorter - CLI.

    python main.py scan
    python main.py summary
    python main.py review
    python main.py export --lang German --lang French --instrumental
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import sqlite3
from collections import Counter
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn,
    TimeElapsedColumn, TimeRemainingColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table

import languages as L
from classifiers.detector import LyricsLanguageDetector
from classifiers.lrclib import LrclibClient, LrclibResult
from classifiers.musicbrainz import MusicBrainzClient
from config import ConfigError, Settings
from db import Database
from models import Track
from pipeline import Pipeline, lrclib_key
from spotify_client import SpotifyClient

app = typer.Typer(help="Sort your Spotify Liked Songs into language playlists.",
                  no_args_is_help=True, add_completion=False)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(message)s", handlers=[RichHandler(console=console, show_path=False)],
    )


def _load(*, need_spotify: bool, need_contact: bool, verbose: bool = False) -> Settings:
    _setup_logging(verbose)
    try:
        return Settings.load(need_spotify=need_spotify, need_contact=need_contact)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/] {exc}")
        raise typer.Exit(2) from exc


# ============================================================================== scan
@app.command()
def scan(
    full: bool = typer.Option(True, "--full/--incremental",
                              help="Full walk (detects un-liked tracks) or stop at the first fully-known page."),
    reclassify_review: bool = typer.Option(False, "--reclassify-review",
                                           help="Also retry tracks currently marked needs_review."),
    limit: Optional[int] = typer.Option(None, "--limit", min=1, help="Only fetch the newest N liked songs."),
    workers: Optional[int] = typer.Option(None, "--workers", min=1, help="Concurrent workers."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Pull Liked Songs and classify every track that is not classified yet."""
    settings = _load(need_spotify=True, need_contact=True, verbose=verbose)
    if workers:
        settings = dataclasses.replace(settings, workers=workers)
    db = Database(settings.db_path)
    sp = SpotifyClient(settings)
    run_id = db.start_run("full" if full else "incremental")

    # ---- phase 1: fetch -------------------------------------------------------------
    seen = 0
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                  MofNCompleteColumn(), console=console) as progress:
        total = sp.liked_total()
        task = progress.add_task("Fetching liked songs", total=min(total, limit) if limit else total)
        known = None if full else db.known_ids()
        for track in sp.iter_liked_tracks(known_ids=known):
            db.upsert_track(track, run_id)
            seen += 1
            progress.advance(task)
            if limit and seen >= limit:
                break
    if full and not limit:
        removed = db.mark_unseen_as_removed(run_id)
        if removed:
            console.print(f"[dim]{removed} track(s) are no longer in your Liked Songs.[/]")
    missing_isrc = db.count_without_isrc()
    if missing_isrc:
        console.print(f"[yellow]{missing_isrc} track(s) have no ISRC[/] - MusicBrainz is skipped "
                      "for those; LRCLIB + lyrics detection still apply.")

    # ---- phase 2: classify -----------------------------------------------------------
    todo = db.tracks_to_classify(include_needs_review=reclassify_review)
    if not todo:
        console.print("[green]Nothing to classify - everything is cached.[/]")
        db.finish_run(run_id, seen, 0)
        return
    uncached_isrcs = {
        t.isrc for t in todo if t.isrc and db.get_lookup(
            "musicbrainz", t.isrc, settings.lookup_hit_ttl_days, settings.lookup_miss_ttl_days) is None
    }
    console.print(f"Classifying {len(todo)} tracks; ~{len(uncached_isrcs)} MusicBrainz lookups "
                  f"(≈ {len(uncached_isrcs) * settings.mb_interval / 60:.1f} min at the 1 req/s limit).")
    try:
        stats = asyncio.run(_classify(db, settings, run_id, todo))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/] Finished tracks are saved - re-run `scan` to resume.")
        raise typer.Exit(130)
    db.finish_run(run_id, seen, sum(stats.values()))
    console.print(f"[green]Done.[/] auto: {stats['auto']}, needs review: {stats['needs_review']}. "
                  "Run `summary`, then `review`.")


async def _classify(db: Database, settings: Settings, run_id: int, todo: list[Track]) -> Counter[str]:
    detector = LyricsLanguageDetector(settings.min_words)
    headers = {"User-Agent": settings.user_agent, "Accept": "application/json"}
    limits = httpx.Limits(max_connections=settings.workers + 4)
    async with httpx.AsyncClient(headers=headers, timeout=settings.http_timeout,
                                 limits=limits, follow_redirects=True) as client:
        pipeline = Pipeline(
            db, settings,
            MusicBrainzClient(client, min_interval=settings.mb_interval),
            LrclibClient(client, concurrency=settings.lrclib_concurrency),
            detector, run_id,
        )
        with Progress(SpinnerColumn(), TextColumn("{task.description}", justify="left"), BarColumn(),
                      MofNCompleteColumn(), TimeElapsedColumn(), TimeRemainingColumn(),
                      console=console) as progress:
            task = progress.add_task("Starting…", total=len(todo))

            def on_result(t: Track, c) -> None:  # noqa: ANN001
                progress.update(task, advance=1,
                                description=f"{t.label[:48]:<48} → {L.display(c.language)}")

            return await pipeline.run(todo, on_result)


# ============================================================================ summary
@app.command()
def summary() -> None:
    """Breakdown of languages, instrumentals and pending reviews."""
    settings = _load(need_spotify=False, need_contact=False)
    db = Database(settings.db_path)
    rows = db.summary_counts()
    if not rows:
        console.print("Library is empty - run `scan` first.")
        raise typer.Exit(1)

    langs: Counter[str] = Counter()
    review = pending = 0
    for r in rows:
        if r["status"] in ("auto", "override"):
            langs[r["language"]] += r["n"]
        elif r["status"] == "needs_review":
            review += r["n"]
        else:
            pending += r["n"]
    total = sum(langs.values()) + review + pending

    table = Table(title=f"Liked Songs: {total} tracks")
    table.add_column("Language"); table.add_column("Tracks", justify="right"); table.add_column("%", justify="right")
    for code, n in langs.most_common():
        table.add_row(L.display(code), str(n), f"{n / total:.1%}")
    if review:
        table.add_row("[yellow]Needs review[/]", str(review), f"{review / total:.1%}")
    if pending:
        table.add_row("[red]Not classified yet[/]", str(pending), f"{pending / total:.1%}")
    console.print(table)
    console.print(f"[dim]Manual overrides: {db.override_count()} · tracks without ISRC: "
                  f"{db.count_without_isrc()}[/]")
    if review:
        console.print("Next: [bold]python main.py review[/]")


# ============================================================================= review
def _lyrics_snippet(db: Database, settings: Settings, row: sqlite3.Row) -> str:
    key = row["isrc"] or f"sp:{row['spotify_id']}"
    cached = db.get_lookup("lrclib", key, 10**6, 10**6)
    if not cached or not cached.payload:
        return ""
    lyrics = LrclibResult.from_dict(cached.payload).lyrics or ""
    lines = [ln for ln in lyrics.splitlines() if ln.strip()][:2]
    return "\n".join(lines)


@app.command()
def review() -> None:
    """Interactively confirm or correct tracks marked needs_review."""
    settings = _load(need_spotify=False, need_contact=False)
    db = Database(settings.db_path)
    queue = db.review_queue()
    if not queue:
        console.print("[green]Nothing to review.[/]")
        return
    console.print(f"{len(queue)} tracks to review. Highest-confidence suggestions first.\n"
                  "[dim]Enter = accept suggestion · type a language (German, fr, gsw…) · "
                  "i = instrumental · s = skip · q = quit[/]\n")
    done = 0
    for i, row in enumerate(queue, 1):
        suggestion = row["suggested"]
        body = (f"[bold]{row['artists']} – {row['name']}[/]\n"
                f"Album: {row['album'] or '—'}\n"
                f"Suggested: [cyan]{L.display(suggestion)}[/] "
                f"(confidence {row['confidence'] or 0:.2f})\n"
                f"Why: {row['detail'] or '—'}\n"
                f"https://open.spotify.com/track/{row['spotify_id']}")
        snippet = _lyrics_snippet(db, settings, row)
        if snippet:
            body += f"\n[dim]{snippet}[/]"
        console.print(Panel(body, title=f"{i}/{len(queue)}"))
        while True:
            answer = Prompt.ask("Language", default="", show_default=False).strip()
            low = answer.lower()
            if low == "q":
                console.print(f"Saved {done} decision(s).")
                return
            if low == "s":
                break
            if low == "":
                if not suggestion:
                    console.print("[yellow]No suggestion available - type a language or 's' to skip.[/]")
                    continue
                code: str | None = suggestion
            else:
                code = L.INSTRUMENTAL if low == "i" else L.resolve(answer)
                if code is None:
                    console.print(f"[yellow]Unknown language {answer!r}. Try a name or ISO code.[/]")
                    continue
            db.set_override(row["spotify_id"], code)
            done += 1
            break
    console.print(f"[green]Review finished.[/] Saved {done} decision(s).")


# ============================================================================= export
def _effective_language(row: sqlite3.Row, use_suggestions: bool) -> str | None:
    if row["status"] in ("auto", "override"):
        return row["language"]
    if use_suggestions and row["status"] == "needs_review" and row["suggested"]:
        return row["suggested"]
    return None


@app.command()
def export(
    lang: list[str] = typer.Option([], "--lang", "-l", help="Language name or code; repeatable."),
    instrumental: bool = typer.Option(False, "--instrumental", help="Playlist of instrumentals."),
    exclude_english: bool = typer.Option(False, "--exclude-english",
                                         help="Playlist 'Everything but English' (instrumentals excluded)."),
    all_languages: bool = typer.Option(False, "--all", help="One playlist per detected language."),
    min_tracks: int = typer.Option(1, "--min-tracks", min=1, help="With --all: skip tiny languages."),
    include_instrumental: bool = typer.Option(False, "--include-instrumental",
                                              help="Let --exclude-english keep instrumentals."),
    use_suggestions: bool = typer.Option(False, "--use-suggestions",
                                         help="Treat unreviewed best-guesses as real classifications."),
    dialects: bool = typer.Option(True, "--dialects/--no-dialects",
                                  help="--lang German also includes Swiss German / Bavarian."),
    public: bool = typer.Option(False, "--public/--private"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Create or sync Spotify playlists from the classification results."""
    if not (lang or instrumental or exclude_english or all_languages):
        console.print("[red]Choose at least one of --lang, --instrumental, --exclude-english, --all.[/]")
        raise typer.Exit(2)
    settings = _load(need_spotify=not dry_run, need_contact=False, verbose=verbose)
    db = Database(settings.db_path)
    rows = db.effective_rows()  # newest liked first
    lang_of = {r["spotify_id"]: _effective_language(r, use_suggestions) for r in rows}
    ids_by_lang: dict[str, list[str]] = {}
    for sid, code in lang_of.items():
        if code and code != L.UNKNOWN:
            ids_by_lang.setdefault(code, []).append(sid)
    skipped = sum(1 for c in lang_of.values() if c is None)

    selections: dict[str, list[str]] = {}
    for text in lang:
        code = L.resolve(text)
        if code is None:
            raise typer.BadParameter(f"unknown language {text!r}", param_hint="--lang")
        ids = [sid for sid, c in lang_of.items() if L.matches(c, code, dialects)] \
            if code != L.INSTRUMENTAL else ids_by_lang.get(L.INSTRUMENTAL, [])
        selections[f"Liked [{L.display(code)}]"] = ids
    if instrumental:
        selections["Liked [Instrumental]"] = ids_by_lang.get(L.INSTRUMENTAL, [])
    if exclude_english:
        keep_inst = include_instrumental
        selections["Liked [Everything but English]"] = [
            sid for sid, c in lang_of.items()
            if c and c != "eng" and c != L.UNKNOWN and (keep_inst or c != L.INSTRUMENTAL)
        ]
    if all_languages:
        for code, ids in sorted(ids_by_lang.items(), key=lambda kv: -len(kv[1])):
            if len(ids) >= min_tracks:
                selections[f"Liked [{L.display(code)}]"] = ids

    table = Table(title="Playlists to sync")
    table.add_column("Playlist"); table.add_column("Tracks", justify="right"); table.add_column("Action")
    for name, ids in selections.items():
        table.add_row(name, str(len(ids)), "update" if db.get_playlist_id(name) else "create")
    console.print(table)
    if skipped:
        console.print(f"[dim]{skipped} track(s) skipped (unreviewed or unclassified).[/]")
    if dry_run:
        return
    empty = [n for n, ids in selections.items() if not ids]
    for n in empty:
        console.print(f"[yellow]Skipping {n}: no matching tracks.[/]")
        del selections[n]
    if not selections or not (yes or Confirm.ask("Write these playlists to Spotify?", default=True)):
        return

    sp = SpotifyClient(settings)
    for name, ids in selections.items():
        pid = sp.sync_playlist(name, ids, db.get_playlist_id(name), public)
        db.set_playlist_id(name, pid)
        console.print(f"[green]✓[/] {name}: {len(ids)} tracks")


if __name__ == "__main__":
    app()
