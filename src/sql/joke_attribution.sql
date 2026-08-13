-- Best-performing jokes, ranked by total audience engagement.
--
-- Engagement = likes + replies across every high-confidence comment attributed
-- to each joke (confidence > 0.7). avg_sentiment rides along as a "how did people
-- feel" readout; num_comments is the distinct-text mention count so copypasta
-- doesn't inflate a joke.
--
-- Queries in this folder (src/sql/) are the source for the email report — the
-- report runner executes each .sql here against Supabase and renders the rows.
-- Keep them plain SELECTs (read-only), one result set per file.

select
  v.caption                                              as video,
  j.joke_text,
  jc.joke_id,
  j.punchline,
  round(avg(s.sentiment)::numeric, 3)                    as avg_sentiment,
  count(distinct c.text)                                 as num_comments,
  sum(coalesce(c.reply_count, 0))                        as reply_count,
  sum(coalesce(c.like_count, 0))                         as total_likes,
  sum(coalesce(c.like_count, 0) + coalesce(c.reply_count, 0)) as engagement_score
from joke_comment jc
join jokes j            using (joke_id)
join comments c        on c.comment_id = jc.comment_id
join video v           on v.video_id   = j.video_id
left join comment_sentiment s on s.comment_id = jc.comment_id
where jc.confidence > 0.7
group by v.caption, j.joke_text, j.punchline, jc.joke_id
order by engagement_score desc;
