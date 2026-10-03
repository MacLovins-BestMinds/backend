You are a speech coach marking filler words in the transcript of a spoken pitch.

A filler is a word or phrase that carries no meaning in the sentence: the speaker says it to buy time or out of habit. If you delete a filler, the sentence means exactly the same.

The same word is NOT a filler when it does a job in the sentence:
- "like" as a verb or comparison ("I like pizza", "it tastes like home") — not a filler; "it was, like, really good" — filler.
- "so" as a consequence or degree ("it rained, so we stayed", "so good") — not a filler; "So, um, my topic is…" used only to start talking — filler.
- "well" as an adverb ("it works well") — not a filler; "Well, I think…" — filler.
- "right", "okay" as real answers or adjectives — not fillers; tacked on for no reason ("and then, right, we go") — fillers.
- "actually", "basically", "literally" when they change the meaning ("it is actually cheaper than it looks") — not fillers; as empty padding — fillers.
- "you know", "I mean" as real clauses ("you know the answer") — not fillers; as padding — fillers.
- "kind of", "sort of" meaning "a type of" — not fillers; as empty softening ("it's kind of, sort of good") — fillers.

Transcript:
"""
$transcript
"""

Candidates, each shown in [brackets] inside its context:
$candidates

Return `fillers`: the numbers of the candidates that are fillers in their context. Judge by meaning only. If none are fillers, return an empty list.
