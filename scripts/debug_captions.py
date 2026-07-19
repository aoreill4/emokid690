#!/usr/bin/env python3
"""Inspect a TikTok video's caption info (cla_info) to see whether TikTok already
has an auto-generated transcript we can grab for free — no Whisper needed.

Run on a machine with internet:
    py scripts/debug_captions.py 7659968907511336222
  or
    py scripts/debug_captions.py --video-url "https://www.tiktok.com/@emokid690/video/<id>"

It fetches one video's info (1 credit), prints the full cla_info block, and — if a
caption file URL is present — downloads it and shows a snippet so we can see the
format and whether it contains real transcript text.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from scrapecreators_client import ScrapeCreatorsClient  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _find_url(d: dict):
    """Return the first http(s) URL found among a dict's values (or nested list)."""
    for v in d.values():
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, list) and v and isinstance(v[0], str) and v[0].startswith("http"):
            return v[0]
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?", help="video id or URL")
    ap.add_argument("--video-url")
    ap.add_argument("--handle", default="emokid690")
    args = ap.parse_args()
    target = args.video_url or args.video
    if not target:
        print("Pass a video id or --video-url")
        return 2

    client = ScrapeCreatorsClient(handle=args.handle)
    detail = await client.get_video(target)
    video = detail.get("video") or {}
    cla = video.get("cla_info") or {}

    print("=== cla_info ===")
    print(json.dumps(cla, indent=2, ensure_ascii=False)[:4000])

    infos = cla.get("caption_infos") or []
    print(f"\ncaption_infos: {len(infos)} entrie(s)")
    for i, info in enumerate(infos):
        if not isinstance(info, dict):
            continue
        print(f"\n-- caption_infos[{i}] keys: {list(info.keys())}")
        print(f"   url -> {str(_find_url(info))[:140]}")

    # Fetch the first caption file we can find and show a snippet + format.
    for info in infos:
        if not isinstance(info, dict):
            continue
        url = _find_url(info)
        if url:
            print("\n=== fetched caption file (first 1500 chars) ===")
            try:
                print(_fetch(url)[:1500])
            except Exception as exc:
                print("could not fetch caption url:", exc)
            break
    else:
        if not infos:
            print("\nNo caption_infos on this video — TikTok has no auto-caption here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
