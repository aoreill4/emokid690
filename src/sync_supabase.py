"""Push the local parquet tables into Supabase (Postgres) via supabase-py.

Decoupled from ingestion: the loader writes parquet (spending API credits);
this reads that parquet and upserts it into Supabase. Re-run it anytime — it's
idempotent (upsert by primary key), so it never duplicates and never re-spends
fetch credits.

Setup
-----
1. Create the tables once: open your Supabase project → SQL Editor → paste and
   run ``db/supabase_schema.sql``.
2. Put credentials in ``.env`` (Supabase dashboard → Project Settings → API):

       SUPABASE_URL=https://<project-ref>.supabase.co
       SUPABASE_KEY=<service_role key>      # server-side key; keep it secret

   Use the **service_role** key, not the anon key — the anon key is blocked by
   row-level security on writes. It's gitignored via ``.env``; never commit it.
3. Install deps and run:

       python3 -m pip install -r requirements-supabase.txt
       python src/sync_supabase.py --client emokid690

Only the ``video`` and ``comments`` parquet for ``--client`` are synced.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema  # noqa: E402
import storage  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CHUNK = 500  # rows per upsert request (keeps payloads well under PostgREST limits)


def _paths_for(client: str) -> tuple[Path, Path]:
    base = REPO_ROOT / "data" / client
    return base / "video" / "video.parquet", base / "comments" / "comments.parquet"


def _clean_value(v):
    """Make one parquet cell JSON-serializable for PostgREST.

    - list/array (hashtags) -> plain list
    - NaN / NaT / None       -> None (SQL NULL)
    - datetime / Timestamp   -> ISO-8601 string (Postgres timestamptz)
    - numpy scalar           -> native Python scalar
    """
    # arrays first: pd.isna() on a list/array is ambiguous and would raise.
    if isinstance(v, (list, tuple)):
        return [x for x in v]
    if hasattr(v, "tolist") and not isinstance(v, (str, bytes, datetime)):
        # numpy ndarray -> list; numpy scalar -> Python scalar (fall through).
        try:
            listed = v.tolist()
        except Exception:
            listed = v
        if isinstance(listed, list):
            return listed
        v = listed

    # Null-ish check BEFORE the datetime branch: pandas NaT is a datetime
    # subclass, and NaT.isoformat() returns the literal "NaT" — we want NULL.
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    try:
        import pandas as pd  # local import so the module loads without pandas
        if not isinstance(v, (list, dict)) and pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _load_records(path: Path, columns: list[str]) -> list[dict]:
    """Read a parquet grain into a list of clean, JSON-serializable dicts."""
    df = storage.read_parquet(path, columns)
    if df.empty:
        return []
    return [
        {k: _clean_value(val) for k, val in row.items()}
        for row in df.to_dict(orient="records")
    ]


def _supabase_client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise SystemExit(
            "Missing SUPABASE_URL / SUPABASE_KEY. Add them to .env "
            "(Supabase dashboard → Project Settings → API; use the service_role key)."
        )
    try:
        from supabase import create_client
    except ImportError:
        raise SystemExit(
            "supabase not installed. Run: "
            "python3 -m pip install -r requirements-supabase.txt"
        )
    return create_client(url, key)


def _upsert(sb, table: str, records: list[dict], on_conflict: str) -> int:
    """Upsert records into ``table`` in chunks, deduping on ``on_conflict``."""
    if not records:
        print(f"  {table}: nothing to sync")
        return 0
    done = 0
    for i in range(0, len(records), CHUNK):
        batch = records[i : i + CHUNK]
        try:
            sb.table(table).upsert(batch, on_conflict=on_conflict).execute()
        except Exception as exc:  # surface a clear, actionable message
            raise SystemExit(
                f"Supabase upsert into '{table}' failed: {exc}\n"
                f"Have you created the tables? Run db/supabase_schema.sql in the "
                f"Supabase SQL editor, and confirm SUPABASE_KEY is the service_role key."
            )
        done += len(batch)
        print(f"  {table}: upserted {done}/{len(records)}")
    return done


def sync(client: str) -> None:
    video_path, comments_path = _paths_for(client)
    sb = _supabase_client()

    print(f"Syncing '{client}' to Supabase...")
    video_records = _load_records(video_path, schema.VIDEO_COLUMNS)
    comment_records = _load_records(comments_path, schema.COMMENT_COLUMNS)

    n_video = _upsert(sb, "video", video_records, schema.VIDEO_PK)
    n_comments = _upsert(sb, "comments", comment_records, schema.COMMENT_PK)
    print(f"Done: {n_video} video rows, {n_comments} comment rows upserted.")


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Sync parquet tables into Supabase.")
    parser.add_argument("--client", required=True, help="client id / handle")
    args = parser.parse_args()
    sync(args.client)


if __name__ == "__main__":
    main()
