-- Supabase (Postgres) schema for the TikTok analytics tables.
--
-- Run this ONCE before the first sync: open your Supabase project → SQL Editor →
-- paste this in → Run. Columns mirror schema.py (VIDEO_COLUMNS / COMMENT_COLUMNS)
-- exactly, so sync_supabase.py can upsert by primary key.
--
-- Re-running is safe: every object uses "if not exists".

create table if not exists video (
    video_id      text primary key,
    client        text,
    url           text,
    created_at    timestamptz,
    caption       text,
    overlay_text  text,
    transcript    text,
    hashtags      text[],
    music         text,
    duration_s    bigint,
    like_count    bigint,
    comment_count bigint,
    share_count   bigint,
    play_count    bigint,
    collected_at  timestamptz
);

create table if not exists comments (
    comment_id        text primary key,
    video_id          text,
    client            text,
    text              text,
    like_count        bigint,
    reply_count       bigint,
    parent_comment_id text,   -- null = top-level; else the comment this replies to
    author_hash       text,   -- SHA-256 of the handle, never the raw username
    created_at        timestamptz,
    collected_at      timestamptz
);

-- Helpful indexes for the common access patterns (join comments->video, thread
-- reconstruction, and per-client filtering). No strict FK on comments.video_id
-- so a comment can sync even if its video row hasn't been ingested yet.
create index if not exists comments_video_id_idx  on comments (video_id);
create index if not exists comments_parent_id_idx on comments (parent_comment_id);
create index if not exists video_client_idx       on video (client);
create index if not exists comments_client_idx     on comments (client);
