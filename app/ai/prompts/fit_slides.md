You help a speaker fit a short spoken pitch to their slide deck: for every slide, write what to say while that slide is on screen.

The pitch:
- Topic: $title
- Audience: $audience

The speaker's own text (use their ideas and wording wherever they match a slide; do not invent facts they did not give):
"""
$text
"""

The slides:
$slides

Return `slides` — one entry per slide of the deck, in order, none skipped:
- `n` — the slide number, starting from 1.
- `title` — what the slide is about, at most 5 words (use the slide's own title when it has one).
- `kind` — "demo" if the slide is devoted to a demonstration: its title or content says demo, live demo, product demo, walkthrough, "let me show you", a screen recording, or it is only a product screenshot meant to be shown live. Otherwise "talk".
- `text` — for a "talk" slide: one to three short spoken sentences in plain English that match what is on this slide. Say what the slide shows, do not read it out word for word. For a "demo" slide the text is exactly: Demo time.

Rules:
- Look at what is actually on each slide and follow it. If the speaker's text has nothing for a slide, write a short neutral line about the slide instead of leaving it empty.
- The whole pitch must fit in three minutes of speech: about 350 words in total. With many slides, keep each one to a single sentence.
- First slide: a hook or a greeting plus the topic. Last slide: a clear closing line or call to action.
