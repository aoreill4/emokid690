"""Score comment sentiment -> `comment_sentiment` grain.

Assigns each comment a signed polarity in [-1, 1] and a label
(positive/neutral/negative), so the joke ranking can weight by *how the audience
reacted*, not just how often a bit was mentioned.

Two backends:
  --method vader    (default) VADER — pure-Python, tuned for social media (emoji,
                    ALL CAPS, slang, negation). No heavy deps, instant.
  --method roberta  cardiffnlp/twitter-roberta-base-sentiment-latest via
                    transformers — more nuanced, but needs torch (heavy) and
                    downloads a model on first run.

    python src/score_sentiment.py --client emokid690
    python src/score_sentiment.py --client emokid690 --method roberta

Reads data/<client>/comments/comments.parquet; writes
data/<client>/sentiment/comment_sentiment.parquet. Incremental (skips comments
already scored with the same method; --refresh redoes all).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema  # noqa: E402
import storage  # noqa: E402
from _util import load_env_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# VADER's standard decision thresholds on the compound score.
POS_THRESHOLD = 0.05
NEG_THRESHOLD = -0.05


def label_for(score: float) -> str:
    if score >= POS_THRESHOLD:
        return "positive"
    if score <= NEG_THRESHOLD:
        return "negative"
    return "neutral"


def _paths_for(client: str) -> tuple[Path, Path]:
    base = REPO_ROOT / "data" / client
    return base / "comments" / "comments.parquet", base / "sentiment" / "comment_sentiment.parquet"


def _pending(comments_path: Path, out_path: Path, method: str, refresh: bool):
    """Return (comment_id, video_id, text) for comments still needing a score."""
    comments = storage.read_parquet(comments_path, schema.COMMENT_COLUMNS)
    if comments.empty:
        return []
    done: set[str] = set()
    if not refresh:
        existing = storage.read_parquet(out_path, schema.COMMENT_SENTIMENT_COLUMNS)
        if not existing.empty:
            # only skip comments already scored with the *same* method
            done = {str(r["comment_id"]) for r in existing.to_dict(orient="records")
                    if r.get("method") == method}
    pending = []
    for rec in comments.to_dict(orient="records"):
        cid, text = rec.get("comment_id"), rec.get("text")
        if cid is None or not text:
            continue
        cid = str(cid)
        if cid not in done:
            pending.append((cid, str(rec.get("video_id")), str(text)))
    return pending


def _score_vader(texts: list[str]) -> list[float]:
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    except ImportError:
        raise SystemExit("vaderSentiment not installed. Run: "
                         "python3 -m pip install -r requirements-sentiment.txt")
    analyzer = SentimentIntensityAnalyzer()
    return [analyzer.polarity_scores(t)["compound"] for t in texts]


def _score_roberta(texts: list[str]) -> list[float]:
    try:
        from transformers import pipeline
    except ImportError:
        raise SystemExit("transformers/torch not installed. For the roberta method: "
                         "python3 -m pip install transformers torch")
    clf = pipeline("sentiment-analysis",
                   model="cardiffnlp/twitter-roberta-base-sentiment-latest",
                   top_k=None, truncation=True)
    scores = []
    for out in clf(texts, batch_size=32):
        probs = {d["label"].lower(): d["score"] for d in out}
        # signed polarity = P(positive) - P(negative)
        scores.append(probs.get("positive", 0.0) - probs.get("negative", 0.0))
    return scores


def run(client: str, method: str, refresh: bool, limit: int | None) -> None:
    comments_path, out_path = _paths_for(client)
    pending = _pending(comments_path, out_path, method, refresh)
    if limit:
        pending = pending[:limit]
    if not pending:
        print("No comments need scoring (use --refresh to redo).")
        return

    print(f"Scoring {len(pending)} comment(s) with {method}...")
    texts = [t for _cid, _vid, t in pending]
    scores = _score_vader(texts) if method == "vader" else _score_roberta(texts)

    rows, counts = [], {"positive": 0, "neutral": 0, "negative": 0}
    for (cid, vid, _text), score in zip(pending, scores):
        lab = label_for(score)
        counts[lab] += 1
        rows.append(schema.CommentSentimentRow(
            comment_id=cid, video_id=vid, client=client,
            sentiment=round(float(score), 4), label=lab, method=method,
        ).as_record())

    total = storage.upsert_parquet(
        rows, out_path, schema.COMMENT_SENTIMENT_COLUMNS, schema.COMMENT_SENTIMENT_PK
    )
    print(f"Done: scored {len(rows)} "
          f"(+{counts['positive']} / ~{counts['neutral']} / -{counts['negative']}). "
          f"comment_sentiment table now {total} rows.")


def main() -> None:
    load_env_file(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Score comment sentiment.")
    parser.add_argument("--client", required=True, help="client id / handle")
    parser.add_argument("--method", choices=("vader", "roberta"), default="vader",
                        help="sentiment backend (default: vader — light, no torch)")
    parser.add_argument("--refresh", action="store_true",
                        help="re-score even comments already scored with this method")
    parser.add_argument("--limit", type=int, default=None,
                        help="only score the first N pending comments (for testing)")
    args = parser.parse_args()
    run(args.client, args.method, args.refresh, args.limit)


if __name__ == "__main__":
    main()
