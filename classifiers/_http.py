"""Shared async HTTP helpers: rate limiting, retries with backoff, uniform failure type."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LookupFailed(RuntimeError):
    """A lookup could not be completed (network/server trouble). NOT the same as 'not found'."""


class RateLimiter:
    """Serialises callers and guarantees >= ``min_interval`` seconds between releases."""

    def __init__(self, min_interval: float) -> None:
        self._interval = min_interval
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next_slot - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_slot = time.monotonic() + self._interval


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "0")), 60.0)
    except ValueError:
        return 0.0


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    limiter: RateLimiter | None = None,
    gate: asyncio.Semaphore | None = None,
    max_attempts: int = 4,
    label: str = "",
) -> Any | None:
    """GET -> parsed JSON. Returns None on 404. Raises LookupFailed once retries are spent.

    Every attempt (including retries) passes through ``limiter``, so back-off can never
    cause a burst that violates the provider's rate limit.
    """
    delay = 1.0
    last_error = "unknown error"
    for attempt in range(1, max_attempts + 1):
        try:
            if limiter is not None:
                await limiter.wait()
            if gate is not None:
                async with gate:
                    resp = await client.get(url, params=params)
            else:
                resp = await client.get(url, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 404:
                return None
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    last_error = "response was not valid JSON"
            elif resp.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {resp.status_code}"
                delay = max(delay, _retry_after(resp))
            else:
                raise LookupFailed(f"{label or url}: HTTP {resp.status_code}")
        if attempt < max_attempts:
            log.debug("%s failed (%s); retry %d in %.1fs", label or url, last_error, attempt, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise LookupFailed(f"{label or url}: {last_error} after {max_attempts} attempts")
