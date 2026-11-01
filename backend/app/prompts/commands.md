A patient is talking to Lana, an AI assistant who finds and books orthodontic care. Decide whether this short thing the patient said is one of the app commands below, or not a command at all.

## What the patient said

{text}

## Commands

- watch: the patient wants to watch a demo run instead of talking, for example "just show me", "let me see it work".
- clinic: the patient wants to play the clinic's receptionist, for example "I'll be the clinic", "let me answer the call".
- start_over: the patient wants to go back to the start, for example "start over", "from the top".
- yes_book: the patient agrees to book, for example "yes", "book it", "sounds good".
- not_yet: the patient does not want to book now, for example "not yet", "hold off".
- skip: the patient wants to skip the current question, for example "skip", "next one".

## Rules

- Anything else is null: a request for care ("braces please"), a place ("Dallas"), an amount ("five grand"), a payment choice ("monthly"), or a question.
- When in doubt, null.
