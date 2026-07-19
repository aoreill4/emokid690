"""Fill the transcript grain from TikTok's auto-captions (free — no Whisper).

For each of a client's videos that doesn't have a transcript yet, this fetches
the video's caption (WebVTT) via ScrapeCreators, flattens it to plain text, and
upserts a `transcript` row. Videos with no caption are reported as gaps — a
Whisper fallback could fill those later.

    python src/transcribe.py --client emokid690            # only missing ones
    python src/transcribe.py --client emokid690 --refresh  # redo all
    python src/transcribe.py --client emokid690 --limit 5  # try a handful first

Reads video ids from data/<client>/video/video.parquet; writes
data/<client>/transcript/transcript.parquet. Push to Supabase with
sync_supabase.py. Costs ~1 API credit per video (the video-info call); the
caption download itself is free.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema  # noqa: E402
import storage  # noqa: E402
from scrapecreators_client import ScrapeCreatorsClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _paths_for(client: str) -> tuple[Path, Path]:
    base = REPO_ROOT / "data" / client
    return base / "video" / "video.parquet", base / "transcript" / "transcript.parquet"


def _pending(video_path: Path, transcript_path: Path, refresh: bool) -> list[tuple[str, str]]:
    """Return (video_id, url) pairs that still need a transcript."""
    videos = storage.read_parquet(video_path, schema.VIDEO_COLUMNS)
    if videos.empty:
        return []
    have: set[str] = set()
    if not refresh:
        existing = storage.read_parquet(transcript_path, schema.TRANSCRIPT_COLUMNS)
        if not existing.empty:
            have = {str(v) for v in existing["video_id"].tolist()}
    pending: list[tuple[str, str]] = []
    for rec in videos.to_dict(orient="records"):
        vid = rec.get("video_id")
        if vid is None:
            continue
        vid = str(vid)
        if vid not in have:
            pending.append((vid, rec.get("url") or vid))
    return pending


async def run(client: str, handle: str, refresh: bool, limit: int | None) -> None:
    video_path, transcript_path = _paths_for(client)
    pending = _pending(video_path, transcript_path, refresh)
    if limit:
        pending = pending[:limit]
    if not pending:
        print("No videos need transcripts (use --refresh to redo).")
        return

    print(f"Transcribing {len(pending)} video(s) for @{handle}...")
    got, gaps, total_rows = 0, 0, 0
    async with ScrapeCreatorsClient(handle=handle) as tt:
        for i, (vid, url) in enumerate(pending, 1):
            try:
                result = await tt.get_transcript(url)
            except Exception as exc:  # keep going; one bad video shouldn't stop the run
                print(f"  [{i}/{len(pending)}] {vid}: error ({exc})")
                gaps += 1
                continue
            if not result:
                print(f"  [{i}/{len(pending)}] {vid}: no caption")
                gaps += 1
                continue
            row = schema.TranscriptRow(
                video_id=vid,
                client=client,
                transcript=result["transcript"],
                lang=result.get("lang"),
                source=result.get("source"),
            )
            total_rows = storage.upsert_parquet(
                [row.as_record()], transcript_path,
                schema.TRANSCRIPT_COLUMNS, schema.TRANSCRIPT_PK,
            )
            got += 1
            print(f"  [{i}/{len(pending)}] {vid}: {len(result['transcript'])} chars "
                  f"({result.get('lang')})")

    covered = f"{got}/{got + gaps}" if (got + gaps) else "0/0"
    print(f"\nDone: transcribed {got}, no caption {gaps} ({covered} covered). "
          f"Transcript table now {total_rows} rows.")


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Fetch transcripts from TikTok captions.")
    parser.add_argument("--client", required=True, help="client id / handle")
    parser.add_argument("--refresh", action="store_true",
                        help="re-fetch transcripts even for videos that already have one")
    parser.add_argument("--limit", type=int, default=None,
                        help="only process the first N pending videos (for testing)")
    args = parser.parse_args()
    handle = args.client  # storage dir + video urls are keyed by handle
    asyncio.run(run(args.client, handle, args.refresh, args.limit))


if __name__ == "__main__":
    main()
