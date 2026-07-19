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
4. **Don't over-fragment.** If two clauses land a *single* laugh, they're one joke.
   Splitting them creates noise that pollutes the later comment-matching.
5. **Don't merge distinct laughs.** If a passage has two separate funny turns,
   that's two jokes, even if they're adjacent.
6. **Skip non-comedic filler.** Intros ("hey guys"), outros, calls-to-action
   ("follow for part 2"), and pure connective tissue aren't jokes. It's fine for a
   transcript to yield only a few jokes — quality over quantity.
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

> Hey, I'm home. Oh, I can't wait to get this off, my collar burn is so bad. What
> did you do today? Did you go on a walk? Did you see anyone? Did you sniff her
> butt? Oh my god. I just need to get a drink. You know my friends go to that park.
> Roxy and Luna are there every single day. It just reminds me of our fight from
> last week when you came home smelling like the cat section at Petsmart. We've
> been together for 14 years. It feels important to mention that it's only two
> human years.

**Output:**

```json
{
  "premise": "She narrates her dog's ordinary day as if the dog were her long-term romantic partner.",
  "jokes": [
    {
      "joke_index": 1,
      "joke_text": "my collar burn is so bad",
      "punchline": "collar burn",
      "theme": "dog-as-partner reframe"
    },
    {
      "joke_index": 2,
      "joke_text": "Did you see anyone? Did you sniff her butt?",
      "punchline": "Did you sniff her butt?",
      "theme": "jealousy"
    },
    {
      "joke_index": 3,
      "joke_text": "our fight from last week when you came home smelling like the cat section at Petsmart",
      "punchline": "smelling like the cat section at Petsmart",
      "theme": "cheating accusation"
    },
    {
      "joke_index": 4,
      "joke_text": "We've been together for 14 years. It feels important to mention that it's only two human years",
      "punchline": "it's only two human years",
      "theme": "dog years"
    }
  ]
}
```

Notice: the intro ("Hey, I'm home") and connective lines ("I just need to get a
drink") are dropped — they set the scene but aren't laughs. Each kept beat is a
distinct variation on the one premise, and each `joke_text` keeps the words a
commenter would quote back.
