from __future__ import annotations

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

GITHUB_API_BASE = "https://api.github.com"


def build_async_client() -> httpx.AsyncClient:
    """
    Configured httpx.AsyncClient for all outbound requests.

    GitHub API requires a User-Agent header or returns 403.
    Large file downloads (CS2 assets) need a generous read timeout.
    """
    return httpx.AsyncClient(
        headers={
            "User-Agent": "cs2-server-creator/1.0",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=10.0),
        follow_redirects=True,
    )


def github_retry():
    """
    Tenacity retry decorator for GitHub API calls.

    Retries on transient network errors only.
    Does NOT retry 403 (rate limit) or 404 (not found) — those are permanent.
    3 attempts with exponential backoff: 1s, 2s, 4s.
    """
    return retry(
        retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
