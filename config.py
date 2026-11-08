"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # python-dotenv is optional; plain environment variables work too
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover

    def load_dotenv(*_args: object, **_kwargs: object) -> bool:
        return False


VERSION = "0.1.0"
APP_NAME = "SpotifyLanguageSorter"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


@dataclass(frozen=True, slots=True)
class Settings:
    spotify_client_id: str
    spotify_client_secret: str
    spotify_redirect_uri: str
    contact: str
    db_path: Path
    token_cache_path: Path
    confidence_threshold: float
    workers: int
    mb_interval: float
    lrclib_concurrency: int
    http_timeout: float
    lookup_hit_ttl_days: int
    lookup_miss_ttl_days: int
    min_words: int

    @property
    def user_agent(self) -> str:
        # MusicBrainz: "AppName/Version (contact-url-or-email)"
        return f"{APP_NAME}/{VERSION} ({self.contact or 'no-contact-configured'})"

    @classmethod
    def load(cls, *, need_spotify: bool = True, need_contact: bool = True) -> "Settings":
        load_dotenv()

        missing: list[str] = []
        if need_spotify:
            missing += [
                n for n in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET") if not os.getenv(n)
            ]
        contact = os.getenv("LANGSORTER_CONTACT", "").strip()
        if need_contact and not contact:
            missing.append("LANGSORTER_CONTACT")
        if missing:
            raise ConfigError(
                "Missing environment variables: " + ", ".join(missing)
                + " (see .env.example)"
            )

        redirect = os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
        if "localhost" in redirect:
            raise ConfigError(
                "SPOTIPY_REDIRECT_URI must use the loopback IP (http://127.0.0.1:PORT/...), "
                "Spotify no longer accepts 'localhost'."
            )

        threshold = _env_float("LANGSORTER_CONFIDENCE", 0.75)
        if not 0.0 < threshold <= 1.0:
            raise ConfigError("LANGSORTER_CONFIDENCE must be in (0, 1]")
        workers = _env_int("LANGSORTER_WORKERS", 8)
        if workers < 1:
            raise ConfigError("LANGSORTER_WORKERS must be >= 1")
        mb_interval = _env_float("LANGSORTER_MB_INTERVAL", 1.1)
        if mb_interval < 1.0:
            raise ConfigError("LANGSORTER_MB_INTERVAL must be >= 1.0 (MusicBrainz rate limit)")

        return cls(
            spotify_client_id=os.getenv("SPOTIPY_CLIENT_ID", ""),
            spotify_client_secret=os.getenv("SPOTIPY_CLIENT_SECRET", ""),
            spotify_redirect_uri=redirect,
            contact=contact,
            db_path=Path(os.getenv("LANGSORTER_DB", "cache.db")),
            token_cache_path=Path(os.getenv("LANGSORTER_TOKEN_CACHE", ".spotify_token")),
            confidence_threshold=threshold,
            workers=workers,
            mb_interval=mb_interval,
            lrclib_concurrency=max(1, _env_int("LANGSORTER_LRCLIB_CONCURRENCY", 5)),
            http_timeout=_env_float("LANGSORTER_HTTP_TIMEOUT", 15.0),
            lookup_hit_ttl_days=_env_int("LANGSORTER_HIT_TTL_DAYS", 365),
            lookup_miss_ttl_days=_env_int("LANGSORTER_MISS_TTL_DAYS", 30),
            min_words=_env_int("LANGSORTER_MIN_WORDS", 15),
        )
