"""Typer commands to test each piece in the terminal."""

import asyncio
import logging
import time

import json
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from app.agent.caller import Caller
from app.agent.extractor import CHECKLIST_ORDER, build_quote
from app.agent.intake import Intake
from app.agent.negotiator import negotiate as run_negotiation
from app.agent.negotiator import phrase_move, pick_target, plan_moves
from app.agent.orchestrator import Orchestrator
from app.agent.ranker import all_in as offer_all_in
from app.clinics.simulator import ClinicSimulator
from app.config import get_settings
from app.data.loader import get_public_clinics, load_demo_clinics
from app.integrations.meta_llm import stream_chat
from app.schemas.case import Case
from app.schemas.enums import CaseStatus, DemoMode, Speaker, Treatment
from app.schemas.events import (
    CallStatusEvent,
    CaseStatusEvent,
    ConcessionWon,
    ErrorEvent,
    LogLine,
    OffersReady,
    QuoteUpdated,
    TranscriptFinal,
)
from app.schemas.preferences import Preferences
from app.schemas.quote import Quote

app = typer.Typer(no_args_is_help=True)
console = Console()

# Fixed intake answers for commands that skip intake (the example from SETUP.md section 3.10).
DEMO_PREFERENCES = Preferences(
    treatment=Treatment.BRACES,
    zip_code="76010",
    max_distance_mi=15,
    budget_max=5000,
    pay_in_full_ok=True,
    uses_hsa_fsa=True,
    prefers_monthly=True,
    must_haves=["evening hours"],
)


@app.callback()
def main() -> None:
    """Patient Agent (Lana) developer commands."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def _ping_one(model: str) -> tuple[str, float]:
    started = time.perf_counter()
    messages = [{"role": "user", "content": "Say hi in five words."}]
    chunks = [c async for c in stream_chat(messages, model)]
    return "".join(chunks).strip(), time.perf_counter() - started


async def _ping_all() -> bool:
    settings = get_settings()
    ok = True
    for label, model in (("MODEL_FAST", settings.model_fast), ("MODEL_SMART", settings.model_smart)):
        try:
            reply, seconds = await _ping_one(model)
        except Exception as err:
            ok = False
            console.print(f"[red]{label} ({model or 'not set'}): {type(err).__name__}: {err}[/red]")
            continue
        console.print(f"{label} ({model}) {seconds:.2f}s: {reply}")
    return ok


@app.command()
def ping() -> None:
    """Send a short prompt to MODEL_FAST and MODEL_SMART and print each reply and its latency."""
    if not asyncio.run(_ping_all()):
        raise typer.Exit(code=1)


async def _clinic_chat(sim: ClinicSimulator) -> None:
    console.print(f"Talking to {sim.clinic.name}. Type as Lana; 'quit' exits.")
    while True:
        try:
            line = console.input("[bold]LANA>[/bold] ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if line.lower() == "quit":
            return
        if not line:
            continue
        started = time.perf_counter()
        try:
            reply = await sim.next_line(line)
        except Exception as err:
            console.print(f"[red]{type(err).__name__}: {err}[/red]")
            continue
        seconds = time.perf_counter() - started
        hold = " (on hold)" if sim.on_hold else ""
        console.print(f"[magenta]CLINIC>[/magenta] {reply}  [dim]{seconds:.2f}s{hold}[/dim]")


@app.command("clinic-chat")
def clinic_chat(clinic: str = typer.Option(..., help="Demo clinic id, e.g. parkway")) -> None:
    """Chat with a demo clinic receptionist as Lana, one typed line at a time."""
    clinics = {c.id: (c, sheet) for c, sheet in load_demo_clinics()}
    if clinic not in clinics:
        console.print(f"[red]Unknown clinic '{clinic}'. Choose from: {', '.join(clinics)}[/red]")
        raise typer.Exit(code=1)
    asyncio.run(_clinic_chat(ClinicSimulator(*clinics[clinic])))


def _print_line(speaker: Speaker, text: str, t_seconds: float) -> None:
    label = "[bold]LANA[/bold]" if speaker == Speaker.AGENT else "[magenta]CLINIC[/magenta]"
    console.print(f"[dim]\\[{t_seconds:6.1f}s][/dim] {label}: {escape(text)}")


def _value_text(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"${value:,}"
    if isinstance(value, list):
        items = [
            f"{v:%a %b} {v.day} {v.hour % 12 or 12}:{v:%M} {'AM' if v.hour < 12 else 'PM'}"
            if isinstance(v, datetime)
            else str(v)
            for v in value
        ]
        return ", ".join(items) or "none"
    return str(value)


def _print_quote(quote: Quote) -> None:
    console.print(f"\n[bold]Quote for {quote.clinic_id}[/bold]")
    for name in CHECKLIST_ORDER:
        answer = getattr(quote.checklist, name)
        if answer is None:
            console.print(f"  {name}: [yellow]Not confirmed[/yellow]")
            continue
        console.print(
            f"  {name}: {escape(_value_text(answer.value))}  "
            f"[dim]\"{escape(answer.evidence)}\" at {answer.t_seconds:.1f}s[/dim]"
        )
    all_in = f"${quote.all_in_price:,}" if quote.all_in_price is not None else "Not confirmed"
    console.print(f"  all-in price: [bold]{all_in}[/bold]")
    console.print(f"  solid: {'yes' if quote.is_solid else 'no'}")
    console.print(f"  missing: {', '.join(quote.missing_fields) or 'none'}")


async def _simulate_call(clinic, sheet) -> None:
    caller = Caller(clinic, DEMO_PREFERENCES, _print_line)
    console.print(f"Calling {clinic.name} (call {caller.transcript.call_id}).\n")
    result = await caller.run(ClinicSimulator(clinic, sheet))
    _print_quote(build_quote(clinic.id, result.checklist))


@app.command("simulate-call")
def simulate_call(
    clinic: str = typer.Option(..., help="Demo clinic id, e.g. sunrise"),
    text_only: bool = typer.Option(False, "--text-only", help="Text lines only, no audio"),
) -> None:
    """Run Lana's checklist call against a demo clinic and print the transcript and Quote."""
    if not text_only:
        console.print("[red]Voice mode is not built yet. Run with --text-only.[/red]")
        raise typer.Exit(code=1)
    clinics = {c.id: (c, sheet) for c, sheet in load_demo_clinics()}
    if clinic not in clinics:
        console.print(f"[red]Unknown clinic '{clinic}'. Choose from: {', '.join(clinics)}[/red]")
        raise typer.Exit(code=1)
    asyncio.run(_simulate_call(*clinics[clinic]))


async def _negotiate(quotes: list[Quote], live: bool) -> None:
    clinics = get_public_clinics()
    for q in quotes:
        price = f"${q.all_in_price:,}" if q.all_in_price is not None else "Not confirmed"
        console.print(f"  {q.clinic_id}: {price} all in")
    target_id = pick_target(quotes, clinics, DEMO_PREFERENCES)
    if target_id is None:
        console.print("\nNo target: the best fit already has the cheapest solid quote.")
        return
    target = next(q for q in quotes if q.clinic_id == target_id)
    console.print(f"\n[bold]Target: {target_id}[/bold]")

    moves = plan_moves(target, quotes, clinics, DEMO_PREFERENCES)
    for i, move in enumerate(moves, start=1):
        cites = f" (cites {move.cited_clinic}, ${move.cited_amount:,})" if move.cited_clinic else ""
        console.print(f"\n{i}. {move.label}{escape(cites)}")
        console.print(f"   [bold]LANA[/bold]: {escape(await phrase_move(move))}")

    if not live:
        return
    sims = {c.id: (c, sheet) for c, sheet in load_demo_clinics()}
    if target_id not in sims:
        console.print(f"[red]No demo receptionist for '{target_id}'.[/red]")
        raise typer.Exit(code=1)
    console.print(f"\n[bold]Live negotiation with {sims[target_id][0].name}[/bold]\n")
    result = await run_negotiation(
        target, moves, ClinicSimulator(*sims[target_id]), _print_line, call_id=f"negotiate-{target_id}"
    )
    console.print("\n[bold]Concessions won[/bold]")
    for c in result.concessions:
        console.print(
            f"  {c.label}: ${c.amount:,}  [dim]\"{escape(c.evidence.evidence)}\" "
            f"at {c.evidence.t_seconds:.1f}s[/dim]"
        )
    if not result.concessions:
        console.print("  none")
    console.print(f"  refused: {', '.join(result.refused) or 'none'}")
    console.print(
        f"  all in: ${result.first_all_in:,} -> ${result.final_all_in:,}, "
        f"[bold]saved ${result.total_saved:,}[/bold]"
    )


@app.command()
def negotiate(
    fixture: Path = typer.Option(..., exists=True, dir_okay=False, help="JSON list of Quotes"),
    live: bool = typer.Option(False, "--live", help="Also run the moves against the demo receptionist"),
) -> None:
    """Pick the negotiation target from saved quotes, plan the moves and phrase each one."""
    quotes = [Quote.model_validate(q) for q in json.loads(fixture.read_text(encoding="utf-8"))]
    asyncio.run(_negotiate(quotes, live))


def _money(amount: int | None) -> str:
    return f"${amount:,}" if amount is not None else "Not confirmed"


class _EventPrinter:
    """Prints orchestrator events: LANA green-yellow, CLINIC purple, logs dim, states bold."""

    def __init__(self) -> None:
        self.call_clinics: dict[str, str] = {}

    def __call__(self, event) -> None:
        if isinstance(event, TranscriptFinal):
            clinic = self.call_clinics.get(event.call_id or "", "")
            if event.speaker == Speaker.AGENT:
                label = "[bold green_yellow]LANA[/bold green_yellow]"
                text = f"[green_yellow]{escape(event.text)}[/green_yellow]"
            else:
                label = "[bold medium_purple1]CLINIC[/bold medium_purple1]"
                text = f"[medium_purple1]{escape(event.text)}[/medium_purple1]"
            console.print(f"[dim]\\[{clinic:>8} {event.t_seconds:6.1f}s][/dim] {label}: {text}")
        elif isinstance(event, CaseStatusEvent):
            console.print(f"\n[bold]== {event.status.value.upper()} ==[/bold]")
        elif isinstance(event, LogLine):
            console.print(f"[dim]  {escape(event.text)}[/dim]")
        elif isinstance(event, CallStatusEvent):
            self.call_clinics[event.call_id] = event.clinic_id
            console.print(f"[dim]  call {event.call_id} ({event.clinic_id}): {event.status.value}[/dim]")
        elif isinstance(event, QuoteUpdated):
            q = event.quote
            console.print(
                f"[dim]  quote {q.clinic_id}: {_money(q.all_in_price)} all in, "
                f"missing {', '.join(q.missing_fields) or 'nothing'}[/dim]"
            )
        elif isinstance(event, ConcessionWon):
            c = event.concession
            amount = f"-${c.amount:,}" if c.amount else "included"
            console.print(
                f"[bold green]  Concession: {escape(c.label)} {amount} "
                f"(now {_money(event.new_all_in)})[/bold green]"
            )
        elif isinstance(event, OffersReady):
            console.print(f"[dim]  {len(event.offers)} offers ready[/dim]")
        elif isinstance(event, ErrorEvent):
            console.print(f"[red]Error: {escape(event.message)}[/red]")


def _print_offers(case: Case) -> None:
    console.print("\n[bold]Ranked offers[/bold]")
    if not case.offers:
        console.print("  none passed the filters")
    for offer in case.offers:
        quoted = offer.quote.all_in_price
        final = offer_all_in(offer.quote, offer.negotiation)
        price = _money(final)
        if final != quoted:
            price = f"[strike]{_money(quoted)}[/strike] {price}"
        console.print(
            f"  {offer.rank}. [bold]{escape(offer.clinic.name)}[/bold]  {price} all in  "
            f"[dim]score {offer.score:.2f}[/dim]"
        )
        for line in offer.tradeoffs:
            console.print(f"     - {escape(line)}")
        if offer.unknowns:
            console.print(f"     [yellow]Not confirmed: {', '.join(offer.unknowns)}[/yellow]")


def _print_savings(case: Case) -> None:
    console.print("\n[bold]Savings breakdown[/bold]")
    deal = case.negotiation
    if deal is None:
        console.print("  No negotiation: the best-fit clinic already had the lowest price.")
        return
    name = next((c.name for c in case.clinics if c.id == deal.clinic_id), deal.clinic_id)
    console.print(f"  {escape(name)}")
    console.print(f"  First all-in:  {_money(deal.first_all_in)}")
    for c in deal.concessions:
        amount = f"-${c.amount:,}" if c.amount else "included"
        console.print(
            f"  {escape(c.label)}: {amount}  "
            f"[dim]\"{escape(c.evidence.evidence)}\" at {c.evidence.t_seconds:.1f}s[/dim]"
        )
    if not deal.concessions:
        console.print("  No concessions won")
    if deal.refused:
        console.print(f"  [dim]Refused: {', '.join(deal.refused)}[/dim]")
    console.print(f"  Final all-in:  {_money(deal.final_all_in)}")
    console.print(f"  [bold green]Total saved:   ${deal.total_saved:,}[/bold green]")


@app.command("run-demo")
def run_demo(
    mode: DemoMode = typer.Option(DemoMode.WATCH, "--mode", help="Demo mode; only watch is built"),
    text_only: bool = typer.Option(False, "--text-only", help="Text lines only, no audio"),
    clinics: int = typer.Option(3, "--clinics", min=2, max=3, help="How many clinics to call"),
) -> None:
    """Run a whole case: research, calls, hold and resume, negotiation, ranked offers."""
    if mode != DemoMode.WATCH:
        console.print(f"[red]Mode '{mode.value}' is not built yet. Use --mode watch.[/red]")
        raise typer.Exit(code=1)
    if not text_only:
        console.print("[red]Voice mode is not built yet. Run with --text-only.[/red]")
        raise typer.Exit(code=1)
    _run_case(DEMO_PREFERENCES, mode, clinics)


def _run_case(prefs: Preferences, mode: DemoMode, clinics: int) -> None:
    case = Case(preferences=prefs, mode=mode)
    asyncio.run(Orchestrator(case, _EventPrinter(), max_clinics=clinics).run())
    if case.status == CaseStatus.ERROR:
        raise typer.Exit(code=1)
    _print_offers(case)
    _print_savings(case)


def _print_lana(text: str) -> None:
    console.print(f"[bold green_yellow]LANA[/bold green_yellow]: [green_yellow]{escape(text)}[/green_yellow]")


@app.command("intake-text")
def intake_text(text: str = typer.Argument(..., help="One thing the patient says")) -> None:
    """Run one patient reply through intake and print what it filled and the next question."""
    intake = Intake()
    turn = asyncio.run(intake.hear(text))
    console.print(f"[bold]Filled:[/bold] {', '.join(turn.filled) or 'nothing'}")
    console.print("[bold]Draft:[/bold]")
    console.print_json(intake.draft.model_dump_json())
    console.print(f"[bold]Next question:[/bold] {escape(turn.next_question or 'none, intake is done')}")
    _print_lana(turn.spoken)


async def _intake_chat(intake: Intake) -> bool:
    """Typed back-and-forth until intake is done. False if the patient quit first."""
    _print_lana(intake.first_line())
    while True:
        try:
            line = console.input("[bold]YOU>[/bold] ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if line.lower() == "quit":
            return False
        if not line:
            continue
        try:
            turn = await intake.hear(line)
        except Exception as err:
            console.print(f"[red]{type(err).__name__}: {escape(str(err))}. Try typing that again.[/red]")
            continue
        if turn.filled:
            console.print(f"[dim]  filled: {', '.join(turn.filled)}[/dim]")
        _print_lana(turn.spoken)
        if turn.done:
            return True


@app.command("intake-chat")
def intake_chat(
    run: bool = typer.Option(False, "--run", help="Then run the text demo with these preferences"),
) -> None:
    """Chat with Lana by typing until intake is done, then print the final Preferences."""
    intake = Intake()
    if not asyncio.run(_intake_chat(intake)):
        raise typer.Exit(code=1)
    prefs = intake.to_preferences()
    console.print("\n[bold]Preferences[/bold]")
    console.print_json(prefs.model_dump_json())
    if run:
        _run_case(prefs, DemoMode.WATCH, clinics=2)


if __name__ == "__main__":
    app()
