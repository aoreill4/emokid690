"""Orchestrator: pull TikTok data into the video + comment parquet tables.

Vertical slice (this phase):
    python src/loader.py --client emokid690 --video-url <url>

Whole profile (uses the handle from data/clients.csv):
    python src/loader.py --client emokid690 --all --max-videos 30

Both write to data/<client>/video/video.parquet and
data/<client>/comments/comments.parquet, deduped by primary key.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# allow "python src/loader.py" and "python -m src.loader" both to work
sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema  # noqa: E402
import storage  # noqa: E402

# NOTE: the fetch backends are imported lazily in make_fetcher() — TikTokClient
# pulls in TikTokApi/Playwright, which the ScrapeCreators path doesn't need, so
# a ScrapeCreators-only user never has to install that heavy stack.


REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENTS_CSV = REPO_ROOT / "data" / "clients.csv"


def _paths_for(client: str) -> tuple[Path, Path]:
    base = REPO_ROOT / "data" / client
    return base / "video" / "video.parquet", base / "comments" / "comments.parquet"


def collect_proxies(cli_proxies: Optional[list[str]]) -> list[str]:
    """Merge proxies from the env (``TIKTOK_PROXIES``) and ``--proxy`` flags.

    ``TIKTOK_PROXIES`` is a comma- and/or newline-separated list; ``--proxy`` may
    be repeated. Order is env-first then CLI, blanks dropped, duplicates removed
    (first occurrence wins). Parsing/validation happens in TikTokClient.
    """
    env_raw = os.getenv("TIKTOK_PROXIES", "")
    env_proxies = [p.strip() for p in re.split(r"[,\n]", env_raw)]
    merged = [*env_proxies, *(cli_proxies or [])]
    seen: set[str] = set()
    out: list[str] = []
    for p in merged:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _handle_from_url(url: str) -> Optional[str]:
    url = (url or "").rstrip("/")
    return url.rsplit("@", 1)[-1] if "@" in url else None


def resolve_handle(identifier: str) -> str:
    """Resolve a client id OR a TikTok handle to the handle used as storage key.

    ``data/clients.csv`` keys clients by name (e.g. ``ashleigh``) but the data
    directory and row ``client`` field key by handle (e.g. ``emokid690``). We
    accept either form on the CLI and always return the handle.
    """
    if not CLIENTS_CSV.exists():
        raise FileNotFoundError(f"missing registry: {CLIENTS_CSV}")
    with CLIENTS_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            handle = _handle_from_url(row.get("tiktok", ""))
            if identifier in (row.get("client"), handle) and handle:
                return handle
    raise KeyError(f"{identifier!r} not found in {CLIENTS_CSV} (client or handle)")


async def ingest_video(
    fetcher: Any,
    client: str,
    handle: str,
    url_or_id: str,
    comment_count: int,
    include_replies: bool = False,
) -> dict:
    """Fetch one video + its comments and upsert both tables.

    ``fetcher`` is any backend implementing the get_video / iter_comments /
    iter_user_videos async-context-manager surface (TikTokClient or
    ScrapeCreatorsClient). ``include_replies`` also pulls reply threads (hosted
    API only).
    """
    video_path, comments_path = _paths_for(client)

    async with fetcher as tt:
        raw_video = await tt.get_video(url_or_id)
        video_row = schema.parse_video(raw_video, client)
        if video_row is None:
            raise RuntimeError(f"no usable video payload for {url_or_id!r}")

        comment_rows = []
        async for raw_comment in tt.iter_comments(
            url_or_id, count=comment_count, include_replies=include_replies
        ):
            row = schema.parse_comment(raw_comment, video_row.video_id, client)
            if row is not None:
                comment_rows.append(row.as_record())

    video_total = storage.upsert_parquet(
        [video_row.as_record()], video_path, schema.VIDEO_COLUMNS, schema.VIDEO_PK
    )
    comment_total = storage.upsert_parquet(
        comment_rows, comments_path, schema.COMMENT_COLUMNS, schema.COMMENT_PK
    )
    return {
        "video_id": video_row.video_id,
        "caption": video_row.caption,
        "overlay_text_present": video_row.overlay_text is not None,
        "transcript_present": video_row.transcript is not None,
        "reported_comment_count": video_row.comment_count,
        "comments_collected": len(comment_rows),
        "video_rows_total": video_total,
        "comment_rows_total": comment_total,
    }


async def ingest_all(
    fetcher: Any,
    client: str,
    handle: str,
    max_videos: int,
    comment_count: int,
    include_replies: bool = False,
) -> list[dict]:
    """Sweep a creator's recent videos, ingesting each with its comments."""
    video_path, comments_path = _paths_for(client)
    summaries: list[dict] = []

    async with fetcher as tt:
        video_ids: list[str] = []
        async for raw_video in tt.iter_user_videos(handle, count=max_videos):
            row = schema.parse_video(raw_video, client)
            if row is None:
                continue
            storage.upsert_parquet(
                [row.as_record()], video_path, schema.VIDEO_COLUMNS, schema.VIDEO_PK
            )
            video_ids.append(row.video_id)

        for vid in video_ids:
            comment_rows = []
            async for raw_comment in tt.iter_comments(
                vid, count=comment_count, include_replies=include_replies
            ):
                c = schema.parse_comment(raw_comment, vid, client)
                if c is not None:
                    comment_rows.append(c.as_record())
            total = storage.upsert_parquet(
                comment_rows, comments_path, schema.COMMENT_COLUMNS, schema.COMMENT_PK
            )
            summaries.append(
                {"video_id": vid, "comments_collected": len(comment_rows),
                 "comment_rows_total": total}
            )
    return summaries


def make_fetcher(args: argparse.Namespace, handle: str) -> Any:
    """Build the fetch backend selected by --source (imported lazily).

    Returns an async context manager exposing get_video / iter_comments /
    iter_user_videos. Kept lazy so `--source scrapecreators` doesn't require
    TikTokApi/Playwright to be installed, and vice versa.
    """
    if args.source == "scrapecreators":
        from scrapecreators_client import ScrapeCreatorsClient  # noqa: E402

        return ScrapeCreatorsClient(
            handle=handle,
            min_delay=args.min_delay,
            max_delay=args.max_delay,
        )

    from tiktok_client import TikTokClient  # noqa: E402

    return TikTokClient(
        ms_token=os.getenv("MS_TOKEN") or None,
        pool_size=args.pool_size,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        recycle_after=args.recycle_after,
        proxies=collect_proxies(args.proxy),
    )


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Pull TikTok data into parquet tables.")
    parser.add_argument("--client", required=True, help="client id in data/clients.csv")
    parser.add_argument("--video-url", help="single video URL or id (vertical slice)")
    parser.add_argument("--all", action="store_true", help="sweep the creator's profile")
    parser.add_argument("--max-videos", type=int, default=30)
    parser.add_argument("--comment-count", type=int, default=200,
                        help="max top-level comments per video")
    parser.add_argument("--with-replies", action="store_true",
                        help="also fetch reply threads under each comment "
                             "(scrapecreators only; spends extra credits)")
    parser.add_argument("--source", choices=("scrapecreators", "tiktok"),
                        default="scrapecreators",
                        help="fetch backend: 'scrapecreators' (hosted API, "
                             "recommended) or 'tiktok' (local scraper). Default: "
                             "scrapecreators.")
    # pacing knobs. Default depends on --source: the local scraper paces 2-4s to
    # avoid bot-blocking; the hosted API needs no client-side pacing (0s).
    parser.add_argument("--min-delay", type=float, default=None,
                        help="minimum seconds paced between requests")
    parser.add_argument("--max-delay", type=float, default=None,
                        help="maximum seconds paced between requests")
    # scraper-only anti-blocking knobs (ignored when --source scrapecreators).
    parser.add_argument("--pool-size", type=int, default=2,
                        help="[tiktok] warm sessions kept alive and reused")
    parser.add_argument("--recycle-after", type=int, default=50,
                        help="[tiktok] requests before rotating fingerprint + pool")
    parser.add_argument("--proxy", action="append", metavar="URL",
                        help="[tiktok] proxy URL (scheme://[user:pass@]host:port); "
                             "repeatable. Also read from the TIKTOK_PROXIES env var.")
    args = parser.parse_args()

    # resolve source-dependent pacing defaults
    if args.min_delay is None:
        args.min_delay = 0.0 if args.source == "scrapecreators" else 2.0
    if args.max_delay is None:
        args.max_delay = 0.0 if args.source == "scrapecreators" else 4.0

    handle = resolve_handle(args.client)  # storage + row key is the handle
    print(f"Fetching via {args.source} backend.")

    if args.with_replies and args.source != "scrapecreators":
        print("note: --with-replies is only supported by --source scrapecreators; "
              "ignoring it for this run.")
    include_replies = args.with_replies and args.source == "scrapecreators"

    if args.all:
        results = asyncio.run(
            ingest_all(make_fetcher(args, handle), handle, handle,
                       args.max_videos, args.comment_count, include_replies)
        )
        print(f"Ingested {len(results)} videos for @{handle}:")
        for r in results:
            print(f"  {r['video_id']}: {r['comments_collected']} comments "
                  f"(table now {r['comment_rows_total']} rows)")
    elif args.video_url:
        result = asyncio.run(
            ingest_video(make_fetcher(args, handle), handle, handle,
                         args.video_url, args.comment_count, include_replies)
        )
        print("Ingested one video:")
        for k, v in result.items():
            print(f"  {k}: {v}")
    else:
        parser.error("provide --video-url <url> or --all")


if __name__ == "__main__":
    main()
