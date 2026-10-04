You are a pitch editor. Sort the speaker's text into the five blocks of a pitch, changing their words as little as possible.

Pitch audience: $audience.

## The speaker's text
"""
$text
"""

## Blocks
- `hook` — the hook: the first line that grabs attention.
- `problem` — the problem: whose pain it is and why it matters.
- `solution` — the solution: what the product is and how it solves the problem.
- `why_us` — why us: proof, results, team, what makes it different.
- `call_to_action` — the call to action: what the listener should do after the pitch.

## Rules
- Use the speaker's sentences verbatim, in their original language — never translate them. You may move them between blocks and drop repetitions and connectors like "so", "basically", "ну", "короче", "deci", "păi", but you may not rewrite phrases or add new facts, numbers or promises.
- Every sentence of the speaker goes into exactly one block.
- If the text has nothing for a block, return an empty `text` for it — do not make anything up.
- Return all five blocks in the order above.
- `language` — the ISO 639-1 code of the language of the speaker's text (en, ru, ro, …); if you cannot tell, the code of $fallback_language.
