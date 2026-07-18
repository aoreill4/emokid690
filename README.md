# emokid690

TikTok Data Engineering and Analytics for **@emokid690** (`ashleigh`).

This repo pulls data from a creator's TikTok page into clean, typed tables and
(later) runs analysis on top: video categorization, per-joke breakdowns, and
comment sentiment. See `plan` notes for the full roadmap.

## What's built so far (Phase 1 — ingestion)

An ingestion pipeline that turns a creator's videos into two datasets:

- **`video`** — one row per creator-video (caption, overlay text, likes,
  comment/share/play counts, hashtags, music, duration).
- **`comments`** — one row per comment (text, likes, replies, reply-linkage,
  anonymized author, timestamp).

Stored as **Parquet** under `data/<client>/`, queried with **DuckDB**.

```
data/
  clients.csv                     # registry: client -> tiktok/instagram handles
  emokid690/
    video/video.parquet
    comments/comments.parquet
src/
  loader.py         # CLI orchestrator (fetch -> parse -> upsert)
  tiktok_client.py  # async wrapper around TikTokApi
  schema.py         # column contracts + parsers for both grains
  storage.py        # parquet upsert-by-id (dedupe, newest wins)
  query.py          # DuckDB inspection / verification
```

## Fetch backends

The loader can pull data two ways, chosen with `--source`:

| `--source` | How it works | Trade-off |
|---|---|---|
| `scrapecreators` **(default, recommended)** | Calls the [ScrapeCreators](https://scrapecreators.com) hosted API over plain HTTPS. They run the scraping + proxies server-side and return TikTok's native JSON. | Needs an API key (100 free credits, then paid — ~$47 for 25k). **No bot-blocking, no browser, runs anywhere.** |
| `tiktok` | Drives a headless Chromium via `TikTokApi` locally. Free, but gets rate-limited/blocked, needs a heavy install, and must run on a machine that can reach tiktok.com. Optional proxy support (see below). | Free but fragile; you maintain it. |

Both backends feed the exact same parsers and tables — only the fetch differs.

## Setup

**Recommended (hosted API):** lighter install — no TikTokApi/Playwright.

```bash
python3 -m pip install -r requirements-scrapecreators.txt
cp .env.example .env      # then paste your SCRAPECREATORS_API_KEY
```

Get a key (100 free credits, no card) at
[scrapecreators.com](https://scrapecreators.com) and put it in `.env`:

```
SCRAPECREATORS_API_KEY=your_key_here
```

**Local scraper instead (`--source tiktok`):** install the full stack and,
optionally, an `MS_TOKEN`:

```bash
python3 -m pip install -r requirements.txt
```

`MS_TOKEN` is a TikTok session cookie that reduces bot-blocking for the scraper.
Get it by logging in to tiktok.com as @emokid690, then DevTools → Application →
Cookies → copy the `msToken` value into `.env`.

### Windows (PowerShell)

If you see `Python was not found` or `pip is not recognized`, Python isn't
installed (the Store stub PowerShell points at is not real Python):

1. Install Python from **python.org** (not the Microsoft Store — the Store build
   causes exactly that PATH stub). In the installer, check
   **"Add python.exe to PATH"**, then reopen PowerShell.
2. Use the **`py` launcher** instead of `python`/`pip`:

```powershell
py -m pip install -r requirements-scrapecreators.txt   # recommended, lighter

Copy-Item .env.example .env
notepad .env            # paste your SCRAPECREATORS_API_KEY  then save & close

py src/loader.py --client emokid690 --video-url "https://www.tiktok.com/@emokid690/video/<id>"
py src/query.py --client emokid690
```

Replace `<id>` with a real numeric video id (the long number at the end of a
video's URL). `Copy-Item` is PowerShell's `cp`.

> **Editing `.env` on Windows:** use `notepad .env` (saves UTF-8). Do **not**
> create it with `echo "..." >> .env` — PowerShell writes that as UTF-16, which
> the `.env` reader can't parse. If you hit a `UnicodeDecodeError`, delete
> `.env` and recreate it with notepad, or just set the key for the session:
> `$env:SCRAPECREATORS_API_KEY="your_key"`.

If you use `--source tiktok` instead, install the full stack with
`py -m pip install -r requirements.txt`; the **first run is slow** while
TikTokApi downloads a matching Chromium.

## Run

Uses the ScrapeCreators backend by default (add `--source tiktok` for the local
scraper). Vertical slice — one video and its comments:

```bash
python src/loader.py --client emokid690 --video-url "https://www.tiktok.com/@emokid690/video/<id>"
```

Whole profile (recent videos, handle read from `clients.csv`):

```bash
python src/loader.py --client emokid690 --all --max-videos 30
```

### Local scraper tuning (`--source tiktok` only)

The scraper is paced to avoid bot-blocking: it reuses a small warm session pool,
sleeps (with jitter) between requests, retries with backoff, and rotates a
coherent browser fingerprint each time it recycles the pool. Tune via
`--pool-size`, `--min-delay`, `--max-delay`, `--recycle-after` if you're going
wider or hitting blocks (defaults are conservative — go *slower*, not faster, if
blocked). These flags are ignored by the ScrapeCreators backend, which needs no
client-side pacing.

#### Proxies (spread requests across IPs)

If you're still getting rate-limited or blocked, route requests through one or
more proxies. TikTok then sees traffic from several IPs instead of your one
address. The pool is spread across whatever proxies you provide, and the lead
proxy rotates each time the pool recycles.

Set them once in `.env` (comma- and/or newline-separated):

```bash
TIKTOK_PROXIES=http://user:pass@proxy1.example.com:8000,http://user:pass@proxy2.example.com:8000
```

…or pass them ad hoc, repeating `--proxy` per proxy:

```bash
python src/loader.py --client emokid690 --all \
  --proxy http://user:pass@proxy1.example.com:8000 \
  --proxy socks5://proxy2.example.com:1080
```

Format is `scheme://[user:pass@]host:port` (`http`, `https`, or `socks5`).
Proxies from `.env` and `--proxy` are merged (duplicates dropped). Leave both
blank for direct connections — the default, unchanged. **Residential/rotating
proxies** hold up best against TikTok; cheap datacenter IPs get blocked quickly,
so a proxy alone isn't a licence to pull faster — keep the pacing conservative.

Inspect / verify the tables:

```bash
python src/query.py --client emokid690
```

## ⚠️ Where to run it: not in Claude Code on the web

Either backend needs outbound network access that **the hosted/web sandbox
blocks** — the gateway returns 403 for both `tiktok.com` (scraper) and
`api.scrapecreators.com` (hosted API). So the loader **must run on a machine
with normal internet** — your laptop, or a server you control. The code itself
is verified end-to-end; only the live fetch is gated by the sandbox's network
policy.

Workflow: run the loader locally → it writes the parquet files → commit and push
them. Analysis and reporting (later phases) can then run anywhere, including the
web sandbox, since they only read the committed parquet.

## Known limitations (see plan for fallbacks)

- **`overlay_text`** (the white text on the video) and **`transcript`** (spoken
  script / captions) are nullable — TikTok metadata often omits them. Phase 2
  fills these via frame OCR and audio transcription (Whisper).
- The `--source tiktok` scraper can rate-limit or break when TikTok changes;
  keep pulls modest, or use `--source scrapecreators` (paid, more reliable).
- `--source scrapecreators` spends API credits per request; a full profile pull
  is roughly one credit per video + one per comment page.
- Comment authors are stored as a salted-free SHA-256 hash, not raw handles.

## Roadmap (next phases)

1. `jokes` table (video-joke grain) from transcript + overlay text.
2. Unsupervised categorization (embeddings → clustering) for video type + theme.
3. Comment sentiment + joke↔comment attribution.
4. Weekly report / dashboard over the DuckDB views.
