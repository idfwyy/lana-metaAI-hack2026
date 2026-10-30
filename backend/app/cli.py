"""Typer commands to test each piece in the terminal."""

import asyncio
import logging
import time

from datetime import datetime

import typer
from rich.console import Console
from rich.markup import escape

from app.agent.caller import Caller
from app.agent.extractor import CHECKLIST_ORDER, build_quote
from app.clinics.simulator import ClinicSimulator
from app.config import get_settings
from app.data.loader import load_demo_clinics
from app.integrations.meta_llm import stream_chat
from app.schemas.enums import Speaker, Treatment
from app.schemas.preferences import Preferences
from app.schemas.quote import Quote

app = typer.Typer(no_args_is_help=True)
console = Console()

# Fake intake answers until intake is built (the example from SETUP.md section 3.10).
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


if __name__ == "__main__":
    app()
