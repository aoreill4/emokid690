---
name: identify-jokes
description: >-
  Segment a short-form comedy video transcript (TikTok/Reels/Shorts) into its
  distinct jokes — the individual comedic beats and punchlines. Use this whenever
  you need to break a comedy transcript into jokes, find the punchlines/bits in a
  skit, label the beats of a stand-up or sketch clip, or produce a structured list
  of jokes from spoken/caption text — including when populating a `jokes` table
  from transcripts or analyzing which bits land. Trigger even if the user says
  "break this bit down," "what are the jokes here," or "split the transcript into
  gags" without naming this skill.
---

# Identify Jokes

Break a short-form comedy transcript into an ordered list of **discrete jokes**,
so each one can be stored, and later matched against audience comments to see
which bits landed.

## What counts as "a joke" here

The instinct is to look for one-liners, but most short-form comedy — especially
character-driven skits — doesn't work that way. The humor is built from **beats**:
small comedic moves that each land a laugh. Treat a "joke" as *one comedic beat* —
the smallest self-contained unit that delivers a distinct laugh.

A beat is usually one of these:
- **Premise flip** — a mundane situation reframed as something absurd (the engine
  of most skits). "Did you sniff her butt?" reframing a dog's walk as romantic
  jealousy.
- **Punchline** — the line that pays off a setup.
- **Escalation** — pushing the absurd premise further than the last beat. Each
  escalation is its own joke because it earns its own laugh.
- **Callback** — re-invoking an earlier bit in a new context.
- **Act-out / turn** — a sudden shift in character, voice, or tone played for
  laughs.
- **Absurd specific** — an oddly precise detail that's funny because of the
  specificity ("smelling like the cat section at Petsmart").

Why beats and not sentences: a single joke often spans a setup line **and** its
punchline, and sometimes several lines build one laugh. Conversely, one long
speech can contain three separate laughs. Segment by *where the laughs are*, not
by sentence or timestamp.

**When in doubt, split — and lean finer than feels natural.** The purpose of this
list is to match *audience comments* back to specific jokes, and audiences quote
**specific, concrete phrases**: a name, an absurd image, a weird detail. So a good
test is: **if a phrase is specific and quotable enough that a viewer might type it
in the comments, it's probably its own beat.** "A wandering nose," "Roxy and
Luna," "the cat section at Petsmart," "it's only two human years" — each of those
is a distinct laugh a commenter could reference, so each should be its own row,
even when they sit inside the same sentence or share a theme. Merging them buries
the signal we're trying to capture. It's much better to split a borderline beat in
two than to bury a quotable line inside a bigger one.

## Method

1. **Read the whole transcript first.** Identify the **comedic premise** — the
   core conceit the whole piece runs on (e.g., "talking to her dog as if it's a
   long-term romantic partner"). Almost every beat is a variation on this premise;
   naming it first makes the individual beats obvious.
2. **Walk through and mark each laugh.** For each point where the audience is
   meant to laugh, capture the beat: include enough surrounding text that the joke
   is understandable on its own (setup + punchline together), but don't pad it with
   unrelated lines.
3. **Keep setups attached to their punchline.** A punchline without its setup
   isn't a usable joke. If a setup earlier feeds a later punchline, put the whole
   arc in one beat.
4. **Prefer splitting distinct laughs.** If a passage has two separate funny turns
   — even adjacent, even in one sentence, even under the same theme — that's two
   jokes. A named detail ("Roxy and Luna"), an absurd specific ("the cat section
   at Petsmart"), and a pun ("a wandering nose") are each their own beat even when
   they're wrapped in a longer line. Erring toward more beats is the safer mistake
   here, because a quoted comment needs a specific joke to attach to.
5. **The one thing you can't split:** a setup and the punchline it directly pays
   off. Those stay together — a punchline without its setup isn't usable. So the
   unit is "one laugh, with just enough context to stand alone," not "one clause."
6. **Skip non-comedic filler.** Intros ("hey guys"), outros, calls-to-action
   ("follow for part 2", "coming soon"), and pure connective tissue aren't jokes.
   Quality over quantity — but note that in a dense skit, most lines *are* doing
   comedic work, so expect a lot of beats, not a few.
7. **Order them** by appearance as `joke_index` (1-based).

## Output format

Return **only** a JSON object, no prose around it, in exactly this shape:

```json
{
  "premise": "one sentence naming the comedic engine of the piece",
  "jokes": [
    {
      "joke_index": 1,
      "joke_text": "the transcript span for this beat, lightly cleaned",
      "punchline": "the specific funny line/phrase within it",
      "theme": "2-4 word tag for what the joke is about"
    }
  ]
}
```

Field notes:
- `joke_text` — the span a viewer/commenter would recognize as "that bit." Keep it
  close to the transcript wording (light cleanup of filler is fine); this is what
  we'll fuzzy-match audience comments against, so the recognizable words matter.
- `punchline` — the sharpest few words of the beat. Helps comment-matching key on
  the memorable phrase.
- `theme` — a short tag (e.g., "jealousy", "dog years", "cheating accusation").
  Useful for grouping recurring bits across videos.
- If the transcript has **no real jokes** (e.g., a music-only clip or a serious
  talking-head), return `{"premise": "...", "jokes": []}`. An empty list is a valid,
  honest answer — don't invent jokes to fill space.

## Example

**Input transcript (a skit where she talks to her dog like a jealous partner):**

> Hey, I'm home. Oh, I can't wait to get this off. My collar burn is, like, so bad.
> What did you do today? Did you go on a walk? Did you see anyone? Did you sniff her
> butt? I just need to get a drink. You know my friends go to that park. Roxy and
> Luna are there every single day. You just don't seem to understand how
> embarrassing it looks to me when you're out at the park every day. I think I have
> a flea or something. It just reminds me of our fight from last week when you came
> home smelling like the cat section at Petsmart. But, no, babe, I'm not into cats.
> We've been together for 14 years. It feels important to mention that it's only two
> human years. We caught our first squirrels together, you know? I always knew you
> had a wandering nose. Can you please not lick yourself when I'm talking to you? We
> met in college, and I saw you rub your ass across the dance floor for the first
> time. There's probably a dog at the bar right now who would kill to sniff my butt.

**Output** — note how finely it splits: named details, puns, and absurd specifics
each become their own beat, because those are the lines an audience quotes back.

```json
{
  "premise": "She narrates her dog's ordinary day at the park as if the dog were her long-term, cheating romantic partner.",
  "jokes": [
    {"joke_index": 1, "joke_text": "I can't wait to get this off. My collar burn is, like, so bad", "punchline": "collar burn", "theme": "collar-as-clothing"},
    {"joke_index": 2, "joke_text": "Did you see anyone? Did you sniff her butt?", "punchline": "Did you sniff her butt?", "theme": "jealousy"},
    {"joke_index": 3, "joke_text": "Roxy and Luna are there every single day", "punchline": "Roxy and Luna", "theme": "dog friends as her social circle"},
    {"joke_index": 4, "joke_text": "how embarrassing it looks to me when you're out at the park every day", "punchline": "how embarrassing it looks to me", "theme": "controlling partner"},
    {"joke_index": 5, "joke_text": "I think I have a flea or something", "punchline": "I think I have a flea", "theme": "dog/human reality slip"},
    {"joke_index": 6, "joke_text": "our fight from last week when you came home smelling like the cat section at Petsmart", "punchline": "the cat section at Petsmart", "theme": "cheating accusation"},
    {"joke_index": 7, "joke_text": "But, no, babe, I'm not into cats", "punchline": "I'm not into cats", "theme": "denial"},
    {"joke_index": 8, "joke_text": "We've been together for 14 years. It feels important to mention that it's only two human years", "punchline": "it's only two human years", "theme": "dog years"},
    {"joke_index": 9, "joke_text": "We caught our first squirrels together, you know?", "punchline": "caught our first squirrels together", "theme": "relationship milestone"},
    {"joke_index": 10, "joke_text": "I always knew you had a wandering nose", "punchline": "a wandering nose", "theme": "wandering nose pun"},
    {"joke_index": 11, "joke_text": "Can you please not lick yourself when I'm talking to you?", "punchline": "not lick yourself when I'm talking to you", "theme": "dog behavior mid-argument"},
    {"joke_index": 12, "joke_text": "We met in college, and I saw you rub your ass across the dance floor for the first time", "punchline": "rub your ass across the dance floor", "theme": "how they met"},
    {"joke_index": 13, "joke_text": "There's probably a dog at the bar right now who would kill to sniff my butt", "punchline": "a dog at the bar who would kill to sniff my butt", "theme": "jealous retaliation"}
  ]
}
```

Notice: the intro ("Hey, I'm home") and connective filler ("I just need to get a
drink") are dropped, but "Roxy and Luna" (joke 3) and "a wandering nose" (joke 10)
are their **own** beats rather than being folded into the lines around them —
because those are exactly the specific phrases a commenter quotes back. Thirteen
beats from one dense skit is normal; don't compress it down to a handful.
