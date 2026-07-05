from __future__ import annotations

from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.prompt import Prompt
from rich.table import Table

from core.mod_manager import InvalidRepoReferenceError, parse_repo_string
from models.schemas import DatabaseConfig, PluginRef, ServerConfig

console = Console()

_BANNER = """\
  ██████╗███████╗██████╗      ██████╗ ██╗   ██╗████████╗ ██████╗
 ██╔════╝██╔════╝╚════██╗    ██╔═══██╗██║   ██║╚══██╔══╝██╔═══██╗
 ██║     ███████╗ █████╔╝    ██║   ██║██║   ██║   ██║   ██║   ██║
 ██║     ╚════██║██╔═══╝     ██║███╔██║██║   ██║   ██║   ██║   ██║
 ╚██████╗███████║███████╗    ╚██████╔╝╚██████╔╝   ██║   ╚██████╔╝
  ╚═════╝╚══════╝╚══════╝     ╚═════╝  ╚═════╝    ╚═╝    ╚═════╝

    [bold cyan]CS2 Automated Server Setup & Management Tool[/bold cyan]
          [dim]SteamCMD  ·  Metamod  ·  CounterStrikeSharp[/dim]\
"""


def show_banner() -> None:
    console.print(Panel(_BANNER, border_style="cyan", padding=(1, 4)))


def collect_base_dir() -> Path:
    default = str(Path.home() / "cs2_server")
    while True:
        raw = Prompt.ask(
            "\n[bold]Installation directory[/bold]",
            default=default,
            console=console,
        )
        try:
            path = Path(raw).expanduser().resolve()
        except (OSError, ValueError) as exc:
            console.print(f"[red]Invalid path '{raw}': {exc}. Try another path.[/red]")
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.touch()
            probe.unlink()
            return path
        except (OSError, PermissionError) as exc:
            console.print(f"[red]Cannot write to {path}: {exc}. Try another path.[/red]")


def collect_server_config() -> ServerConfig:
    console.rule("[bold cyan]Server Configuration[/bold cyan]")
    console.print(
        "[dim]GSLT: steamcommunity.com/dev/managegameservers\n"
        "Auth key: steamcommunity.com/dev/apikey[/dim]\n"
    )
    gslt = Prompt.ask("[bold]Steam GSLT token[/bold]", password=True, console=console)
    auth_key = Prompt.ask("[bold]Steam Web API key[/bold]", password=True, console=console)
    server_ip = Prompt.ask("[bold]Server IP address[/bold]", console=console)
    server_port_raw = Prompt.ask("[bold]Server port[/bold]", default="27015", console=console)
    try:
        server_port = int(server_port_raw)
    except ValueError:
        server_port = 27015
    server_map = Prompt.ask("[bold]Default map[/bold]", default="de_dust2", console=console)
    rcon_password = Prompt.ask("[bold]RCON password[/bold]", password=True, console=console)

    db_config: Optional[DatabaseConfig] = None
    use_db = Prompt.ask(
        "\n[bold]Configure MySQL database for CSSharp plugins?[/bold] [y/N]",
        default="n",
        console=console,
    ).strip().lower()
    if use_db == "y":
        db_host = Prompt.ask("[bold]  Database host[/bold]", default="127.0.0.1", console=console)
        db_port_raw = Prompt.ask("[bold]  Database port[/bold]", default="3306", console=console)
        try:
            db_port = int(db_port_raw)
        except ValueError:
            db_port = 3306
        db_user = Prompt.ask("[bold]  Database username[/bold]", default="root", console=console)
        db_pass = Prompt.ask("[bold]  Database password[/bold]", password=True, console=console)
        db_name = Prompt.ask("[bold]  Database name[/bold]", default="cs2_server", console=console)
        db_config = DatabaseConfig(
            host=db_host,
            port=db_port,
            username=db_user,
            password=db_pass,
            database=db_name,
            enabled=True,
        )

    return ServerConfig(
        gslt_token=gslt,
        auth_key=auth_key,
        server_ip=server_ip,
        map=server_map,
        rcon_password=rcon_password,
        server_port=server_port,
        db_config=db_config,
    )


def collect_plugins() -> list[PluginRef]:
    console.rule("[bold cyan]Plugin Manager[/bold cyan]")
    console.print(
        "Add CounterStrikeSharp plugins from GitHub.\n"
        "[dim]Format:  owner/repo  or  https://github.com/owner/repo[/dim]\n"
    )

    plugins: list[PluginRef] = []

    while True:
        _render_plugin_table(plugins)
        console.print("\n[A] Add  [R] Remove  [C] Continue\n")
        choice = Prompt.ask("Choice", choices=["a", "r", "c", "A", "R", "C"], console=console).lower()

        if choice == "c":
            break

        if choice == "a":
            raw = Prompt.ask("GitHub repo", console=console)
            ref = _validate_repo_input(raw)
            if ref is None:
                continue
            if any(p.full_ref == ref.full_ref for p in plugins):
                console.print(f"[yellow]'{ref.full_ref}' is already in the list.[/yellow]")
            else:
                plugins.append(ref)
                console.print(f"[green]Added: {ref.full_ref}[/green]")

        elif choice == "r":
            if not plugins:
                console.print("[yellow]No plugins to remove.[/yellow]")
                continue
            num = Prompt.ask(f"Remove plugin number (1–{len(plugins)})", console=console)
            try:
                idx = int(num) - 1
                if 0 <= idx < len(plugins):
                    removed = plugins.pop(idx)
                    console.print(f"[yellow]Removed: {removed.full_ref}[/yellow]")
                else:
                    console.print("[red]Number out of range.[/red]")
            except ValueError:
                console.print("[red]Please enter a valid number.[/red]")

    return plugins


def _render_plugin_table(plugins: list[PluginRef]) -> None:
    if not plugins:
        console.print("[dim]No plugins added yet.[/dim]")
        return

    table = Table(box=box.ROUNDED, border_style="cyan", show_header=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Plugin", style="bold white")
    table.add_column("GitHub Repo", style="cyan")

    for i, p in enumerate(plugins, 1):
        table.add_row(str(i), p.display_name, p.full_ref)

    console.print(table)


def _validate_repo_input(raw: str) -> Optional[PluginRef]:
    try:
        owner, repo = parse_repo_string(raw)
        return PluginRef(owner=owner, repo=repo)
    except InvalidRepoReferenceError as exc:
        console.print(f"[red]Invalid repo: {exc}[/red]")
        return None


class InstallationProgress:
    """
    Context manager wrapping rich.progress.Progress with named task handles.

    The orchestrator calls start_task() for each step, then update_task() as
    progress events arrive, and finally complete_task() or fail_task().
    """

    def __init__(self) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description:<40}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=False,
        )
        self._task_ids: dict[str, TaskID] = {}
        self._task_descs: dict[str, str] = {}

    def __enter__(self) -> InstallationProgress:
        self._progress.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        self._progress.__exit__(*args)

    def start_task(self, key: str, description: str, total: float = 100.0) -> None:
        task_id = self._progress.add_task(description, total=total)
        self._task_ids[key] = task_id
        self._task_descs[key] = description

    def update_task(self, key: str, completed: float) -> None:
        if key in self._task_ids:
            self._progress.update(self._task_ids[key], completed=completed)

    def complete_task(self, key: str) -> None:
        if key in self._task_ids:
            self._progress.update(self._task_ids[key], completed=100.0)

    def fail_task(self, key: str, reason: str) -> None:
        if key in self._task_ids:
            desc = self._task_descs.get(key, key)
            self._progress.update(
                self._task_ids[key],
                description=f"[red]✗ {desc}[/red]",
                completed=0,
            )
            console.print(f"[red]  ERROR ({desc}): {reason}[/red]")


def show_summary(results: list[tuple[str, bool, str]]) -> None:
    console.rule("[bold cyan]Installation Summary[/bold cyan]")

    table = Table(box=box.ROUNDED, border_style="cyan")
    table.add_column("Step", style="bold white")
    table.add_column("Status")
    table.add_column("Details", style="dim")

    for step, success, detail in results:
        status = "[green]✓  Done[/green]" if success else "[red]✗  Failed[/red]"
        table.add_row(step, status, detail)

    console.print(table)

    all_ok = all(ok for _, ok, _ in results)
    if all_ok:
        console.print(
            Panel(
                "[bold green]Installation complete![/bold green]\n"
                "Launch your server by running [cyan]start_server.bat[/cyan] "
                "inside the [cyan]game/[/cyan] folder.",
                border_style="green",
                padding=(1, 4),
            )
        )
    else:
        console.print(
            Panel(
                "[bold red]Installation completed with errors.[/bold red]\n"
                "Re-run the tool — already completed steps will be skipped automatically.",
                border_style="red",
                padding=(1, 4),
            )
        )
