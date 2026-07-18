"""Async wrapper around TikTokApi with anti-blocking hardening.

Isolates every library quirk behind three coroutines — ``get_video``,
``iter_comments``, ``iter_user_videos`` — that only ever hand back plain
``dict`` payloads.

Anti-blocking strategy (personal scale — her + a few comp creators):
  * **Warm pool, reused.** A small pool of TikTokApi sessions is created once and
    reused across many requests. We never spin up a session per request — that's
    both slow and a bot signature.
  * **Coherent UA/fingerprint rotation at session boundaries.** Each pool
    generation runs under one realistic desktop-Chrome fingerprint (UA + matching
    viewport + locale, so nothing contradicts). We rotate to a fresh fingerprint
    every time the pool is recycled — never a per-request UA swap, which would
    mismatch the real browser fingerprint and look *more* botty.
  * **Proxy rotation (optional).** When one or more proxies are supplied, the pool
    is spread across them (Playwright applies a proxy per browser session), and the
    lead proxy is rotated every time the pool recycles — so TikTok sees traffic
    from several IPs instead of hammering one. Without proxies this is a no-op and
    behaviour is unchanged.
  * **Pacing + jitter** between requests, and **exponential backoff** on transient
    errors / empty (blocked) payloads.
  * **Recycling** after ``recycle_after`` requests: tear the pool down and rebuild
    it with a new fingerprint + fresh msToken session (and the next proxy), so long
    runs don't ride one stale identity.

TikTokApi drives a headless Chromium via Playwright. On this repo's dev sandbox
Chromium is pre-installed at ``$PLAYWRIGHT_BROWSERS_PATH`` — do NOT run
``playwright install``. On a normal machine TikTokApi downloads its own on first
run (slow once).
"""

from __future__ import annotations

import asyncio
import glob
import os
import random
from typing import Any, AsyncIterator, Optional, Union
from urllib.parse import unquote, urlparse

from TikTokApi import TikTokApi

from _util import backoff_schedule, extract_video_id  # noqa: F401  (re-exported)


def parse_proxy(proxy: Union[str, dict, None]) -> Optional[dict]:
    """Normalize one proxy spec into the dict Playwright/TikTokApi expects.

    Accepts either a URL string (``http://host:port``,
    ``http://user:pass@host:port``, ``socks5://host:port``) or an already-formed
    ``{"server": ...}`` dict, and returns
    ``{"server": "scheme://host:port", "username"?: ..., "password"?: ...}``.

    Credentials are URL-decoded so values containing ``@`` / ``:`` (percent-encoded
    in the URL) survive. Returns ``None`` for empty input. Exposed as a pure
    function so it can be unit-tested without a live browser.
    """
    if not proxy:
        return None
    if isinstance(proxy, dict):
        return proxy
    parsed = urlparse(str(proxy).strip())
    if not parsed.hostname:
        raise ValueError(f"Could not parse proxy: {proxy!r}")
    scheme = parsed.scheme or "http"
    server = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    out: dict[str, str] = {"server": server}
    if parsed.username:
        out["username"] = unquote(parsed.username)
    if parsed.password:
        out["password"] = unquote(parsed.password)
    return out


# ---------------------------------------------------------------------------
# coherent browser fingerprints (UA must agree with viewport/locale/platform)
# ---------------------------------------------------------------------------
FINGERPRINTS: list[dict[str, Any]] = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "viewport": {"width": 1920, "height": 1080},
        "locale": "en-US",
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "viewport": {"width": 1536, "height": 864},
        "locale": "en-US",
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-US",
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "viewport": {"width": 1680, "height": 1050},
        "locale": "en-GB",
    },
    {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "viewport": {"width": 1600, "height": 900},
        "locale": "en-US",
    },
]


def choose_fingerprint(rng: Optional[random.Random] = None) -> dict[str, Any]:
    """Pick one coherent (user_agent, viewport, locale) fingerprint."""
    r = rng or random
    return dict(r.choice(FINGERPRINTS))


def _preinstalled_chromium() -> Optional[str]:
    """Return a pre-installed Chromium path so Playwright doesn't download one.

    The dev sandbox ships a Chromium build under $PLAYWRIGHT_BROWSERS_PATH that
    may not match the pip-installed Playwright's expected version; we point
    launch() at it via ``executable_path``. Returns None when nothing is found —
    then TikTokApi uses its own managed browser (normal on a user's machine).
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


class TikTokClient:
    """Async context manager owning a warm, self-recycling TikTokApi pool.

    Usage:
        async with TikTokClient(ms_token=...) as client:
            info = await client.get_video("https://.../video/123")
            async for c in client.iter_comments("123"):
                ...
    """

    def __init__(
        self,
        ms_token: Optional[str] = None,
        pool_size: int = 2,
        min_delay: float = 2.0,
        max_delay: float = 4.0,
        recycle_after: int = 50,
        max_retries: int = 4,
        proxies: Optional[list[Union[str, dict]]] = None,
        rng: Optional[random.Random] = None,
    ):
        self._ms_token = ms_token
        self._pool_size = max(1, pool_size)
        self._min_delay = max(0.0, min_delay)
        self._max_delay = max(self._min_delay, max_delay)
        self._recycle_after = max(1, recycle_after)
        self._max_retries = max(0, max_retries)
        # Normalize once; skip blanks so an empty env var / trailing comma is inert.
        self._proxies: list[dict] = [
            p for p in (parse_proxy(x) for x in (proxies or [])) if p
        ]
        self._proxy_offset = 0
        self._rng = rng or random.Random()

        self._api: Optional[TikTokApi] = None
        self._fingerprint: dict[str, Any] = {}
        self._request_count = 0

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "TikTokClient":
        await self._create_pool()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._close_pool()

    def _proxies_for_generation(self) -> Optional[list[dict]]:
        """Rotate the proxy list so a fresh pool leads with the next proxy.

        TikTokApi spreads a pool across the proxies it's handed; rotating the
        lead each generation means successive pools ride different IPs even when
        there are more proxies than sessions. Returns ``None`` when no proxies
        were configured (TikTokApi then makes direct connections, as before).
        """
        if not self._proxies:
            return None
        n = len(self._proxies)
        rotated = [self._proxies[(self._proxy_offset + i) % n] for i in range(n)]
        # advance so the next generation starts past the sessions we just used
        self._proxy_offset = (self._proxy_offset + self._pool_size) % n
        return rotated

    async def _create_pool(self) -> None:
        self._fingerprint = choose_fingerprint(self._rng)
        self._api = TikTokApi()
        ms_tokens = [self._ms_token] if self._ms_token else None
        await self._api.create_sessions(
            ms_tokens=ms_tokens,
            proxies=self._proxies_for_generation(),
            num_sessions=self._pool_size,
            sleep_after=3,
            browser="chromium",
            headless=True,
            executable_path=_preinstalled_chromium(),
            # one coherent fingerprint for this whole pool generation
            context_options={
                "user_agent": self._fingerprint["user_agent"],
                "viewport": self._fingerprint["viewport"],
                "locale": self._fingerprint["locale"],
            },
        )

    async def _close_pool(self) -> None:
        if self._api is not None:
            try:
                await self._api.close_sessions()
            finally:
                self._api = None

    async def _recycle(self) -> None:
        """Rebuild the pool under a fresh fingerprint (new session identity)."""
        await self._close_pool()
        await self._create_pool()
        self._request_count = 0

    def _require_api(self) -> TikTokApi:
        if self._api is None:
            raise RuntimeError("TikTokClient used outside its async context")
        return self._api

    # -- pacing / recycling gate ------------------------------------------
    async def _before_request(self) -> None:
        """Recycle at the generation boundary, then pace with jitter.

        Called at a safe boundary (before any iterator is created), so recycling
        never invalidates an in-flight paginator.
        """
        if self._request_count and self._request_count % self._recycle_after == 0:
            await self._recycle()
        if self._request_count > 0:  # no delay before the very first request
            await asyncio.sleep(self._rng.uniform(self._min_delay, self._max_delay))
        self._request_count += 1

    async def _backoff(self, attempt: int) -> None:
        bases = backoff_schedule(self._max_retries)
        base = bases[attempt] if attempt < len(bases) else (bases[-1] if bases else 1.0)
        await asyncio.sleep(base + self._rng.uniform(0, 1.0))

    # -- fetch surface -----------------------------------------------------
    async def get_video(self, url_or_id: str) -> dict:
        """Return the raw item dict for one video (retried with backoff)."""
        video_id = extract_video_id(url_or_id)
        for attempt in range(self._max_retries + 1):
            try:
                await self._before_request()
                video = self._require_api().video(id=video_id)
                info = await video.info()
                if isinstance(info, dict) and info:
                    return info
                raise RuntimeError("empty video payload (likely blocked)")
            except Exception:
                if attempt >= self._max_retries:
                    raise
                await self._backoff(attempt)
        return {}

    async def iter_comments(
        self,
        url_or_id: str,
        count: int = 200,
        include_replies: bool = False,
        max_replies_per_comment: int = 1000,
    ) -> AsyncIterator[dict]:
        """Yield raw comment dicts for a video, deduped across retry restarts.

        ``include_replies`` / ``max_replies_per_comment`` are accepted for
        interface parity with ScrapeCreatorsClient but not separately honored
        here — the scraper backend does not fetch reply threads. Use
        ``--source scrapecreators`` for full threads.
        """
        del include_replies, max_replies_per_comment  # not supported by this backend
        video_id = extract_video_id(url_or_id)
        seen: set[str] = set()
        for attempt in range(self._max_retries + 1):
            try:
                await self._before_request()
                video = self._require_api().video(id=video_id)
                async for comment in video.comments(count=count):
                    d = _as_dict(comment)
                    cid = d.get("cid") or d.get("id")
                    if cid is not None and cid in seen:
                        continue
                    if cid is not None:
                        seen.add(cid)
                    yield d
                return
            except Exception:
                if attempt >= self._max_retries:
                    raise
                await self._backoff(attempt)

    async def iter_user_videos(
        self, username: str, count: int = 30
    ) -> AsyncIterator[dict]:
        """Yield raw item dicts for a creator's recent videos (deduped on retry)."""
        seen: set[str] = set()
        for attempt in range(self._max_retries + 1):
            try:
                await self._before_request()
                user = self._require_api().user(username=username)
                async for video in user.videos(count=count):
                    d = _as_dict(video)
                    vid = d.get("id") or d.get("aweme_id")
                    if vid is not None and vid in seen:
                        continue
                    if vid is not None:
                        seen.add(vid)
                    yield d
                return
            except Exception:
                if attempt >= self._max_retries:
                    raise
                await self._backoff(attempt)


def _as_dict(obj) -> dict:
    """TikTokApi objects expose their payload via ``.as_dict``."""
    data = getattr(obj, "as_dict", None)
    if isinstance(data, dict):
        return data
    return obj if isinstance(obj, dict) else {}
