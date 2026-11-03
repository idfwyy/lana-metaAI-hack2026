# Lana

Lana is a voice AI that handles your care for you. You tell her what you need, she calls clinics, gets real quotes, negotiates with them, and calls you back with the best deal.

**Live demo:** https://lana-health.pages.dev
**Video:** [add link]

## Why

The same braces can cost $3,000 at one clinic and $7,000 at the next. Prices aren't posted, so the only way to compare is to call around, wait on hold and ask the same questions again and again. Most people pay whatever the first clinic says.

Right now Lana handles braces. The same approach works for any care people pay for out of pocket, like dental work, eye care, imaging, physical therapy or minor procedures, and that's where she's going next.

## Try it

Open the link, pick one:

- **Talk to Lana.** Tell her what you need out loud. She asks a few questions, calls the clinics, and calls you back.
- **Just watch.** An AI patient and AI clinics. Watch the whole thing play out.
- **Be the clinic.** You play the front desk. Lana calls you and negotiates. A card shows your prices.

Use headphones and Chrome if you can.

**About the demo clinics.** Lana doesn't call real offices during judging, so she doesn't waste real clinics' time. She calls demo clinics instead. Each one is its own AI receptionist with a hidden price sheet and a lowest price it won't go below. Lana never sees those sheets, so she really has to ask and negotiate, and results change from run to run.

## How it works

1. **Intake.** Lana talks with you and fills in what she needs: treatment, location, budget, how you pay, timing, anything else that matters to you.
2. **Research.** She picks the clinics that fit best.
3. **Calls.** She calls them one by one. Every call starts with her saying she's an AI and the call is recorded. If the best fit isn't the cheapest, she asks them to hold and calls the others.
4. **Negotiation.** She goes back to the best fit with the real lower quote and asks them to match it, waive fees, or discount for paying in full.
5. **Callback.** She calls you, tells you the price and what she saved, and books only if you say yes.

Every price on screen links to the clinic's exact words and the second they said it.

## Meta models

| Model | What it does |
|---|---|
| Muse Spark 1.3 | Intake conversation, Lana's live call turns, the AI receptionists, reading quotes out of each call |
| Muse Wonder 1.0 | Writing each negotiation ask and the callback summary |
| Muse Voice Transcribe 1.0 | Streaming speech to text with Meta's endpointing, so Lana knows when you've stopped talking |

Lana's voice comes from ElevenLabs, since Meta doesn't have text to speech.

## Rules Lana can't break

These are enforced in code, not just in prompts:

- She only negotiates with quotes she actually got. A line with a made-up price or clinic name is thrown out.
- Ranking, call order, hold and who to negotiate with are decided in plain Python, not by the model.
- Every number she reports has evidence from the call, or it shows as "Not confirmed".
- She never commits you to anything you didn't say, like paying in full.
- She never gives medical advice.
- She never books without a clear yes.

## Stack

Python, FastAPI, WebSockets, Pydantic. React, TypeScript, Vite, Tailwind. Backend on Railway, site on Cloudflare Pages.

## Run it locally

```bash
# backend
cd backend
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
copy ..\.env.example ..\.env    # add your Meta and ElevenLabs keys
uvicorn app.main:app --port 8000

# web (new terminal)
cd web
npm install
npm run dev                     # http://localhost:5173
```

Tests: `cd backend && pytest`

## What's next

Real clinics, real calls with consent, any medical service people shop for (not just braces), and a mobile app.

Built by Rahul Sah.