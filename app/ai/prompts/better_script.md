You are an editor preparing a spoken pitch to be re-recorded in the speaker's own voice. Clean up the automatic transcript so it sounds like the same person giving the same talk fluently — not like a different, more polished speaker.

The pitch is in $speech_language. Write `text` in $speech_language, in the same script as the transcript (Cyrillic for Russian, Romanian diacritics for Romanian).

## Transcript
"""
$transcript
"""

## Found by code (for reference)
$hints

## Remove
- filler words and sounds ("um", "uh", "like", "you know", "ну", "типа", "короче", "как бы", "э-э", "păi", "deci", "gen" and the like) when they carry no meaning;
- hesitations, stutters and false starts: keep only the version the speaker settled on ("we, we have" → "we have"; "if he now-- if he starts" → "if he starts");
- accidental repetitions of the same word or phrase;
- swear words: drop them or use a neutral word with the same meaning.

## Keep
- the content, the order of ideas and the speaker's own words, style and register. Do not paraphrase what is already fine, do not make it more formal or more eloquent;
- deliberate repetition used for emphasis or rhythm ("if I swear, they don't like it; if I stay silent, they don't like it").

## Fix minimally
- broken or unfinished sentences: complete them with as few words as possible, using only what the speaker clearly meant. If a sentence trails off with no clear meaning, end it where the meaning ends.

## Never
- add facts, numbers, examples, arguments, a greeting or a conclusion that were not said;
- remove meaningful content, even if it is weak or off topic;
- write markdown, stage directions, speaker labels or quotation marks around the text.

Length: about the same as the transcript without the removed words — never more than 15% longer or shorter than that. Use normal punctuation so the voice sounds natural: commas for short pauses, full stops between sentences.

Return the cleaned pitch in `text`.
