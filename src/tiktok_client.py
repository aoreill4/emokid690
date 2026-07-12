"""Thin async wrapper around TikTokApi.

Isolates every library quirk (session creation, browser wiring, field access)
behind three coroutines: ``get_video``, ``iter_comments``, ``iter_user_videos``.
The rest of the codebase only ever sees plain ``dict`` payloads.

TikTokApi drives a headless Chromium via Playwright. This environment ships
Chromium at ``$PLAYWRIGHT_BROWSERS_PATH`` — do NOT call ``playwright install``.
"""

from __future__ import annotations

import glob
import os
import re
from typing import AsyncIterator, Optional

from TikTokApi import TikTokApi


_VIDEO_ID_RE = re.compile(r"/video/(\d+)")


def _preinstalled_chromium() -> Optional[str]:
    """Find the pre-installed Chromium so Playwright doesn't try to download one.

    This environment ships a Chromium build under $PLAYWRIGHT_BROWSERS_PATH that
    may not match the version the freshly-pip-installed Playwright expects. We
    point launch() at the existing binary via ``executable_path`` instead of
    running ``playwright install``. Returns None if nothing is found (then the
    library falls back to its own managed browser).
    """
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    for pattern in (
        os.path.join(root, "chromium-*", "chrome-linux", "chrome"),
        os.path.join(root, "chromium", "chrome-linux", "chrome"),
    ):
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


def extract_video_id(url_or_id: str) -> str:
    """Accept a full TikTok URL or a bare numeric id and return the id."""
    s = str(url_or_id).strip()
    if s.isdigit():
        return s
    m = _VIDEO_ID_RE.search(s)
    if m:
        return m.group(1)
    raise ValueError(f"Could not extract a video id from: {url_or_id!r}")


class TikTokClient:
    """Async context manager owning a TikTokApi session.

    Usage:
        async with TikTokClient(ms_token=...) as client:
            info = await client.get_video("https://.../video/123")
            async for c in client.iter_comments("123"):
                ...
    """

    def __init__(self, ms_token: Optional[str] = None, num_sessions: int = 1):
        self._ms_token = ms_token
        self._num_sessions = num_sessions
        self._api: Optional[TikTokApi] = None

    async def __aenter__(self) -> "TikTokClient":
        self._api = TikTokApi()
        ms_tokens = [self._ms_token] if self._ms_token else None
        await self._api.create_sessions(
            ms_tokens=ms_tokens,
            num_sessions=self._num_sessions,
            sleep_after=3,
            browser="chromium",
            headless=True,
            executable_path=_preinstalled_chromium(),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._api is not None:
            await self._api.close_sessions()
            self._api = None

    def _require_api(self) -> TikTokApi:
        if self._api is None:
            raise RuntimeError("TikTokClient used outside its async context")
        return self._api

    async def get_video(self, url_or_id: str) -> dict:
        """Return the raw item dict for one video."""
        api = self._require_api()
        video_id = extract_video_id(url_or_id)
        video = api.video(id=video_id)
        info = await video.info()
        return info if isinstance(info, dict) else {}

    async def iter_comments(
        self, url_or_id: str, count: int = 200
    ) -> AsyncIterator[dict]:
        """Yield raw comment dicts for a video (paginated by the library)."""
        api = self._require_api()
        video_id = extract_video_id(url_or_id)
        video = api.video(id=video_id)
        async for comment in video.comments(count=count):
            yield _as_dict(comment)

    async def iter_user_videos(
        self, username: str, count: int = 30
    ) -> AsyncIterator[dict]:
        """Yield raw item dicts for a creator's recent videos."""
        api = self._require_api()
        user = api.user(username=username)
        async for video in user.videos(count=count):
            yield _as_dict(video)


def _as_dict(obj) -> dict:
    """TikTokApi objects expose their payload via ``.as_dict``."""
    data = getattr(obj, "as_dict", None)
    if isinstance(data, dict):
        return data
    return obj if isinstance(obj, dict) else {}
