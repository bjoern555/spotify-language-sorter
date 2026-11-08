# Spotify Language Sorter

Sorts your Spotify Liked Songs into playlists by the language that is actually sung, and flags instrumentals.

Existing tools guess from the title or artist nationality, which fails for instrumentals, short dialect titles and English songs by non-English artists. This one looks at the real data:

1. **MusicBrainz** (via the track's ISRC) for the language of the linked work
2. **LRCLIB** for the lyrics and an instrumental flag
3. **Lingua** to detect the language of the lyrics
4. Anything uncertain goes to a manual review queue

Everything is cached in a local SQLite file, so rescans are fast and your manual decisions are never overwritten.

## Setup

Requires Python 3.11+ and a Spotify Premium account (needed to own a developer app).

1. Create an app at https://developer.spotify.com/dashboard (Web API) with the redirect URI `http://127.0.0.1:8888/callback`, and add your account under User Management.
2. Install:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env   # add your client ID, secret and a contact email
   ```

## Usage

```bash
python main.py scan                  # fetch liked songs and classify new ones
python main.py scan --incremental    # only newly liked songs
python main.py summary               # language breakdown
python main.py review                # confirm or correct uncertain tracks
python main.py export --all --min-tracks 5      # one playlist per language
python main.py export --exclude-english         # "Liked [Everything but English]"
```

Use `--dry-run` with `export` to preview. Tests: `pip install -r requirements-dev.txt && pytest`.

## Notes

- The first scan takes about 1 second per unique track (MusicBrainz rate limit).
- Very new releases often have no lyrics yet and stay in review.
- Lingua has no Swiss German model; use `review` or MusicBrainz data to label `gsw`.
- Spotify development-mode apps are limited to 5 users, so this is a personal tool rather than a hosted service.
