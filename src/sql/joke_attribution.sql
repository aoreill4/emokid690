select
  v.caption                                              as video,
  j.joke_text,
  jc.joke_id,
  j.punchline,
  round(avg(s.sentiment)::numeric, 3)                    as avg_sentiment,
  count(distinct c.text)                                 as num_comments,
  sum(coalesce(c.reply_count, 0))                        as reply_count,
  sum(coalesce(c.like_count, 0))                         as total_likes,
  sum(coalesce(c.like_count+c.reply_count, 0))                         as total_impact
from joke_comment jc
join jokes j            using (joke_id)
join comments c        on c.comment_id = jc.comment_id
join video v           on v.video_id   = j.video_id
left join comment_sentiment s on s.comment_id = jc.comment_id
where confidence >.7
group by v.caption, j.joke_text, j.punchline, jc.joke_id
order by total_likes desc;
