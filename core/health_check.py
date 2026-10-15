"""
health_check.py — inspects an existing install without reinstalling anything.

Static checks read the filesystem only (seconds, nothing launched). The live
check boots cs2.exe exactly like the runtime verifier, asks Metamod and
CounterStrikeSharp to identify themselves over RCON, then shuts down.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from core.log_validator import console_log_path
from core.rcon_manager import RCONAuthError, RCONClient, RCONConnectionError
from core.server_controller import (
    ServerControlError,
    ServerController,
    _is_port_already_serving,
    rcon_host_for,
)
from core.validator import (
    has_stale_metamod,
    is_cs2_installed,
    is_cssharp_installed,
    is_gameinfo_patched,
    is_metamod_installed,
    is_steamcmd_installed,
)
from models.schemas import ServerConfig

CheckStatus = Literal["ok", "warn", "fail"]

_MM_VERSION_RE = re.compile(
    r'(?:Metamod:Source version\s+|"metamod_version"\s+=?\s*")(\d[\w.+-]*)', re.IGNORECASE
)
_CSS_LOADED_RE = re.compile(r"\[#\d+:LOADED\]", re.IGNORECASE)
_SOURCEHOOK_MISMATCH_RE = re.compile(r"Failed to load plugin .*counterstrikesharp.*old SourceHook", re.IGNORECASE)
_CSS_BOOT_RE = re.compile(r"CounterStrikeSharp.{0,80}\b(?:loaded|initialized|starting up)\b", re.IGNORECASE)


@dataclass
class CheckItem:
    name: str
    status: CheckStatus
    detail: str


def has_failures(items: list[CheckItem]) -> bool:
    return any(item.status == "fail" for item in items)


# ---------------------------------------------------------------------------
# start_server.bat parsing
# ---------------------------------------------------------------------------

def parse_start_bat(bat_path: Path) -> Optional[ServerConfig]:
    """
    Recovers the launch settings written by write_server_configs. Returns
    None when the file is missing or unreadable.
    """
    try:
        text = bat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    tokens = text.replace("^", " ").split()

    def _value(*flags: str) -> str:
        for i, tok in enumerate(tokens[:-1]):
            if tok in flags:
                return tokens[i + 1]
        return ""

    try:
        port = int(_value("-port") or 27015)
    except ValueError:
        port = 27015
    return ServerConfig(
        gslt_token=_value("+sv_setsteamaccount"),
        auth_key=_value("-authkey"),
        server_ip=_value("-ip"),
        map=_value("+map") or "de_dust2",
        rcon_password=_value("+rcon_password", "-rcon_password"),
        server_port=port,
    )


def _read_manifest(server_dir: Path) -> dict[str, str]:
    manifest = server_dir / "steamapps" / "appmanifest_730.acf"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return dict(re.findall(r'^\s*"(\w+)"\s+"([^"]*)"', text, re.MULTILINE))


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

def run_static_checks(base_dir: Path) -> list[CheckItem]:
    steamcmd_dir = base_dir / "steamcmd"
    server_dir = base_dir / "cs2_server"
    csgo_dir = server_dir / "game" / "csgo"
    items: list[CheckItem] = []

    if is_steamcmd_installed(steamcmd_dir):
        items.append(CheckItem("SteamCMD", "ok", str(steamcmd_dir)))
    else:
        items.append(CheckItem("SteamCMD", "warn", "Missing — needed only for --update"))

    if not is_cs2_installed(server_dir):
        items.append(CheckItem("CS2 Server", "fail", f"cs2.exe not found under {server_dir}"))
        return items  # everything below lives inside the CS2 tree
    manifest = _read_manifest(server_dir)
    build = manifest.get("buildid", "?")
    if manifest.get("StateFlags", "4") == "4":
        items.append(CheckItem("CS2 Server", "ok", f"Fully installed, build {build}"))
    else:
        items.append(CheckItem(
            "CS2 Server", "warn",
            f"Steam state flags {manifest.get('StateFlags')} (not 'fully installed') — run --update",
        ))

    if is_metamod_installed(csgo_dir):
        items.append(CheckItem("Metamod", "ok", "Metamod:Source 2.0 (CS2 loader present)"))
    elif has_stale_metamod(csgo_dir):
        items.append(CheckItem(
            "Metamod", "fail",
            "Metamod 1.x (Source 1 only) — re-run the installer to replace it with 2.0",
        ))
    else:
        items.append(CheckItem("Metamod", "fail", "Not installed"))

    css_dir = csgo_dir / "addons" / "counterstrikesharp"
    if not is_cssharp_installed(csgo_dir):
        items.append(CheckItem("CounterStrikeSharp", "fail", "Not installed"))
    elif not (css_dir / "bin" / "win64" / "counterstrikesharp.dll").is_file():
        items.append(CheckItem("CounterStrikeSharp", "fail", "counterstrikesharp.dll missing"))
    elif not (csgo_dir / "addons" / "metamod" / "counterstrikesharp.vdf").is_file():
        items.append(CheckItem(
            "CounterStrikeSharp", "fail",
            "addons/metamod/counterstrikesharp.vdf missing — Metamod won't load it",
        ))
    elif not (css_dir / "dotnet").is_dir():
        items.append(CheckItem("CounterStrikeSharp", "fail", ".NET runtime folder missing (need with-runtime build)"))
    else:
        items.append(CheckItem("CounterStrikeSharp", "ok", "Core DLL + .NET runtime present"))

    if is_gameinfo_patched(csgo_dir):
        items.append(CheckItem("gameinfo.gi", "ok", "Metamod search path present"))
    else:
        items.append(CheckItem("gameinfo.gi", "fail", "Metamod patch missing — run --update"))

    bat = server_dir / "game" / "start_server.bat"
    cfg = parse_start_bat(bat)
    if cfg is None:
        items.append(CheckItem("start_server.bat", "fail", f"Missing: {bat}"))
    else:
        problems: list[str] = []
        try:
            bat_text = bat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            bat_text = ""
        if "-rcon_password" in bat_text:
            problems.append("legacy -rcon_password flag (use +rcon_password)")
        if cfg.server_ip.startswith("127."):
            problems.append(f"-ip {cfg.server_ip}: only this PC can connect")
        if not cfg.gslt_token:
            problems.append("no GSLT token: server won't be listed publicly")
        if not cfg.rcon_password:
            problems.append("no RCON password: --check --live can't run")
        if problems:
            items.append(CheckItem("start_server.bat", "warn", "; ".join(problems)))
        else:
            items.append(CheckItem(
                "start_server.bat", "ok", f"{cfg.server_ip}:{cfg.server_port}, map {cfg.map}"
            ))

    if (csgo_dir / "cfg" / "server.cfg").is_file():
        items.append(CheckItem("server.cfg", "ok", "Present"))
    else:
        items.append(CheckItem("server.cfg", "warn", "Missing — engine defaults will be used"))

    plugins_dir = css_dir / "plugins"
    try:
        plugin_dirs = [d for d in plugins_dir.iterdir() if d.is_dir() and d.name.lower() != "disabled"]
    except OSError:
        plugin_dirs = []
    empty = [d.name for d in plugin_dirs if not any(d.iterdir())]
    if empty:
        items.append(CheckItem("Plugins", "warn", f"Empty plugin folders: {', '.join(empty)}"))
    else:
        names = ", ".join(d.name for d in plugin_dirs) or "none installed"
        items.append(CheckItem("Plugins", "ok", f"{len(plugin_dirs)}: {names}"))

    return items


# ---------------------------------------------------------------------------
# Live check
# ---------------------------------------------------------------------------

def _read_console_since(csgo_dir: Path, offset: int) -> str:
    path = console_log_path(csgo_dir)
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _installed_plugin_names(csgo_dir: Path) -> list[str]:
    plugins_dir = csgo_dir / "addons" / "counterstrikesharp" / "plugins"
    try:
        return sorted(
            d.name for d in plugins_dir.iterdir()
            if d.is_dir() and d.name.lower() != "disabled" and any(d.glob("*.dll"))
        )
    except OSError:
        return []


_LOADING_RE = re.compile(r"Loading plugin (\S+)")
_FAILED_FROM_RE = re.compile(r"Failed to load plugin from .*?[\\/]plugins[\\/]([^\\/]+)[\\/]", re.IGNORECASE)
_EXC_MSG_RE = re.compile(r"\w+Exception: (.+)")
_WRAPPER_EXC = "Exception has been thrown by the target of an invocation"


def plugin_load_results(text: str) -> tuple[set[str], dict[str, str]]:
    """
    Walks CSSharp's log in order. 'Loading plugin <Folder>' is followed by
    either 'Finished loading plugin <Display Name>' (the display name often
    differs from the folder) or 'Failed to load plugin from …/plugins/<Folder>/'
    with the real exception a few lines below. Returns (loaded folder names
    lowercased, {folder: reason}).
    """
    loaded: set[str] = set()
    failed: dict[str, str] = {}
    lines = text.splitlines()
    current: Optional[str] = None
    for i, line in enumerate(lines):
        failure = _FAILED_FROM_RE.search(line)
        if failure:
            name = failure.group(1)
            reason = "see CSSharp log"
            for follow in lines[i + 1:i + 8]:
                m = _EXC_MSG_RE.search(follow)
                if m and _WRAPPER_EXC not in m.group(1):
                    reason = m.group(1).strip()[:160]
                    break
            failed[name] = reason
            loaded.discard(name.lower())
            current = None
            continue
        start = _LOADING_RE.search(line)
        if start and "Finished" not in line:
            current = start.group(1)
        elif "Finished loading plugin" in line and current:
            loaded.add(current.lower())
            failed.pop(current, None)
            current = None
    return loaded, failed


def _read_cssharp_logs_since(csgo_dir: Path, since_epoch: float) -> str:
    """Text of CSSharp's own log files touched during this boot."""
    logs_dir = csgo_dir / "addons" / "counterstrikesharp" / "logs"
    chunks: list[str] = []
    try:
        for path in logs_dir.glob("log-cssharp*.txt"):
            # Windows mtimes are coarse and can trail time.time() slightly.
            if path.stat().st_mtime >= since_epoch - 2.0:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    return "\n".join(chunks)


async def run_live_check(
    server_dir: Path,
    config: ServerConfig,
    *,
    ready_timeout: float = 180.0,
    poll_interval: float = 3.0,
    load_grace_seconds: float = 5.0,
    stop_timeout: float = 15.0,
) -> list[CheckItem]:
    """
    Boots the server, queries `meta version` and `css_plugins list` over RCON,
    and always shuts the process down before returning.
    """
    if not config.rcon_password:
        return [CheckItem("Live boot", "fail", "start_server.bat has no RCON password")]

    host = rcon_host_for(config.server_ip)
    if await _is_port_already_serving(config.server_port, config.rcon_password, host=host):
        return [CheckItem(
            "Live boot", "fail",
            f"Port {config.server_port} already answers RCON — stop the running server first",
        )]

    csgo_dir = server_dir / "game" / "csgo"
    try:
        offset = console_log_path(csgo_dir).stat().st_size
    except OSError:
        offset = 0
    started_at = time.time()

    items: list[CheckItem] = []
    meta_out = css_out = ""
    controller = ServerController(server_dir, config)
    try:
        await controller.start()
    except ServerControlError as exc:
        return [CheckItem("Live boot", "fail", str(exc))]
    try:
        ready = await controller.wait_until_ready(timeout=ready_timeout, poll_interval=poll_interval)
        if not ready:
            items.append(CheckItem(
                "Live boot", "fail",
                f"No RCON answer within {ready_timeout:.0f}s (crash, wrong password, or slow boot)",
            ))
        else:
            items.append(CheckItem("Live boot", "ok", f"RCON answered on {host}:{config.server_port}"))
            if load_grace_seconds > 0:
                await asyncio.sleep(load_grace_seconds)
            try:
                async with RCONClient(host, config.server_port, config.rcon_password) as rcon:
                    meta_out = await rcon.execute("meta version")
                    css_out = await rcon.execute("css_plugins list")
            except (RCONAuthError, RCONConnectionError) as exc:
                items.append(CheckItem("RCON", "fail", str(exc)))
    finally:
        await controller.stop(timeout=stop_timeout)

    if not items or items[0].status != "ok":
        return items

    # RCON output capture is unreliable for some plugin prints, so the logs
    # written during this boot are consulted as a fallback.
    combined = "\n".join((
        meta_out,
        css_out,
        _read_console_since(csgo_dir, offset),
        _read_cssharp_logs_since(csgo_dir, started_at),
    ))

    m = _MM_VERSION_RE.search(combined)
    if m and m.group(1).startswith("2."):
        items.append(CheckItem("Metamod (runtime)", "ok", f"Loaded, version {m.group(1)}"))
    elif m:
        items.append(CheckItem("Metamod (runtime)", "fail", f"Wrong version loaded: {m.group(1)}"))
    else:
        items.append(CheckItem("Metamod (runtime)", "fail", "Not loaded — 'meta version' unknown"))

    loaded = len(_CSS_LOADED_RE.findall(css_out))
    installed = _installed_plugin_names(csgo_dir)
    loaded_names, failures = plugin_load_results(combined)
    not_loaded = [
        f"{n} ({failures[n]})" if n in failures else n
        for n in installed if n.lower() not in loaded_names
    ]
    css_answered = bool(css_out.strip()) and "unknown command" not in css_out.lower()
    if loaded or css_answered or _CSS_BOOT_RE.search(combined):
        detail = f"Loaded, {loaded} plugin(s) reported LOADED" if loaded else "Loaded (no plugins listed)"
        items.append(CheckItem("CounterStrikeSharp (runtime)", "ok", detail))
        if installed:
            if not_loaded:
                items.append(CheckItem(
                    "Plugins (runtime)", "fail",
                    f"Installed but not loaded: {', '.join(not_loaded)}",
                ))
            else:
                items.append(CheckItem(
                    "Plugins (runtime)", "ok", f"All loaded: {', '.join(installed)}"
                ))
    elif _SOURCEHOOK_MISMATCH_RE.search(combined):
        items.append(CheckItem(
            "CounterStrikeSharp (runtime)", "fail",
            "Rejected by Metamod: built for an older SourceHook — update CounterStrikeSharp",
        ))
    else:
        items.append(CheckItem(
            "CounterStrikeSharp (runtime)", "fail", "Not loaded — 'css_plugins' unknown"
        ))
    return items
