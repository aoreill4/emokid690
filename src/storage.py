"""Parquet read/write with upsert-by-id dedupe.

The source of truth is a single parquet file per grain. Re-running the loader
must not duplicate rows: for a repeated primary key we keep the *newest* record
(latest ``collected_at``) so metric snapshots (likes/comments growing over time)
update in place rather than piling up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd


def _empty_frame(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def read_parquet(path: str | Path, columns: list[str]) -> pd.DataFrame:
    """Load an existing parquet file, or an empty typed frame if absent."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return _empty_frame(columns)
    df = pd.read_parquet(p)
    # tolerate schema drift: guarantee every expected column is present
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df[columns]


def upsert_parquet(
    records: Iterable[dict],
    path: str | Path,
    columns: list[str],
    pk: str,
) -> int:
    """Merge ``records`` into the parquet at ``path``, deduping on ``pk``.

    Returns the total row count of the written file. Newest record wins on a
    primary-key collision (keep last after sorting by collected_at).
    """
    new_df = pd.DataFrame(list(records), columns=columns)
    existing = read_parquet(path, columns)

    # Only concat frames that actually have rows: concatenating an empty frame
    # (e.g. a first run with no existing file) triggers a pandas FutureWarning
    # about empty/all-NA dtype handling.
    frames = [df for df in (existing, new_df) if not df.empty]
    if not frames:
        combined = _empty_frame(columns)
    else:
        combined = pd.concat(frames, ignore_index=True)
        sort_key = "collected_at" if "collected_at" in combined.columns else pk
        combined = (
            combined.sort_values(sort_key, kind="stable")
            .drop_duplicates(subset=[pk], keep="last")
            .reset_index(drop=True)
        )

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(p, index=False)
    return len(combined)
