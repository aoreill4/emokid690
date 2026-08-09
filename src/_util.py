"""Small dependency-free helpers shared by the fetch backends.

Kept in its own module (no third-party imports) so the ScrapeCreators client can
reuse them without importing ``tiktok_client`` — which would pull in TikTokApi /
Playwright, the exact heavy stack the hosted-API path exists to avoid.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_VIDEO_ID_RE = re.compile(r"/video/(\d+)")


def load_env_file(path) -> None:
    """Load a .env file, tolerating Windows encodings.

    PowerShell's ``>>`` / ``>`` writes UTF-16 (with a BOM), which python-dotenv's
    default UTF-8 reader can't parse — it raises UnicodeDecodeError and crashes
    the whole script. Detect the BOM and load with the matching encoding so a
    UTF-16 .env works transparently; a missing or unreadable file is non-fatal
    (real environment variables still apply).
    """
    p = Path(path)
    if not p.exists():
        return
    try:
        head = p.read_bytes()[:3]
    except OSError:
        return
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encoding = "utf-16"
    elif head[:3] == b"\xef\xbb\xbf":
        encoding = "utf-8-sig"  # UTF-8 with BOM (PowerShell's Set-Content -Encoding utf8)
    else:
        encoding = "utf-8"
    try:
        from dotenv import load_dotenv
        load_dotenv(p, encoding=encoding)
    except Exception as exc:  # never let a bad .env crash the pipeline
        print(f"warning: could not read {p} ({exc}); using environment variables",
              file=sys.stderr)


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
