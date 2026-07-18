#!/usr/bin/env python3
"""Smoke-test the ScrapeCreators TikTok API before wiring it into the pipeline.

Run this on a machine with normal internet access. **It will not work in the
Claude Code web sandbox** — that environment's network policy blocks outbound
requests to ``api.scrapecreators.com`` (same reason the scraper can't reach
tiktok.com there). Run it on your laptop.

What it does: spends a handful of free credits hitting the three endpoints the
ingestion pipeline needs, and prints the *shape* of each JSON response (field
paths + value types). That proves the key works and shows exactly which fields
we'll map into ``schema.py`` when building the real ScrapeCreators backend.

Setup
-----
1. Sign up free at https://scrapecreators.com (100 credits, no card).
2. Put the key in ``.env`` at the repo root (it's gitignored — never commit it):

       SCRAPECREATORS_API_KEY=your_key_here

   (or ``export SCRAPECREATORS_API_KEY=...`` in your shell instead).
3. Run:

       python scripts/test_scrapecreators.py --handle emokid690

   Add ``--video-url "https://www.tiktok.com/@emokid690/video/<id>"`` to test a
   specific video instead of the profile's newest one.

Endpoints exercised (each costs ~1 credit):
  * GET /v3/tiktok/profile/videos?handle=<handle>     -> creator's videos
  * GET /v2/tiktok/video?url=<video_url>              -> single video info
  * GET /v1/tiktok/video/comments?url=<video_url>     -> a page of comments
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

BASE = "https://api.scrapecreators.com"
REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Load .env if present; never let a bad .env crash the test.

    A missing .env is fine (real environment variables win). A *malformed* one is
    also non-fatal: on Windows, PowerShell's ``>>`` writes UTF-16, which trips
    python-dotenv's UTF-8 reader — we warn and fall back to the environment
    rather than dying before the first request.
    """
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    try:
        load_dotenv(REPO_ROOT / ".env")
    except Exception as exc:
        print(f"warning: ignoring unreadable .env ({exc}); "
              f"using environment variables instead", file=sys.stderr)


def get(path: str, params: dict, api_key: str, timeout: int = 60) -> tuple[int, Any]:
    """GET a ScrapeCreators endpoint. Returns (http_status, parsed_json_or_error)."""
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"x-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)
        except Exception:
            pass
        return exc.code, {"error": detail}
    except urllib.error.URLError as exc:
        return 0, {"error": f"connection failed: {exc.reason}"}


def sketch(obj: Any, prefix: str = "", depth: int = 0, max_depth: int = 4,
           max_keys: int = 40) -> None:
    """Print a compact map of a JSON structure: field paths -> value type.

    Recurses into dicts and the first element of lists so we see the shape of a
    representative item without dumping the whole payload.
    """
    if depth > max_depth:
        print(f"{prefix}: ...")
        return
    if isinstance(obj, dict):
        for i, (k, v) in enumerate(obj.items()):
            if i >= max_keys:
                print(f"{prefix}.… ({len(obj) - max_keys} more keys)")
                break
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)) and v:
                sketch(v, path, depth + 1, max_depth, max_keys)
            else:
                print(f"{path}: {_typename(v)}")
    elif isinstance(obj, list):
        print(f"{prefix}: list[{len(obj)}]")
        if obj:
            sketch(obj[0], f"{prefix}[0]", depth + 1, max_depth, max_keys)
    else:
        print(f"{prefix}: {_typename(obj)}")


def _typename(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, str):
        preview = v[:60].replace("\n", " ")
        return f"str ({preview!r})" if v else "str (empty)"
    return type(v).__name__


def _find_first_video_url(profile_json: Any, handle: str) -> Optional[str]:
    """Best-effort dig for a video id/url in the profile-videos payload.

    The exact response shape is what we're here to discover, so try a few common
    layouts and fall back to None (the script still reports what it got).
    """
    candidates = []
    if isinstance(profile_json, dict):
        for key in ("aweme_list", "videos", "itemList", "data", "items"):
            val = profile_json.get(key)
            if isinstance(val, list):
                candidates = val
                break
    for item in candidates:
        if not isinstance(item, dict):
            continue
        vid = item.get("aweme_id") or item.get("id") or item.get("video_id")
        if vid:
            return f"https://www.tiktok.com/@{handle}/video/{vid}"
    return None


def section(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handle", default="emokid690",
                        help="TikTok handle to test (default: emokid690)")
    parser.add_argument("--video-url",
                        help="specific video URL to test (else the profile's newest)")
    parser.add_argument("--comment-cursor", type=int, default=0)
    args = parser.parse_args()

    api_key = os.getenv("SCRAPECREATORS_API_KEY")
    if not api_key:
        print("ERROR: set SCRAPECREATORS_API_KEY in .env or the environment.",
              file=sys.stderr)
        print("Sign up free at https://scrapecreators.com (100 credits, no card).",
              file=sys.stderr)
        return 2

    # 1) Profile videos --------------------------------------------------
    section(f"1. Profile videos  (GET /v3/tiktok/profile/videos?handle={args.handle})")
    status, profile = get("/v3/tiktok/profile/videos", {"handle": args.handle}, api_key)
    print(f"HTTP {status}")
    if status != 200:
        print(json.dumps(profile, indent=2)[:1500])
        print("\nStopping — fix the key/plan and re-run.")
        return 1
    sketch(profile)

    video_url = args.video_url or _find_first_video_url(profile, args.handle)
    if not video_url:
        print("\nCould not auto-pick a video URL from the profile response above.")
        print("Re-run with --video-url to continue the video/comment checks.")
        return 0
    print(f"\n-> Using video: {video_url}")

    # 2) Single video info ----------------------------------------------
    section("2. Video info  (GET /v2/tiktok/video?url=...)")
    status, video = get("/v2/tiktok/video", {"url": video_url}, api_key)
    print(f"HTTP {status}")
    sketch(video if status == 200 else video)

    # 3) Comments (first page) ------------------------------------------
    section("3. Comments  (GET /v1/tiktok/video/comments?url=...)")
    status, comments = get(
        "/v1/tiktok/video/comments",
        {"url": video_url, "cursor": args.comment_cursor},
        api_key,
    )
    print(f"HTTP {status}")
    sketch(comments if status == 200 else comments)

    section("Done")
    print("If all three returned HTTP 200 with sensible fields, the key works and")
    print("we can build the ScrapeCreators backend mapping these fields to schema.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
