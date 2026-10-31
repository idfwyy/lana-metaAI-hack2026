You read one reply from a patient talking to {agent_name}, an AI assistant who helps patients in Dallas-Fort Worth find orthodontic care (braces or clear aligners). Pull out every preference the patient stated in this reply. One reply often answers several things at once: "braces near Dallas under 5k, evenings" gives treatment, city, budget and must_haves.

## What {agent_name} just asked

{question}

## What the patient said before

{draft}

## The patient's reply

{text}

## Fields

- treatment: "braces", "aligners" (clear aligners, Invisalign) or "not sure". Use "not sure" only if the patient says they don't know which one.
- zip_code: a 5 digit ZIP code, only if the patient said one.
- city: the city or area name exactly as the patient said it, for example "Dallas", only if they named a place.
- max_distance_mi: how many miles the patient will travel, only if they said a distance.
- budget: the most the patient wants to spend in total, in the patient's own words, for example "5k", "under five thousand", "4,500". Leave out monthly amounts.
- prefers_monthly: true if the patient wants monthly payments, false if they say they don't.
- pay_in_full_ok: true only if the patient clearly says they could pay the whole amount at once, for example "I can pay in full if it's cheaper". false if they say they can't. Otherwise null.
- uses_hsa_fsa: true if the patient will use an HSA or FSA, false if they say they don't have one.
- has_xrays: true if the patient already has x-rays, false if they say they don't.
- has_plan: true if the patient already has a treatment plan from a dentist or orthodontist, false if they say they don't.
- must_haves: things a clinic must offer, as short lowercase phrases, for example "evening hours", "saturday hours", "spanish speaking". "evenings" or "after work" means "evening hours". An empty list if the patient says they have none.
- free_times: when the patient is free for a consult, as short phrases, for example "weekday evenings".
- skip: true if the patient declines to answer what {agent_name} just asked, for example "skip", "I don't know", "no preference", "doesn't matter".
- medical_question: true if the patient asks for medical or dental advice, for example whether they need braces, which treatment is right for them, or whether their teeth are bad.

## Rules

- Use only what the patient said in this reply. Never guess a value they did not say.
- If the reply changes an earlier answer, give the new value.
- For must_haves and free_times, give the full list: keep earlier items unless the patient drops them.
- Every field you are not sure about is null.
- A question like "do I need braces?" is a medical_question and does not set treatment.

<!-- lines -->

- greeting: Hey, I'm {agent_name}, your personal health companion! What do you need help with?
- treatment: Are you looking for braces, clear aligners, or not sure yet?
- location: What ZIP code or city should I search near?
- budget: What's the most you'd want to spend in total?
- pay: How would you like to pay: monthly, in full if it's cheaper, or with an HSA or FSA?
- records: Do you already have x-rays or a treatment plan from a dentist?
- must_haves: Is there anything a clinic must have, like evening hours?
- no_advice: I can't advise on treatment, but a dentist or orthodontist can.
- confirm: Got it. {summary}. I'll start calling now.
