"""Ad-hoc DuckDB views over the parquet tables — for verification and quick looks.

    python src/query.py --client emokid690

Prints per-video row counts and a sample join of videos to their comments.
DuckDB reads the parquet files directly; no import/load step.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent


def summarize(client: str) -> None:
    base = REPO_ROOT / "data" / client
    video_glob = str(base / "video" / "*.parquet")
    comments_glob = str(base / "comments" / "*.parquet")

    con = duckdb.connect()
    con.execute(f"CREATE VIEW video AS SELECT * FROM read_parquet('{video_glob}')")
    con.execute(
        f"CREATE VIEW comments AS SELECT * FROM read_parquet('{comments_glob}')"
    )

    n_videos = con.execute("SELECT count(*) FROM video").fetchone()[0]
    n_comments = con.execute("SELECT count(*) FROM comments").fetchone()[0]
    n_replies = con.execute(
        "SELECT count(*) FROM comments WHERE parent_comment_id IS NOT NULL"
    ).fetchone()[0]
    print(f"videos: {n_videos}   comments: {n_comments}  "
          f"(top-level: {n_comments - n_replies}, replies: {n_replies})\n")

    print("per-video: reported vs. collected comments")
    rows = con.execute(
        """
        SELECT v.video_id,
               v.like_count,
               v.comment_count           AS reported_comments,
               count(c.comment_id)        AS collected_comments,
               v.overlay_text IS NOT NULL AS has_overlay,
               v.transcript  IS NOT NULL  AS has_transcript
        FROM video v
        LEFT JOIN comments c USING (video_id)
        GROUP BY 1, 2, 3, 5, 6
        ORDER BY max(v.created_at) DESC NULLS LAST
        """
    ).fetchall()
    for r in rows:
        print(f"  {r[0]}  likes={r[1]}  reported={r[2]}  collected={r[3]}  "
              f"overlay={r[4]}  transcript={r[5]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the parquet tables via DuckDB.")
    parser.add_argument("--client", required=True)
    args = parser.parse_args()
    summarize(args.client)


if __name__ == "__main__":
    main()
