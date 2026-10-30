You read a phone call between Lana, an AI assistant calling for a patient, and the front desk of an orthodontic clinic. Fill in the facts the clinic has stated so far.

Today is {today}. The clinic is in the {timezone} time zone.

## Transcript

{transcript}

## Already confirmed

{current}

## Fields

- total_price: the total price of the full treatment, in whole dollars.
- records_fee: a separate fee for records (x-rays, scans, photos), in whole dollars. 0 if the clinic says there is no records fee.
- consult_fee: the price of the first consult, in whole dollars. 0 if it is free.
- includes: what the total price covers, as short lowercase items, for example "records", "x-rays", "scans", "adjustments", "retainers", "consult". Use "records" when the clinic says records, x-rays or scans are included.
- monthly_payment: the monthly payment on their plan, in whole dollars.
- down_payment: the down payment for the monthly plan, in whole dollars.
- payment_plan: true if the clinic offers a monthly payment plan, false if it says it does not.
- next_slots: the consult appointment times the clinic offered, as local ISO datetimes without a time zone offset, for example "2026-10-30T07:30:00". Work out the date from today's date.
- evening_hours: true if the clinic offers evening or after-work appointments, false if it says it does not.
- needs_exam_first: true if the clinic says it must examine the patient before giving a price.

## Rules

- Use only what a CLINIC line clearly says. Never use LANA's lines as a source, even if the clinic agrees with them; quote the clinic's own words instead.
- For every field you fill, "evidence" must be words copied exactly, in order, from one CLINIC line. Copy the shortest part that proves the value, for example "$4,200 total".
- If the clinic did not say it, was unsure, or said it would need to check, use null. Never guess or work a number out yourself.
- If the clinic changed an answer later in the call, use the latest one.
- Money is a whole number of dollars, no $ sign or commas.
