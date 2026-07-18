"""Small dependency-free helpers shared by the fetch backends.

Kept in its own module (no third-party imports) so the ScrapeCreators client can
reuse them without importing ``tiktok_client`` — which would pull in TikTokApi /
Playwright, the exact heavy stack the hosted-API path exists to avoid.
"""

from __future__ import annotations

import re

_VIDEO_ID_RE = re.compile(r"/video/(\d+)")


def extract_video_id(url_or_id: str) -> str:
    """Accept a full TikTok URL or a bare numeric id and return the id."""
    s = str(url_or_id).strip()
    if s.isdigit():
        return s
    m = _VIDEO_ID_RE.search(s)
    if m:
        return m.group(1)
    raise ValueError(f"Could not extract a video id from: {url_or_id!r}")


def backoff_schedule(
    max_retries: int, base: float = 2.0, cap: float = 16.0
) -> list[float]:
    """Deterministic backoff bases: base * 2**i, capped (jitter added at runtime).

    e.g. max_retries=4 -> [2, 4, 8, 16]. Exposed as a pure function for testing.
    """
    return [min(base * (2 ** i), cap) for i in range(max_retries)]
