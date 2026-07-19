"""Segment transcripts into a `jokes` grain using the Claude API.

For each transcript that hasn't been segmented yet, this runs the
`identify-jokes` skill's prompt through Claude and stores one row per joke
(video_id, joke_index, joke_text, punchline, theme). The skill's SKILL.md is the
single source of truth for the prompt, so tuning the skill automatically changes
this step's behaviour.

    python src/segment_jokes.py --client emokid690            # only missing ones
    python src/segment_jokes.py --client emokid690 --limit 3  # try a few first
    python src/segment_jokes.py --client emokid690 --refresh  # redo all

Setup: put an ANTHROPIC_API_KEY in .env (platform.claude.com). Reads transcripts
from data/<client>/transcript/transcript.parquet; writes
data/<client>/jokes/jokes.parquet. Push to Supabase with sync_supabase.py.

Cost: one Claude call per transcript (~a few cents total for a full backfill on
the default model).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema  # noqa: E402
import storage  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "identify-jokes" / "SKILL.md"
DEFAULT_MODEL = "claude-opus-4-8"

# Structured-output schema so the model returns clean, parseable JSON.
JOKE_SCHEMA = {
    "type": "object",
    "properties": {
        "premise": {"type": "string"},
        "jokes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "joke_index": {"type": "integer"},
                    "joke_text": {"type": "string"},
                    "punchline": {"type": "string"},
                    "theme": {"type": "string"},
                },
                "required": ["joke_index", "joke_text", "punchline", "theme"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["premise", "jokes"],
    "additionalProperties": False,
}


def _paths_for(client: str) -> tuple[Path, Path]:
    base = REPO_ROOT / "data" / client
    return base / "transcript" / "transcript.parquet", base / "jokes" / "jokes.parquet"


def _skill_system_prompt() -> str:
    """Use the identify-jokes SKILL.md body (minus YAML frontmatter) as the prompt."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)  # frontmatter, meta, body
        if len(parts) == 3:
            text = parts[2]
    return text.strip()


def _pending(transcript_path: Path, jokes_path: Path, refresh: bool) -> list[tuple[str, str]]:
    """Return (video_id, transcript) pairs that still need segmenting."""
    transcripts = storage.read_parquet(transcript_path, schema.TRANSCRIPT_COLUMNS)
    if transcripts.empty:
        return []
    done: set[str] = set()
    if not refresh:
        existing = storage.read_parquet(jokes_path, schema.JOKE_COLUMNS)
        if not existing.empty:
            done = {str(v) for v in existing["video_id"].tolist()}
    pending: list[tuple[str, str]] = []
    for rec in transcripts.to_dict(orient="records"):
        vid = rec.get("video_id")
        text = rec.get("transcript")
        if vid is None or not text:
            continue
        vid = str(vid)
        if vid not in done:
            pending.append((vid, str(text)))
    return pending


def _segment(client, model: str, system_prompt: str, transcript: str) -> list[dict]:
    """Call Claude to split one transcript into jokes; returns the jokes list."""
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=system_prompt,
        output_config={"format": {"type": "json_schema", "schema": JOKE_SCHEMA}},
        messages=[{"role": "user", "content": transcript}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(text) if text.strip() else {}
    jokes = data.get("jokes")
    return jokes if isinstance(jokes, list) else []


def run(client: str, model: str, refresh: bool, limit: int | None) -> None:
    transcript_path, jokes_path = _paths_for(client)
    pending = _pending(transcript_path, jokes_path, refresh)
    if limit:
        pending = pending[:limit]
    if not pending:
        print("No transcripts need segmenting (use --refresh to redo).")
        return

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

    system_prompt = _skill_system_prompt()
    print(f"Segmenting {len(pending)} transcript(s) with {model}...")
    total_jokes, total_rows, failed = 0, 0, 0
    for i, (vid, transcript) in enumerate(pending, 1):
        try:
            jokes = _segment(api, model, system_prompt, transcript)
        except Exception as exc:  # keep going; one bad transcript shouldn't stop the run
            print(f"  [{i}/{len(pending)}] {vid}: error ({exc})")
            failed += 1
            continue
        rows = []
        for j in jokes:
            idx = j.get("joke_index")
            if idx is None:
                continue
            rows.append(schema.JokeRow(
                joke_id=f"{vid}-{idx}",
                video_id=vid,
                client=client,
                joke_index=int(idx),
                joke_text=j.get("joke_text"),
                punchline=j.get("punchline"),
                theme=j.get("theme"),
            ).as_record())
        if rows:
            total_rows = storage.upsert_parquet(
                rows, jokes_path, schema.JOKE_COLUMNS, schema.JOKE_PK
            )
        total_jokes += len(rows)
        print(f"  [{i}/{len(pending)}] {vid}: {len(rows)} jokes")

    print(f"\nDone: {total_jokes} jokes from {len(pending) - failed} transcript(s) "
          f"({failed} failed). Jokes table now {total_rows} rows.")


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Segment transcripts into a jokes table.")
    parser.add_argument("--client", required=True, help="client id / handle")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Claude model id (default: {DEFAULT_MODEL})")
    parser.add_argument("--refresh", action="store_true",
                        help="re-segment even transcripts already in the jokes table")
    parser.add_argument("--limit", type=int, default=None,
                        help="only process the first N pending transcripts (for testing)")
    args = parser.parse_args()
    run(args.client, args.model, args.refresh, args.limit)


if __name__ == "__main__":
    main()
