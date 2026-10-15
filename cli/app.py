from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

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

from core.config_patcher import is_safe_batch_value, is_valid_gslt, is_valid_port
from core.mod_manager import InvalidRepoReferenceError, parse_repo_string
from models.schemas import DatabaseConfig, PluginRef, ServerConfig

if TYPE_CHECKING:
    from core.health_check import CheckItem

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
        "Auth key: steamcommunity.com/dev/apikey\n"
        "Input is visible so you can paste and verify tokens — they are "
        "stored in plain text inside start_server.bat anyway.[/dim]\n"
    )
    while True:
        gslt = Prompt.ask("[bold]Steam GSLT token[/bold]", console=console).strip()
        if not gslt:
            console.print(
                "[yellow]No GSLT — the server will run LAN-only and won't be "
                "publicly listable.[/yellow]"
            )
            break
        if is_valid_gslt(gslt):
            break
        console.print("[red]A GSLT is exactly 32 hex characters. Check the token and retry.[/red]")

    while True:
        auth_key = Prompt.ask("[bold]Steam Web API key[/bold]", console=console).strip()
        if is_safe_batch_value(auth_key):
            break
        console.print("[red]The key contains characters that would break start_server.bat.[/red]")

    while True:
        server_ip = Prompt.ask("[bold]Server IP address[/bold]", console=console).strip()
        if server_ip and is_safe_batch_value(server_ip):
            if server_ip.startswith("127.") or server_ip.lower() == "localhost":
                console.print(
                    "[yellow]Loopback address: only this PC will be able to connect. "
                    "Use 0.0.0.0 or your LAN/public IP for other players.[/yellow]"
                )
            break
        console.print("[red]Enter an IP address without spaces or cmd metacharacters.[/red]")

    while True:
        server_port_raw = Prompt.ask("[bold]Server port[/bold]", default="27015", console=console)
        try:
            server_port = int(server_port_raw)
        except ValueError:
            console.print("[red]Port must be a number.[/red]")
            continue
        if is_valid_port(server_port):
            break
        console.print("[red]Port must be between 1024 and 65535.[/red]")

    while True:
        server_map = Prompt.ask("[bold]Default map[/bold]", default="de_dust2", console=console).strip()
        if server_map and is_safe_batch_value(server_map):
            break
        console.print("[red]Map names cannot contain spaces or cmd metacharacters.[/red]")

    while True:
        rcon_password = Prompt.ask("[bold]RCON password[/bold]", console=console)
        if is_safe_batch_value(rcon_password):
            break
        console.print(
            "[red]The password would break start_server.bat — avoid spaces "
            'and the characters & | < > ^ % ! ".[/red]'
        )

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
        db_pass = Prompt.ask("[bold]  Database password[/bold]", console=console)
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


def collect_runtime_verify() -> bool:
    """Asks whether each plugin install should be verified against the live server."""
    return Prompt.ask(
        "\n[bold]Verify each plugin at runtime after install "
        "(briefly launches the server and scans logs)?[/bold] [y/N]",
        default="n",
        console=console,
    ).strip().lower() == "y"


def collect_plugins() -> list[PluginRef]:
    console.rule("[bold cyan]Plugin Manager[/bold cyan]")
    console.print(
        "Add CounterStrikeSharp plugins from GitHub or from your disk.\n"
        "[dim]GitHub:  owner/repo  or  https://github.com/owner/repo\n"
        "Local:   a plugin folder or .zip path (drag & drop works), or [L] to browse[/dim]\n"
    )

    plugins: list[PluginRef] = []

    while True:
        _render_plugin_table(plugins)
        console.print("\n[A] Add  [L] Local (browse)  [R] Remove  [C] Continue\n")
        choice = Prompt.ask(
            "Choice", choices=["a", "l", "r", "c", "A", "L", "R", "C"], console=console
        ).lower()

        if choice == "c":
            break

        if choice in ("a", "l"):
            if choice == "l":
                raw = pick_local_plugin()
                if raw is None:
                    console.print("[yellow]Nothing selected.[/yellow]")
                    continue
            else:
                raw = Prompt.ask("GitHub repo or local path", console=console)
            ref = validate_plugin_input(raw)
            if ref is None:
                continue
            if any(p.full_ref == ref.full_ref for p in plugins):
                console.print(f"[yellow]'{ref.source}' is already in the list.[/yellow]")
            else:
                plugins.append(ref)
                console.print(f"[green]Added: {ref.source}[/green]")

        elif choice == "r":
            if not plugins:
                console.print("[yellow]No plugins to remove.[/yellow]")
                continue
            num = Prompt.ask(f"Remove plugin number (1–{len(plugins)})", console=console)
            try:
                idx = int(num) - 1
                if 0 <= idx < len(plugins):
                    removed = plugins.pop(idx)
                    console.print(f"[yellow]Removed: {removed.source}[/yellow]")
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
    table.add_column("Type", style="magenta")
    table.add_column("Source", style="cyan")

    for i, p in enumerate(plugins, 1):
        table.add_row(str(i), p.display_name, "Local" if p.local_path else "GitHub", p.source)

    console.print(table)


def validate_plugin_input(raw: str) -> Optional[PluginRef]:
    """
    An existing folder or .zip on disk becomes a local plugin; anything else
    must parse as a GitHub reference. Quotes added by drag & drop are stripped.
    """
    cleaned = raw.strip().strip('"').strip("'")
    if not cleaned:
        console.print("[red]Enter a GitHub repo or a local path.[/red]")
        return None
    try:
        path = Path(cleaned).expanduser()
        is_local = path.exists()
    except (OSError, ValueError):
        is_local = False
    if is_local:
        if path.is_file() and path.suffix.lower() != ".zip":
            console.print("[red]Local plugins must be a folder or a .zip file.[/red]")
            return None
        resolved = path.resolve()
        return PluginRef(owner="local", repo=resolved.stem, local_path=str(resolved))
    try:
        owner, repo = parse_repo_string(cleaned)
        return PluginRef(owner=owner, repo=repo)
    except InvalidRepoReferenceError as exc:
        console.print(f"[red]Not a local path and not a valid repo: {exc}[/red]")
        return None


def pick_local_plugin() -> Optional[str]:
    """
    Opens a native file dialog for a plugin .zip, falling back to a folder
    dialog when cancelled. Returns None if nothing was chosen; drops back to
    a typed path when no GUI is available (e.g. over SSH).
    """
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return Prompt.ask("Local plugin path (folder or .zip)", console=console) or None

    try:
        root = tkinter.Tk()
    except tkinter.TclError:
        return Prompt.ask("Local plugin path (folder or .zip)", console=console) or None
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askopenfilename(
            title="Select a plugin .zip (Cancel to pick a folder instead)",
            filetypes=[("Plugin archive", "*.zip")],
            parent=root,
        )
        if not chosen:
            chosen = filedialog.askdirectory(title="Select a plugin folder", parent=root)
        return chosen or None
    finally:
        root.destroy()


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

    def note_task(self, key: str, note: str) -> None:
        """Shows a dim, truncated live status next to the task description."""
        if key in self._task_ids:
            desc = self._task_descs.get(key, key)
            snippet = note.strip()[:48]
            self._progress.update(
                self._task_ids[key],
                description=f"{desc} [dim]{snippet}[/dim]",
            )

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


_CHECK_STATUS = {
    "ok": "[green]✓  OK[/green]",
    "warn": "[yellow]!  Warning[/yellow]",
    "fail": "[red]✗  Failed[/red]",
}


def show_check_report(items: list[CheckItem], title: str = "Health Check") -> None:
    console.rule(f"[bold cyan]{title}[/bold cyan]")
    table = Table(box=box.ROUNDED, border_style="cyan")
    table.add_column("Check", style="bold white")
    table.add_column("Status")
    table.add_column("Details", style="dim")
    for item in items:
        table.add_row(item.name, _CHECK_STATUS[item.status], item.detail)
    console.print(table)
