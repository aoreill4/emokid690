"""Column contracts and parsers for the two datasets.

Maps raw TikTok JSON (as returned by TikTokApi) onto our clean, typed columns.
TikTokApi field names drift between library versions and between the web/app
payloads, so every accessor is defensive: it tries several likely keys and
falls back to ``None`` rather than raising. Missing-but-expected fields
(``overlay_text``, ``transcript``) are nullable by design — see the plan's
"Known risks" section.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _first(d: dict, *keys: str) -> Any:
    """Return the first present, non-None value among ``keys`` in ``d``."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch_to_utc(value: Any) -> Optional[datetime]:
    """TikTok createTime is a unix epoch (seconds)."""
    ts = _to_int(value)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def hash_author(username: Any) -> Optional[str]:
    """Anonymize a handle: store a stable hash, never the raw username (PII)."""
    if not username:
        return None
    return hashlib.sha256(str(username).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# video grain
# ---------------------------------------------------------------------------
@dataclass
class VideoRow:
    video_id: str
    client: str
    url: Optional[str]
    created_at: Optional[datetime]
    caption: Optional[str]
    overlay_text: Optional[str]
    transcript: Optional[str]
    hashtags: list[str]
    music: Optional[str]
    duration_s: Optional[int]
    like_count: Optional[int]
    comment_count: Optional[int]
    share_count: Optional[int]
    play_count: Optional[int]
    collected_at: datetime = field(default_factory=_now_utc)

    def as_record(self) -> dict:
        return asdict(self)


VIDEO_COLUMNS = list(VideoRow.__dataclass_fields__.keys())
VIDEO_PK = "video_id"


def _extract_overlay_text(raw: dict) -> Optional[str]:
    """Pull in-video sticker/overlay text if TikTok exposed it.

    Two known shapes:
      raw["stickersOnItem"] -> [{"stickerText": ["...", "..."]}]
      raw["contents"]       -> [{"desc": "..."}]
    Often absent entirely; returns None then (OCR fallback is Phase 2).
    """
    fragments: list[str] = []

    stickers = raw.get("stickersOnItem")
    if isinstance(stickers, list):
        for sticker in stickers:
            texts = (sticker or {}).get("stickerText")
            if isinstance(texts, list):
                fragments.extend(str(t) for t in texts if t)

    contents = raw.get("contents")
    if isinstance(contents, list):
        for content in contents:
            desc = (content or {}).get("desc")
            if desc:
                fragments.append(str(desc))

    joined = "\n".join(f.strip() for f in fragments if f and f.strip())
    return joined or None


def _extract_hashtags(raw: dict) -> list[str]:
    """Collect hashtag names, deduped in order.

    Field names vary by payload source: TikTokApi's web payload uses
    ``textExtra``/``hashtagName``; the native ``aweme`` JSON (what the hosted
    API returns) uses ``text_extra``/``content_desc_extra`` with ``hashtag_name``.
    Try all of them.
    """
    tags: list[str] = []
    seen: set[str] = set()
    for key in ("textExtra", "text_extra", "content_desc_extra"):
        for extra in raw.get(key) or []:
            name = _first(extra or {}, "hashtagName", "hashtag_name")
            if name and str(name) not in seen:
                seen.add(str(name))
                tags.append(str(name))
    return tags


def _canonical_url(video_id: str, author: dict, client: str) -> str:
    unique_id = _first(author or {}, "uniqueId", "unique_id") or client
    return f"https://www.tiktok.com/@{unique_id}/video/{video_id}"


def parse_video(raw: dict, client: str) -> Optional[VideoRow]:
    """Map a raw TikTok item dict onto a VideoRow. None if it has no id."""
    if not isinstance(raw, dict):
        return None
    video_id = _first(raw, "id", "aweme_id")
    if not video_id:
        return None
    video_id = str(video_id)

    # "statistics" is the native-aweme name (hosted API); "stats"/"statsV2" are
    # TikTokApi's web-payload names.
    stats = raw.get("stats") or raw.get("statsV2") or raw.get("statistics") or {}
    video_meta = raw.get("video") or {}
    # native aweme calls the attached sound "added_sound_music_info".
    music = raw.get("music") or raw.get("added_sound_music_info") or {}
    author = raw.get("author") or {}

    return VideoRow(
        video_id=video_id,
        client=client,
        url=_canonical_url(video_id, author, client),
        created_at=_epoch_to_utc(_first(raw, "createTime", "create_time")),
        caption=_first(raw, "desc", "content_desc"),
        overlay_text=_extract_overlay_text(raw),
        transcript=None,  # populated by the subtitle/Whisper fallback in Phase 2
        hashtags=_extract_hashtags(raw),
        music=_first(music, "title"),
        duration_s=_to_int(_first(video_meta, "duration")),
        like_count=_to_int(_first(stats, "diggCount", "digg_count")),
        comment_count=_to_int(_first(stats, "commentCount", "comment_count")),
        share_count=_to_int(_first(stats, "shareCount", "share_count")),
        play_count=_to_int(_first(stats, "playCount", "play_count")),
    )


# ---------------------------------------------------------------------------
# comment grain
# ---------------------------------------------------------------------------
@dataclass
class CommentRow:
    comment_id: str
    video_id: str
    client: str
    text: Optional[str]
    like_count: Optional[int]
    reply_count: Optional[int]
    parent_comment_id: Optional[str]
    author_hash: Optional[str]
    created_at: Optional[datetime]
    collected_at: datetime = field(default_factory=_now_utc)

    def as_record(self) -> dict:
        return asdict(self)


COMMENT_COLUMNS = list(CommentRow.__dataclass_fields__.keys())
COMMENT_PK = "comment_id"


def _extract_parent_comment_id(raw: dict) -> Optional[str]:
    """A reply points at the comment it answers; top-level comments have '0'."""
    pid = _first(raw, "reply_id", "replyId", "reply_to_reply_id", "reply_comment_id")
    if pid in (None, "0", 0):
        return None
    return str(pid)


def parse_comment(raw: dict, video_id: str, client: str) -> Optional[CommentRow]:
    """Map a raw TikTok comment dict onto a CommentRow. None if it has no id."""
    if not isinstance(raw, dict):
        return None
    comment_id = _first(raw, "cid", "id")
    if not comment_id:
        return None

    user = raw.get("user") or {}
    username = _first(user, "unique_id", "uniqueId", "uid") or _first(raw, "uid")

    return CommentRow(
        comment_id=str(comment_id),
        video_id=str(video_id),
        client=client,
        text=_first(raw, "text"),
        like_count=_to_int(_first(raw, "digg_count", "diggCount")),
        reply_count=_to_int(
            _first(raw, "reply_comment_total", "replyCommentTotal", "reply_total")
        ),
        parent_comment_id=_extract_parent_comment_id(raw),
        author_hash=hash_author(username),
        created_at=_epoch_to_utc(_first(raw, "create_time", "createTime")),
    )
