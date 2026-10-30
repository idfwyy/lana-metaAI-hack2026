"""Typer commands to test each piece in the terminal."""

import asyncio
import logging
import time

import typer
from rich.console import Console

from app.clinics.simulator import ClinicSimulator
from app.config import get_settings
from app.data.loader import load_demo_clinics
from app.integrations.meta_llm import stream_chat

app = typer.Typer(no_args_is_help=True)
console = Console()


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


if __name__ == "__main__":
    app()
