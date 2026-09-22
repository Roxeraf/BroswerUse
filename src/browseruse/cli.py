"""The chat interface: you type, the browser moves."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from browseruse.agent.jev import JevAdvisor
from browseruse.agent.loop import BrowserAgent
from browseruse.agent.replay import RecipeRunner
from browseruse.browser.launcher import BrowserLaunchError
from browseruse.browser.session import BrowserSession
from browseruse.config import Config
from browseruse.recipes import Recipe, RecipeBook, learn
from browseruse.safety import Judgement

console = Console()

HELP = """\
Type what you want done in plain language, e.g.
  find the cheapest direct flight from Vienna to Lisbon in October
  open my GitHub notifications and tell me which ones mention me

Commands
  /tabs              list open tabs
  /goto <url>        open a URL without involving the agent
  /readonly          toggle read-only mode (every click needs a yes)
  /risk <0-3>        set the confirmation threshold (default 1.5)
  /cost              what this session has spent so far

Learned procedures
  /learn <name>      remember the last successful task under this name
  /recipes           list what has been learned
  /run <name> [k=v]  repeat a procedure from memory, without asking Claude
  /forget <name>     delete one
  /browser           show which browser is attached
  /help              this text
  /quit              detach and exit
"""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="browseruse",
        description="Drive your own Brave or Chrome by chat.",
    )
    parser.add_argument("--browser", choices=["brave", "chrome"], help="Which browser to drive.")
    parser.add_argument(
        "--real-profile",
        action="store_true",
        help="Attach to your everyday profile instead of a separate automation one. "
        "The browser must be fully quit first.",
    )
    parser.add_argument("--headless", action="store_true", help="Run without a visible window.")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Start in read-only mode: navigation is free, every click needs a yes.",
    )
    parser.add_argument("--port", type=int, help="CDP port (default 9222).")
    parser.add_argument("task", nargs="*", help="Run one task and exit instead of chatting.")
    return parser.parse_args(argv)


def _config_from(args: argparse.Namespace) -> Config:
    base = Config.from_env()
    overrides: dict[str, Any] = {}
    if args.browser:
        overrides["browser"] = args.browser
    if args.real_profile:
        overrides["real_profile"] = True
    if args.headless:
        overrides["headless"] = True
    if args.port:
        overrides["cdp_port"] = args.port
    return Config(**{**base.__dict__, **overrides})


def _reporter(kind: str, text: str) -> None:
    if kind == "say":
        console.print(text, end="", highlight=False, markup=False)
    elif kind == "act":
        console.print(f"  [dim]->[/dim] {text}", highlight=False)
    elif kind == "auto":
        console.print(f"  [magenta]~>[/magenta] {text}", highlight=False)
    elif kind == "info":
        console.print(f"  [dim cyan]i {text}[/dim cyan]", highlight=False)
    elif kind == "warn":
        console.print(f"  [yellow]! {text}[/yellow]", highlight=False)
    elif kind == "block":
        console.print(f"  [red]x {text}[/red]", highlight=False)


async def _ask_approval(
    verdict: Judgement, action: str, args: dict[str, Any], target: str
) -> bool:
    console.print()
    detail = target or ", ".join(f"{k}={v!r}" for k, v in args.items() if k != "target_description")
    body = f"[bold]{action}[/bold]  {detail}\n\n[dim]{verdict.reason}[/dim]"
    console.print(
        Panel(
            body,
            title="[yellow]Approve this?[/yellow]",
            border_style="yellow",
            expand=False,
        )
    )
    return await asyncio.to_thread(Confirm.ask, "  Go ahead?", default=False)


async def _show_tabs(session: BrowserSession) -> None:
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("#", justify="right")
    table.add_column("Title")
    table.add_column("URL", style="dim")
    for tab in await session.tab_infos():
        marker = "*" if tab.active else ""
        table.add_row(f"{tab.index}{marker}", tab.title[:60], tab.url[:70])
    console.print(table)


async def _show_recipes(book: RecipeBook) -> None:
    recipes = book.all()
    if not recipes:
        console.print(
            "  [dim]Nothing learned yet. Finish a task, then /learn <name> to keep it.[/dim]"
        )
        return
    for recipe in recipes:
        console.print(f"  {recipe.summary()}")


async def _replay(
    runner: RecipeRunner, book: RecipeBook, recipe: Recipe, parameters: dict[str, str],
    agent: BrowserAgent,
) -> None:
    console.print(f"\n[dim]Replaying {recipe.name} -- {len(recipe.steps)} remembered steps[/dim]")
    report = await runner.run(recipe, parameters)
    book.record_run(recipe, succeeded=report.ok)

    if report.ok:
        console.print(Panel(
            f"{report.reason} {report.completed}/{report.total} steps, no Claude turns.\n"
            f"[dim]cost {report.cost.one_line() if report.cost else ''}[/dim]",
            border_style="green", expand=False))
        return

    console.print(Panel(
        f"Stopped at step {report.completed + 1} of {report.total}.\n{report.reason}",
        title="[yellow]Recipe no longer fits[/yellow]", border_style="yellow", expand=False))
    if await asyncio.to_thread(Confirm.ask, "  Let Claude do it instead?", default=True):
        await _do_task(agent, recipe.goal)


async def _handle_command(
    line: str, session: BrowserSession, agent: BrowserAgent, config: Config,
    book: RecipeBook, runner: RecipeRunner, memory: dict,
) -> bool:
    """Returns False when the user asked to quit."""
    command, _, rest = line[1:].partition(" ")
    rest = rest.strip()

    match command:
        case "learn":
            if not rest:
                console.print("[yellow]Usage: /learn <name>[/yellow]")
            elif not memory.get("steps"):
                console.print("[yellow]Nothing to learn -- finish a task first.[/yellow]")
            else:
                recipe = learn(rest, memory["goal"], memory["steps"])
                if not recipe.steps:
                    console.print("[yellow]That run had no repeatable steps.[/yellow]")
                else:
                    path = book.save(recipe)
                    console.print(
                        f"  Learned [bold]{recipe.name}[/bold]: {len(recipe.steps)} steps.\n"
                        f"  [dim]{path}  -- edit it to add {{{{placeholders}}}}[/dim]"
                    )
        case "recipes":
            await _show_recipes(book)
        case "run":
            name, *pairs = rest.split() or [""]
            recipe = book.load(name) if name else None
            if recipe is None:
                console.print(f"[yellow]No recipe called {name!r}. Try /recipes.[/yellow]")
            else:
                parameters = dict(
                    pair.split("=", 1) for pair in pairs if "=" in pair
                )
                await _replay(runner, book, recipe, parameters, agent)
        case "forget":
            console.print(
                f"  Forgot [bold]{rest}[/bold]." if book.delete(rest)
                else f"[yellow]No recipe called {rest!r}.[/yellow]"
            )
        case "quit" | "exit" | "q":
            return False
        case "help" | "h":
            console.print(HELP)
        case "tabs":
            await _show_tabs(session)
        case "goto":
            if not rest:
                console.print("[yellow]Usage: /goto <url>[/yellow]")
            else:
                await session.goto(rest)
                console.print(f"  [dim]->[/dim] {session.page.url}")
        case "readonly":
            agent.read_only_mode = not agent.read_only_mode
            state = "on" if agent.read_only_mode else "off"
            console.print(f"  Read-only mode is [bold]{state}[/bold].")
        case "risk":
            try:
                agent.risk_threshold = float(rest)
                console.print(f"  Confirming anything scoring >= [bold]{agent.risk_threshold}[/bold].")
            except ValueError:
                console.print("[yellow]Usage: /risk <number between 0 and 3>[/yellow]")
        case "cost":
            console.print()
            console.print(Panel(agent.meter.detail(), title="Session spend",
                                border_style="cyan", expand=False))
        case "browser":
            console.print(
                f"  {config.browser.title()} on CDP port {config.cdp_port}, "
                f"{'your real profile' if config.real_profile else 'a separate automation profile'}."
            )
        case _:
            console.print(f"[yellow]Unknown command /{command}. Try /help.[/yellow]")
    return True


async def _run(args: argparse.Namespace) -> int:
    config = _config_from(args)

    if not config.anthropic_api_key:
        console.print("[red]ANTHROPIC_API_KEY is not set.[/red] Copy .env.example to .env.")
        return 1

    jev: JevAdvisor | None = None
    if config.typesafe_api_key:
        jev = JevAdvisor(config)
    else:
        console.print(
            "[yellow]TYPESAFE_API_KEY is not set, so Jev is off: no risk scoring, no page "
            "classification, and every action that changes something will ask first.[/yellow]"
        )

    try:
        session = BrowserSession(config)
        await session.start()
    except BrowserLaunchError as exc:
        console.print(f"[red]{exc}[/red]")
        if jev is not None:
            await jev.aclose()
        return 1

    agent = BrowserAgent(config, session, jev, approve=_ask_approval, report=_reporter)
    agent.read_only_mode = args.read_only
    book = RecipeBook(config.state_dir / "recipes")
    runner = RecipeRunner(
        config, session, jev, approve=_ask_approval, report=_reporter, meter=agent.meter
    )
    runner.read_only_mode = args.read_only
    #: The last task that finished, so /learn has something to remember.
    memory: dict = {}

    console.print(
        Panel(
            f"Attached to [bold]{config.browser.title()}[/bold] at {session.page.url}\n"
            f"Jev: {'on' if jev else 'off'}"
            f"{' (autopilot)' if jev and config.fast_path else ''}   "
            f"Confirm at risk >= {config.risk_threshold}   "
            f"Read-only: {'on' if agent.read_only_mode else 'off'}   "
            f"Recipes: {len(book.all())}\n\n"
            "[dim]/help for commands, /quit to leave.[/dim]",
            title="browseruse",
            border_style="cyan",
            expand=False,
        )
    )

    try:
        if args.task:
            await _do_task(agent, " ".join(args.task), memory)
            return 0

        while True:
            try:
                line = (await asyncio.to_thread(Prompt.ask, "\n[bold cyan]you[/bold cyan]")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line.startswith("/"):
                if not await _handle_command(
                    line, session, agent, config, book, runner, memory
                ):
                    break
                continue
            await _do_task(agent, line, memory)
    finally:
        await session.close()
        if jev is not None:
            await jev.aclose()
        if agent.meter.claude.calls:
            console.print()
            console.print(Panel(agent.meter.detail(), title="Session spend",
                                border_style="cyan", expand=False))
        console.print("\n[dim]Detached. Your browser is still open.[/dim]")
    return 0


async def _do_task(agent: BrowserAgent, goal: str, memory: dict | None = None) -> None:
    console.print()
    try:
        report = await agent.run(goal)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
        return
    except Exception as exc:  # noqa: BLE001 - the REPL must survive a bad task
        console.print(f"\n[red]{type(exc).__name__}: {exc}[/red]")
        return
    if memory is not None and report.stopped_because == "done":
        memory["goal"], memory["steps"] = goal, report.steps
    console.print()
    console.print(Panel(report.summary, border_style="green", expand=False))
    trailer = []
    if report.steps:
        trailer.append(f"{len(report.steps)} step(s), stopped because: {report.stopped_because}")
    if report.cost is not None and report.cost.claude.calls:
        trailer.append(f"cost {report.cost.one_line()}")
    if trailer:
        console.print(f"[dim]{'  |  '.join(trailer)}[/dim]")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
