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

## Setup

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env      # then paste an MS_TOKEN (see below)
```

`MS_TOKEN` is a TikTok session cookie. It's optional but strongly recommended —
without it TikTok bot-blocks most requests. Get it by logging in to tiktok.com
as @emokid690, then DevTools → Application → Cookies → copy the `msToken` value
into `.env`.

### Windows (PowerShell)

If you see `Python was not found` or `pip is not recognized`, Python isn't
installed (the Store stub PowerShell points at is not real Python):

1. Install Python from **python.org** (not the Microsoft Store — the Store build
   causes exactly that PATH stub). In the installer, check
   **"Add python.exe to PATH"**, then reopen PowerShell.
2. Use the **`py` launcher** instead of `python`/`pip`:

```powershell
py -m pip install -r requirements.txt

Copy-Item .env.example .env
notepad .env            # paste your msToken after MS_TOKEN=  then save & close

py src/loader.py --client emokid690 --video-url "https://www.tiktok.com/@emokid690/video/<id>"
py src/query.py --client emokid690
```

Replace `<id>` with a real numeric video id (the long number at the end of a
video's URL). The **first run is slow** — TikTokApi downloads a matching Chromium
once. `Copy-Item` is PowerShell's `cp`.

## Run

Vertical slice — one video and its comments:

```bash
python src/loader.py --client emokid690 --video-url "https://www.tiktok.com/@emokid690/video/<id>"
```

Whole profile (recent videos, handle read from `clients.csv`):

```bash
python src/loader.py --client emokid690 --all --max-videos 30
```

The loader is paced to avoid bot-blocking: it reuses a small warm session pool,
sleeps (with jitter) between requests, retries with backoff, and rotates a
coherent browser fingerprint each time it recycles the pool. Tune via
`--pool-size`, `--min-delay`, `--max-delay`, `--recycle-after` if you're going
wider or hitting blocks (defaults are conservative — go *slower*, not faster, if
blocked).

### Proxies (spread requests across IPs)

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

TikTok scraping needs outbound access to `tiktok.com`. **The hosted/web sandbox
blocks it** (the network gateway returns 403 for `tiktok.com`), so the loader
**must run on a machine that can reach TikTok** — your laptop, or a server you
control. The code itself is verified end-to-end there; only the live fetch is
gated by the sandbox's network policy.

Workflow: run the loader locally → it writes the parquet files → commit and push
them. Analysis and reporting (later phases) can then run anywhere, including the
web sandbox, since they only read the committed parquet.

## Known limitations (see plan for fallbacks)

- **`overlay_text`** (the white text on the video) and **`transcript`** (spoken
  script / captions) are nullable — TikTok metadata often omits them. Phase 2
  fills these via frame OCR and audio transcription (Whisper).
- Free unofficial scraping can rate-limit or break when TikTok changes; keep
  pulls modest.
- Comment authors are stored as a salted-free SHA-256 hash, not raw handles.

## Roadmap (next phases)

1. `jokes` table (video-joke grain) from transcript + overlay text.
2. Unsupervised categorization (embeddings → clustering) for video type + theme.
3. Comment sentiment + joke↔comment attribution.
4. Weekly report / dashboard over the DuckDB views.
