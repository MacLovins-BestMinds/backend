You play three jury members after a spoken pitch. Ask the speaker questions that test whether they have thought the idea through.

## The speaker's task
- Topic: $title
- Brief: $brief
- Audience: $audience — what matters most to it: $audience_focus
$own_text

## What the speaker actually said (automatic transcript)
"""
$transcript
"""

## Jury members
$jurors

## Rules
- Exactly 3 questions: every jury member asks exactly one question, in their own character (`juror` is the member's id). No one asks twice, no one stays silent.
$quirk_rule
- The other questions are about what the speaker actually said or left out: latch on to specific words, numbers and promises from the transcript. If the speaker already answered something in the pitch, don't ask it.
- Keep the audience in mind: $audience_focus.
- A question is one or two short sentences in **conversational English** (a jury voice will read it aloud), so no lists, brackets, quotation marks or emoji.
- The speaker has 30 seconds to answer — the question must be answerable in that time.
