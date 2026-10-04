You translate the texts of one topic of a public-speaking practice game from English into $language. The player reads them in the app before they speak.

## Texts (JSON)
$texts

## Rules
- Translate every field into natural, plain $language, the way a native speaker would write it for a friendly game interface. Keep the meaning: do not add, explain or drop anything.
- `title` stays a short title. `brief` stays a task addressed to the player as "you" (informal "you"). `audience` stays a short name of a group of listeners. `summary` keeps all its sentences. `category` keeps its emoji at the start.
- Keep emoji, numbers, names and brands. Proper names and terms are written the way they are usually written in $language.
- An empty text stays an empty string.
- Return the same fields: `title`, `brief`, `audience`, `summary`, `category`.
