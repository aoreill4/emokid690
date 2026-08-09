"""Score comment sentiment -> `comment_sentiment` grain.

Assigns each comment a signed polarity in [-1, 1] and a label
(positive/neutral/negative), so the joke ranking can weight by *how the audience
reacted*, not just how often a bit was mentioned.

Three backends:
  --method claude   (default) Claude API — context-aware, understands the
                    internet-comedy register (😭/💀 = laughing, profanity =
                    emphasis, playful insults = affection, sarcasm). Batched, so
                    it's cheap. Needs ANTHROPIC_API_KEY (same key as the jokes
                    step). This is the right tool for TikTok comedy comments.
  --method vader    VADER — pure-Python lexicon. Fast and free, but tuned for
                    product/movie reviews: it reads 😭 as "sob" and profanity as
                    hostile, so it *inverts* on comedy comments. Fallback only.
  --method roberta  cardiffnlp/twitter-roberta-base-sentiment-latest via
                    transformers — better than VADER on tweets, but needs torch
                    (heavy) and downloads a model on first run.

    python src/score_sentiment.py --client emokid690                 # claude
    python src/score_sentiment.py --client emokid690 --method vader
    python src/score_sentiment.py --client emokid690 --method roberta

Reads data/<client>/comments/comments.parquet; writes
data/<client>/sentiment/comment_sentiment.parquet. Incremental (skips comments
already scored with the same method; --refresh redoes all). Because the parquet
is keyed by comment_id, switching methods overwrites a comment's prior score
rather than duplicating it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema  # noqa: E402
import storage  # noqa: E402
from _util import load_env_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Decision thresholds on the signed compound/polarity score.
POS_THRESHOLD = 0.05
NEG_THRESHOLD = -0.05

# Claude sentiment defaults. Sonnet balances sarcasm/slang understanding against
# cost; bump to opus for max nuance or drop to haiku for max thrift via --model.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"
CLAUDE_BATCH = 40  # comments per API call

# The whole point of the claude backend: teach it this domain's register so it
# scores *intent*, not dictionary polarity (which is what breaks VADER here).
CLAUDE_SYSTEM_PROMPT = """\
You score the sentiment of comments left on short-form comedy videos (TikTok).
Rate how POSITIVELY each commenter reacted to the video/creator on a scale from
-1.0 to +1.0:
  +1.0  loved it / found it hilarious / strong praise or affection
   0.0  neutral, factual, ambiguous, or just quoting/tagging with no reaction
  -1.0  genuine dislike, insult, disgust, disappointment, real criticism

This is the internet-comedy register. Read INTENT, not dictionary polarity:
- Crying/skull emoji (😭 💀 ⚰️ 😵 🥲) mean "this is hilarious" — strongly
  POSITIVE, not sad.
- "I'm dead", "I'm crying", "dying", "screaming", "this killed me", "I can't",
  "no bc", "STOPP", "help" = laughing hard = POSITIVE.
- Profanity is usually emphasis/praise: "so fucking funny", "this shit is gold",
  "unhinged", "insane", "stupid funny", "crazy", "wild" = POSITIVE.
- Playful insults / ribbing at the creator ("how bored were you", "what is wrong
  with you", "you're so weird", "girl are you okay") are affectionate = mildly
  POSITIVE.
- Praise words: "obsessed", "gold", "iconic", "ate", "viral", "need more",
  "binge", "original", "so real", "fr" = POSITIVE.
- Genuinely NEGATIVE: sincere criticism, "not funny", "cringe", "unfollowing",
  non-playful disgust, hate, "this isn't it".
- Quoting a line from the video, tagging a friend, or asking a neutral question
  with no reaction = near 0.

You receive a numbered list of comments. Return a sentiment number for each,
keyed by its index."""

CLAUDE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "sentiment": {"type": "number"},
                },
                "required": ["index", "sentiment"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["scores"],
    "additionalProperties": False,
}


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


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, float(x)))


def _score_claude(texts: list[str], model: str) -> list[float]:
    """Score comments with Claude, batched. Returns one float in [-1,1] each."""
    try:
        import anthropic
    except ImportError:
        raise SystemExit("anthropic not installed. Run: "
                         "python3 -m pip install -r requirements-jokes.txt")
    try:
        api = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    except Exception as exc:
        raise SystemExit(f"Could not init Anthropic client: {exc}. "
                         "Set ANTHROPIC_API_KEY in .env.")

    scores: list[float] = []
    for start in range(0, len(texts), CLAUDE_BATCH):
        batch = texts[start : start + CLAUDE_BATCH]
        numbered = "\n".join(f"[{i}] {t}" for i, t in enumerate(batch))
        resp = api.messages.create(
            model=model,
            max_tokens=4000,
            system=CLAUDE_SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": CLAUDE_SCHEMA}},
            messages=[{"role": "user", "content":
                       f"Score these {len(batch)} comments:\n\n{numbered}"}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text) if text.strip() else {}
        by_index = {int(s["index"]): _clamp(s["sentiment"])
                    for s in data.get("scores", [])
                    if isinstance(s, dict) and "index" in s and "sentiment" in s}
        # align back to the batch order; default missing entries to neutral.
        scores.extend(by_index.get(i, 0.0) for i in range(len(batch)))
        print(f"  scored {min(start + len(batch), len(texts))}/{len(texts)}")
    return scores


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


def _score(texts: list[str], method: str, model: str) -> list[float]:
    if method == "claude":
        return _score_claude(texts, model)
    if method == "vader":
        return _score_vader(texts)
    return _score_roberta(texts)


def run(client: str, method: str, model: str, refresh: bool, limit: int | None) -> None:
    comments_path, out_path = _paths_for(client)
    pending = _pending(comments_path, out_path, method, refresh)
    if limit:
        pending = pending[:limit]
    if not pending:
        print("No comments need scoring (use --refresh to redo).")
        return

    label = f"{method} ({model})" if method == "claude" else method
    print(f"Scoring {len(pending)} comment(s) with {label}...")
    texts = [t for _cid, _vid, t in pending]
    scores = _score(texts, method, model)

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
    parser.add_argument("--method", choices=("claude", "vader", "roberta"),
                        default="claude",
                        help="sentiment backend (default: claude — context-aware)")
    parser.add_argument("--model", default=DEFAULT_CLAUDE_MODEL,
                        help=f"Claude model id for --method claude "
                             f"(default: {DEFAULT_CLAUDE_MODEL})")
    parser.add_argument("--refresh", action="store_true",
                        help="re-score even comments already scored with this method")
    parser.add_argument("--limit", type=int, default=None,
                        help="only score the first N pending comments (for testing)")
    args = parser.parse_args()
    run(args.client, args.method, args.model, args.refresh, args.limit)


if __name__ == "__main__":
    main()
