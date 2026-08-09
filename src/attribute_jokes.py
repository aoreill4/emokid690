"""Attribute comments to the jokes they reference -> `joke_comment` grain.

For each comment, decide which joke (from the *same video*) it's reacting to,
using two tiers:

  1. Phrase match  — the comment quotes a run of consecutive words from a joke's
     text/punchline (e.g. "a wandering nose"). High confidence.
  2. Keyword match — the comment shares distinctive content words with a joke,
     weighted by how rare each word is across that video's jokes (so "Petsmart"
     counts, "dog" barely does). Medium confidence.

Comments that reference no specific joke are left unattributed — that's the
common case and the correct outcome. Matching is scoped per video: a comment on
video X can only match a joke from video X.

Pure Python, no API cost. Output feeds the sentiment/ranking step:
  weighted_score = Σ(comment_sentiment × max(likes, 1)) grouped by joke.

    python src/attribute_jokes.py --client emokid690

Reads data/<client>/{jokes,comments}/*.parquet; writes
data/<client>/joke_comment/joke_comment.parquet.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema  # noqa: E402
import storage  # noqa: E402
from _util import load_env_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Matching knobs
MIN_PHRASE_LEN = 3          # a quoted run must be at least this many consecutive words
MIN_PHRASE_CONTENT = 2      # ...and contain at least this many non-filler words
# A keyword match needs >=1 word that is genuinely distinctive: it appears in at
# most this fraction of ALL jokes (with a floor of 1, so a word unique to a single
# joke always qualifies). Scale-invariant — 5% whether the catalog is 40 jokes or
# 4,000. "petsmart"/"wandering" (unique) qualify; "day"/"dog"/"park" (recurring)
# don't, so they can't trigger a match on their own.
DISTINCTIVE_DF_FRACTION = 0.05

_TOKEN_RE = re.compile(r"[a-z0-9']+")

# Filler: English function words + TikTok comment noise. Kept modest on purpose —
# the rarity weighting handles the rest (common words score ~0 anyway).
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "so", "as", "of", "at", "by",
    "for", "in", "into", "on", "to", "up", "with", "from", "out", "off", "over",
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did",
    "have", "has", "had", "having", "will", "would", "can", "could", "should",
    "shall", "may", "might", "must", "i", "me", "my", "we", "us", "our", "you",
    "your", "he", "him", "his", "she", "her", "it", "its", "they", "them", "their",
    "this", "that", "these", "those", "who", "what", "which", "when", "where",
    "why", "how", "not", "no", "yes", "here", "there", "then", "than", "too",
    "very", "just", "really", "so", "such", "more", "most", "some", "any", "all",
    "get", "got", "im", "u", "ur", "dont", "cant", "thats", "youre", "gonna",
    "wanna", "like", "omg", "lol", "lmao", "lmaooo", "haha", "hahaha", "dead",
    "literally", "actually", "bro", "girl", "yall", "y'all", "af", "fr", "ngl",
    "pov", "part", "video", "one", "way", "well", "now", "still", "also", "even",
    "much", "many", "make", "made", "know", "think", "see", "say", "said", "go",
    "going", "about", "because", "cause", "coz",
}


def tokenize(text) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(str(text).lower())


def content_tokens(tokens) -> list[str]:
    """Drop filler and very short tokens — what's left carries the meaning."""
    return [t for t in tokens if len(t) > 2 and t not in STOPWORDS]


def longest_common_run(a: list[str], b: list[str]) -> tuple[int, tuple]:
    """Longest run of consecutive tokens shared by lists a and b (length, span)."""
    if not a or not b:
        return 0, ()
    prev = [0] * (len(b) + 1)
    best_len, best_end = 0, 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best_len:
                    best_len, best_end = cur[j], i
        prev = cur
    span = tuple(a[best_end - best_len:best_end]) if best_len else ()
    return best_len, span


def index_video(jokes: list[tuple]) -> list[dict]:
    """Build per-joke token data (ordered seq for phrase match, content set for
    keyword match) for one video's (joke_id, joke_text, punchline) triples."""
    indexed = []
    for jid, text, punch in jokes:
        seq = tokenize(f"{text or ''} {punch or ''}")
        indexed.append({"joke_id": jid, "seq": seq, "content": set(content_tokens(seq))})
    return indexed


def corpus_weights(all_jokes: list[tuple]) -> tuple[dict, set]:
    """Return (weights, distinctive) computed across the WHOLE joke corpus.

    - weights[t] = log(N/df): rarer words score higher (used to rank/score).
    - distinctive = words appearing in <= max(1, 5% of N) jokes — the gate for a
      keyword match, so recurring words ('dog'/'park'/'day') can't trigger one.
    Global (not per-video) so a word common across her catalog is treated as such.
    """
    df, n = {}, 0
    for _jid, text, punch in all_jokes:
        n += 1
        for t in set(content_tokens(tokenize(f"{text or ''} {punch or ''}"))):
            df[t] = df.get(t, 0) + 1
    if n == 0:
        return {}, set()
    cutoff = max(1, round(DISTINCTIVE_DF_FRACTION * n))
    weights = {t: math.log(n / c) for t, c in df.items()}
    distinctive = {t for t, c in df.items() if c <= cutoff}
    return weights, distinctive


def _content_count(span) -> int:
    return sum(1 for t in span if len(t) > 2 and t not in STOPWORDS)


def attribute_comment(text, indexed: list[dict], weights: dict, distinctive: set):
    """Return the best joke match for a comment, or None. See module docstring."""
    ctoks = tokenize(text)
    if not ctoks:
        return None

    # Tier 1: quoted phrase
    best = None  # (length, joke_id, span)
    for j in indexed:
        length, span = longest_common_run(ctoks, j["seq"])
        if length >= MIN_PHRASE_LEN and _content_count(span) >= MIN_PHRASE_CONTENT:
            if best is None or length > best[0]:
                best = (length, j["joke_id"], span)
    if best:
        return {"joke_id": best[1], "method": "phrase",
                "confidence": round(min(0.99, 0.80 + 0.05 * best[0]), 3),
                "matched_text": " ".join(best[2])}

    # Tier 2: distinctive-keyword overlap — needs >=1 corpus-rare shared word.
    cset = set(content_tokens(ctoks))
    best_kw = None  # (score, joke_id, qualifying_words)
    for j in indexed:
        qualifying = [t for t in (cset & j["content"]) if t in distinctive]
        if not qualifying:
            continue
        score = sum(weights[t] for t in qualifying)
        if best_kw is None or score > best_kw[0]:
            best_kw = (score, j["joke_id"], qualifying)
    if best_kw:
        return {"joke_id": best_kw[1], "method": "keyword",
                "confidence": round(min(0.75, 0.45 + 0.08 * len(best_kw[2])), 3),
                "matched_text": " ".join(sorted(best_kw[2]))}
    return None


def _paths_for(client: str) -> tuple[Path, Path, Path]:
    base = REPO_ROOT / "data" / client
    return (base / "jokes" / "jokes.parquet",
            base / "comments" / "comments.parquet",
            base / "joke_comment" / "joke_comment.parquet")


def run(client: str) -> None:
    jokes_path, comments_path, out_path = _paths_for(client)
    jokes_df = storage.read_parquet(jokes_path, schema.JOKE_COLUMNS)
    comments_df = storage.read_parquet(comments_path, schema.COMMENT_COLUMNS)
    if jokes_df.empty:
        print("No jokes yet — run segment_jokes.py first.")
        return
    if comments_df.empty:
        print("No comments yet — run the loader (without --comment-count 0) first.")
        return

    # group jokes by video; index each video's jokes; weight words corpus-wide
    by_video: dict[str, list[tuple]] = {}
    all_jokes: list[tuple] = []
    for rec in jokes_df.to_dict(orient="records"):
        vid = rec.get("video_id")
        if vid is None:
            continue
        triple = (str(rec["joke_id"]), rec.get("joke_text"), rec.get("punchline"))
        by_video.setdefault(str(vid), []).append(triple)
        all_jokes.append(triple)
    index_cache = {vid: index_video(js) for vid, js in by_video.items()}
    weights, distinctive = corpus_weights(all_jokes)

    rows, considered, phrase, keyword = [], 0, 0, 0
    for rec in comments_df.to_dict(orient="records"):
        vid = str(rec.get("video_id"))
        cid = rec.get("comment_id")
        if cid is None or vid not in index_cache:
            continue  # only comments on videos that have jokes are matchable
        considered += 1
        match = attribute_comment(rec.get("text"), index_cache[vid], weights, distinctive)
        if not match:
            continue
        phrase += match["method"] == "phrase"
        keyword += match["method"] == "keyword"
        rows.append(schema.JokeCommentRow(
            comment_id=str(cid), joke_id=match["joke_id"], video_id=vid, client=client,
            method=match["method"], confidence=match["confidence"],
            matched_text=match["matched_text"],
        ).as_record())

    total = storage.upsert_parquet(
        rows, out_path, schema.JOKE_COMMENT_COLUMNS, schema.JOKE_COMMENT_PK
    )
    attributed = len(rows)
    pct = (100.0 * attributed / considered) if considered else 0.0
    print(f"Attributed {attributed}/{considered} comments ({pct:.1f}%) — "
          f"{phrase} by phrase, {keyword} by keyword. "
          f"joke_comment table now {total} rows.")


def main() -> None:
    load_env_file(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Attribute comments to jokes.")
    parser.add_argument("--client", required=True, help="client id / handle")
    args = parser.parse_args()
    run(args.client)


if __name__ == "__main__":
    main()
