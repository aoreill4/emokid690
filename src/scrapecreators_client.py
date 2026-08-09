"""Fetch backend backed by the ScrapeCreators hosted API.

A drop-in alternative to ``TikTokClient`` that exposes the identical async
surface — ``get_video``, ``iter_comments``, ``iter_user_videos``, each handing
back plain ``dict`` payloads — so ``loader.py`` / ``schema.py`` don't care which
backend produced the data.

Why this exists: the unofficial scraper (``tiktok_client``) drives a headless
browser and gets rate-limited / bot-blocked. ScrapeCreators runs the scraping
(and the proxies) server-side and returns TikTok's native ``aweme`` JSON — the
same shape ``schema.parse_video`` / ``parse_comment`` already read — over plain
authenticated HTTPS. No browser, no Playwright, no proxies, runs anywhere.

Dependencies: standard library only. Blocking ``urllib`` calls are pushed to a
worker thread via ``asyncio.to_thread`` so the async interface is honest.

Auth: an API key (``x-api-key`` header), taken from the ``api_key`` argument or
the ``SCRAPECREATORS_API_KEY`` environment variable. Get a key (100 free
credits) at https://scrapecreators.com.

Endpoints used (each ~1 credit):
  GET /v3/tiktok/profile/videos?handle=<handle>&cursor=<n>   -> aweme_list
  GET /v2/tiktok/video?url=<video_url>                       -> aweme_detail
  GET /v1/tiktok/video/comments?url=<video_url>&cursor=<n>   -> comments
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, AsyncIterator, Optional

from _util import backoff_schedule, extract_video_id
from schema import webvtt_to_text

BASE_URL = "https://api.scrapecreators.com"
_CAPTION_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


class ScrapeCreatorsError(RuntimeError):
    """Raised for non-retryable API errors (bad key, out of credits, etc.)."""


class ScrapeCreatorsClient:
    """Async context manager that fetches TikTok data via the ScrapeCreators API.

    Usage mirrors TikTokClient:
        async with ScrapeCreatorsClient(handle="emokid690") as client:
            info = await client.get_video("https://.../video/123")
            async for c in client.iter_comments("123"):
                ...

    ``handle`` is used to build a video URL when a bare video id is passed (the
    API's video/comment endpoints take a URL). TikTok resolves videos by id, so
    the handle only needs to be a plausible owner; pass the real one when known.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        handle: Optional[str] = None,
        min_delay: float = 0.0,
        max_delay: float = 0.0,
        max_retries: int = 4,
        timeout: float = 60.0,
        rng: Optional[random.Random] = None,
    ):
        self._api_key = api_key or os.getenv("SCRAPECREATORS_API_KEY")
        if not self._api_key:
            raise ScrapeCreatorsError(
                "No API key. Set SCRAPECREATORS_API_KEY (or pass api_key=...). "
                "Get one free at https://scrapecreators.com."
            )
        self._handle = handle
        self._min_delay = max(0.0, min_delay)
        self._max_delay = max(self._min_delay, max_delay)
        self._max_retries = max(0, max_retries)
        self._timeout = timeout
        self._rng = rng or random.Random()
        self._request_count = 0
        # best-effort reply-fetching circuit breaker (see _replies_safe)
        self._reply_failures = 0
        self._replies_disabled = False

    # -- lifecycle (no connections to hold, but keep the CM contract) ------
    async def __aenter__(self) -> "ScrapeCreatorsClient":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    # -- URL helper --------------------------------------------------------
    def _video_url(self, url_or_id: str) -> str:
        """Return a full video URL, building one from a bare id if needed."""
        s = str(url_or_id).strip()
        if s.startswith("http"):
            return s
        vid = extract_video_id(s)
        owner = self._handle or "tiktok"
        return f"https://www.tiktok.com/@{owner}/video/{vid}"

    # -- HTTP with pacing + retry -----------------------------------------
    async def _pace(self) -> None:
        if self._request_count > 0 and self._max_delay > 0:
            await asyncio.sleep(self._rng.uniform(self._min_delay, self._max_delay))
        self._request_count += 1

    @staticmethod
    def _blocking_get(url: str, api_key: str, timeout: float) -> tuple[int, str]:
        req = urllib.request.Request(url, headers={"x-api-key": api_key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    async def _get(self, path: str, params: dict) -> dict:
        """GET an endpoint with pacing, retry/backoff, and clear error messages."""
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{BASE_URL}{path}?{query}"
        bases = backoff_schedule(self._max_retries)

        for attempt in range(self._max_retries + 1):
            await self._pace()
            try:
                status, body = await asyncio.to_thread(
                    self._blocking_get, url, self._api_key, self._timeout
                )
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt >= self._max_retries:
                    raise ScrapeCreatorsError(f"connection failed for {path}: {exc}")
                await self._backoff(bases, attempt)
                continue

            if status == 200:
                try:
                    return json.loads(body) if body.strip() else {}
                except json.JSONDecodeError as exc:
                    raise ScrapeCreatorsError(f"bad JSON from {path}: {exc}")

            # 402 (out of credits) / 401 (bad key) are terminal — don't retry.
            if status in (401, 402, 403):
                raise ScrapeCreatorsError(
                    f"{path} -> HTTP {status}: {_message(body)}"
                )
            # 429 / 5xx are transient — retry with backoff.
            if attempt >= self._max_retries:
                raise ScrapeCreatorsError(
                    f"{path} -> HTTP {status} after {attempt + 1} tries: {_message(body)}"
                )
            await self._backoff(bases, attempt)
        return {}

    async def _backoff(self, bases: list[float], attempt: int) -> None:
        base = bases[attempt] if attempt < len(bases) else (bases[-1] if bases else 1.0)
        await asyncio.sleep(base + self._rng.uniform(0, 1.0))

    # -- fetch surface (mirrors TikTokClient) -----------------------------
    async def get_video(self, url_or_id: str) -> dict:
        """Return the raw aweme dict for one video."""
        data = await self._get("/v2/tiktok/video", {"url": self._video_url(url_or_id)})
        detail = data.get("aweme_detail")
        return detail if isinstance(detail, dict) else {}

    async def iter_comments(
        self,
        url_or_id: str,
        count: int = 200,
        include_replies: bool = False,
        max_replies_per_comment: int = 1000,
    ) -> AsyncIterator[dict]:
        """Yield raw comment dicts for a video, deduped and paginated.

        ``count`` bounds the number of *top-level* comments fetched. When
        ``include_replies`` is set, each top-level comment that has replies also
        has its reply thread fetched and yielded (replies are extra — they do not
        count against ``count``). Replies arrive as comment dicts whose
        ``reply_id`` points at the parent, so downstream parsing threads them
        automatically.
        """
        video_url = self._video_url(url_or_id)
        seen: set[str] = set()
        cursor = 0
        top_level = 0
        while top_level < count:
            data = await self._get(
                "/v1/tiktok/video/comments", {"url": video_url, "cursor": cursor}
            )
            items = data.get("comments") or []
            if not items:
                return
            for c in items:
                cid = c.get("cid") or c.get("id")
                if cid is not None and cid in seen:
                    continue
                if cid is not None:
                    seen.add(cid)
                yield c
                top_level += 1
                if include_replies and cid and (c.get("reply_comment_total") or 0) > 0:
                    async for reply in self._replies_safe(
                        video_url, cid, max_replies_per_comment, seen
                    ):
                        yield reply
                if top_level >= count:
                    return
            if not data.get("has_more"):
                return
            next_cursor = data.get("cursor")
            if not next_cursor or next_cursor == cursor:
                return  # guard against a stuck paginator
            cursor = next_cursor

    async def _replies_safe(
        self, video_url: str, comment_id: str, count: int, seen: set
    ) -> AsyncIterator[dict]:
        """Yield a comment's replies, but never let reply errors abort the run.

        Reply fetching is best-effort: if the endpoint errors (e.g. a transient
        failure), we warn and skip that thread. After several failures in a row we
        disable reply fetching for the rest of the run so a systemic problem
        doesn't spam warnings or burn credits.
        """
        if self._replies_disabled:
            return
        try:
            async for reply in self.iter_comment_replies(
                video_url, comment_id, count=count, _seen=seen
            ):
                yield reply
            self._reply_failures = 0
        except ScrapeCreatorsError as exc:
            self._reply_failures += 1
            print(f"warning: could not fetch replies for comment {comment_id}: {exc}",
                  file=sys.stderr)
            if self._reply_failures >= 3:
                self._replies_disabled = True
                print("warning: disabling reply fetching for the rest of this run "
                      "after repeated failures.", file=sys.stderr)

    async def iter_comment_replies(
        self, url_or_id: str, comment_id: str, count: int = 1000, _seen: Optional[set] = None
    ) -> AsyncIterator[dict]:
        """Yield up to ``count`` reply dicts for one comment, deduped, paginated."""
        video_url = self._video_url(url_or_id)
        seen: set = _seen if _seen is not None else set()
        cursor = 0
        yielded = 0
        while yielded < count:
            data = await self._get(
                "/v1/tiktok/video/comment/replies",
                {"url": video_url, "comment_id": comment_id, "cursor": cursor},
            )
            items = data.get("comments") or data.get("replies") or []
            if not items:
                return
            for r in items:
                rid = r.get("cid") or r.get("id")
                if rid is not None and rid in seen:
                    continue
                if rid is not None:
                    seen.add(rid)
                yield r
                yielded += 1
                if yielded >= count:
                    return
            if not data.get("has_more"):
                return
            next_cursor = data.get("cursor")
            if not next_cursor or next_cursor == cursor:
                return  # guard against a stuck paginator
            cursor = next_cursor

    async def iter_user_videos(
        self, username: str, count: int = 30
    ) -> AsyncIterator[dict]:
        """Yield up to ``count`` raw aweme dicts for a creator's videos, paginated."""
        seen: set[str] = set()
        cursor = 0
        yielded = 0
        while yielded < count:
            # NOTE: this endpoint takes the paging cursor as `max_cursor` (the
            # comments endpoint uses `cursor` — they differ). Sending `cursor`
            # here is ignored, so the API keeps returning page 1.
            data = await self._get(
                "/v3/tiktok/profile/videos", {"handle": username, "max_cursor": cursor}
            )
            items = data.get("aweme_list") or []
            if not items:
                return
            for v in items:
                vid = v.get("aweme_id") or v.get("id")
                if vid is not None and vid in seen:
                    continue
                if vid is not None:
                    seen.add(vid)
                yield v
                yielded += 1
                if yielded >= count:
                    return
            if not data.get("has_more"):
                return
            next_cursor = data.get("max_cursor")
            if not next_cursor or next_cursor == cursor:
                return  # guard against a stuck paginator
            cursor = next_cursor

    # -- transcripts (TikTok auto-captions) --------------------------------
    async def get_transcript(
        self, url_or_id: str, detail: Optional[dict] = None
    ) -> Optional[dict]:
        """Return ``{'transcript','lang','source'}`` from TikTok's auto-caption, else None.

        Reads ``video.cla_info.caption_infos`` from the video-info payload, picks
        the original-language WebVTT caption, downloads it, and flattens it to
        plain text. Returns None when the video has no usable caption (a Whisper
        fallback could fill those later). Pass a pre-fetched ``detail``
        (aweme_detail) to skip the video-info call.
        """
        if detail is None:
            detail = await self.get_video(url_or_id)
        info = _pick_caption((detail.get("video") or {}).get("cla_info") or {})
        if not info:
            return None
        url = _caption_url(info)
        if not url:
            return None
        vtt = await self._download_text(url)
        if not vtt:
            return None
        text = webvtt_to_text(vtt)
        if not text:
            return None
        return {
            "transcript": text,
            "lang": info.get("language_code") or info.get("lang"),
            "source": "tiktok_caption",
        }

    async def _download_text(self, url: str) -> Optional[str]:
        """GET an arbitrary URL (e.g. a caption CDN file) as text; None on failure."""
        bases = backoff_schedule(self._max_retries)
        for attempt in range(self._max_retries + 1):
            try:
                status, body = await asyncio.to_thread(
                    _blocking_download, url, self._timeout
                )
            except (urllib.error.URLError, TimeoutError):
                status, body = 0, ""
            if status == 200 and body:
                return body
            if attempt >= self._max_retries:
                return None
            await self._backoff(bases, attempt)
        return None


def _blocking_download(url: str, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": _CAPTION_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def _pick_caption(cla_info: dict) -> Optional[dict]:
    """Choose the best caption entry: original spoken language, WebVTT preferred."""
    infos = cla_info.get("caption_infos") if isinstance(cla_info, dict) else None
    if not isinstance(infos, list):
        return None
    dicts = [i for i in infos if isinstance(i, dict)]
    if not dicts:
        return None
    # prefer the original caption over machine translations
    originals = [
        i for i in dicts
        if i.get("is_original_caption") or i.get("translation_type") in (0, None)
    ]
    pool = originals or dicts
    for i in pool:  # prefer webvtt when a format is stated
        if str(i.get("caption_format", "")).lower() == "webvtt":
            return i
    return pool[0]


def _caption_url(info: dict) -> Optional[str]:
    url = info.get("url")
    if isinstance(url, str) and url.startswith("http"):
        return url
    for u in info.get("url_list") or []:
        if isinstance(u, str) and u.startswith("http"):
            return u
    return None


def _message(body: str) -> str:
    """Best-effort human-readable message out of an error response body."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return (body or "").strip()[:200]
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)[:200]
    return str(payload)[:200]
