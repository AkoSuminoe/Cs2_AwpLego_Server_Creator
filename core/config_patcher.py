from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from models.schemas import DatabaseConfig, ServerConfig

METAMOD_GAME_ENTRY = "\t\t\tGame\tcsgo/addons/metamod\n"
GAMEINFO_ANCHOR = "Game_LowViolence"
METAMOD_CHECK = "csgo/addons/metamod"

# cmd.exe metacharacters plus whitespace — any of these inside an unquoted
# BAT_TEMPLATE field either splits the argument or hijacks the command line.
_BAT_UNSAFE_RE = re.compile(r'[&|<>^%!"\s]')

# Valve GSLT tokens are exactly 32 hex characters.
_GSLT_RE = re.compile(r"[0-9A-Fa-f]{32}\Z")


def is_safe_batch_value(value: str) -> bool:
    """True when value can be embedded in start_server.bat without breaking cmd parsing."""
    return not _BAT_UNSAFE_RE.search(value)


def is_valid_gslt(token: str) -> bool:
    """True when token matches Valve's 32-hex GSLT format."""
    return bool(_GSLT_RE.fullmatch(token.strip()))


def is_valid_port(port: int) -> bool:
    """True when port sits in the unprivileged, addressable range."""
    return 1024 <= port <= 65535

BAT_TEMPLATE = """\
@echo off
cd /d "{server_dir}\\game\\bin\\win64"
start /wait cs2.exe -dedicated -usercon -console -condebug ^
+game_type 3 +game_mode 0 ^
+sv_logfile 1 -serverlogging ^
+sv_setsteamaccount {gslt_token} ^
-authkey {auth_key} ^
-ip {server_ip} ^
-port {server_port} ^
+map {map} ^
+exec server.cfg ^
-rcon_password {rcon_password} ^
+sv_kick_players_with_cooldown 0 ^
+sv_cheats 0
"""


def patch_gameinfo(csgo_dir: Path) -> bool:
    """
    Idempotently injects the Metamod search path into gameinfo.gi.

    Finds the Game_LowViolence anchor line and inserts the metamod entry
    on the next line. Returns True if a change was made, False if already
    patched. Raises FileNotFoundError if gameinfo.gi does not exist yet
    (CS2 not installed).
    """
    gameinfo_path = csgo_dir / "gameinfo.gi"
    if not gameinfo_path.exists():
        raise FileNotFoundError(
            f"gameinfo.gi not found at {gameinfo_path}. "
            "Make sure CS2 is fully installed before patching."
        )

    try:
        content = gameinfo_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OSError(f"Failed to read gameinfo.gi: {exc}") from exc

    if METAMOD_CHECK in content:
        return False  # Already patched

    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if GAMEINFO_ANCHOR in line:
            lines.insert(i + 1, METAMOD_GAME_ENTRY)
            break
    else:
        raise ValueError(
            f"Could not find '{GAMEINFO_ANCHOR}' anchor in gameinfo.gi. "
            "The file may be corrupted or from an unexpected CS2 version."
        )

    try:
        gameinfo_path.write_text("".join(lines), encoding="utf-8")
    except OSError as exc:
        raise OSError(f"Failed to write gameinfo.gi: {exc}") from exc
    return True


def write_server_configs(
    base_dir: Path,
    server_dir: Path,
    config: ServerConfig,
    cfg_template_path: Optional[Path] = None,
) -> None:
    """
    Writes start_server.bat and copies server.cfg into place.

    Both operations are idempotent — safe to call on every run.
    """
    unsafe = [
        name
        for name, value in (
            ("gslt_token", config.gslt_token),
            ("auth_key", config.auth_key),
            ("server_ip", config.server_ip),
            ("map", config.map),
            ("rcon_password", config.rcon_password),
        )
        if value and not is_safe_batch_value(value)
    ]
    if unsafe:
        raise ValueError(
            "Values contain characters unsafe for start_server.bat "
            f"(whitespace or cmd metacharacters): {', '.join(unsafe)}"
        )
    if not is_valid_port(config.server_port):
        raise ValueError(
            f"Server port out of range (1024-65535): {config.server_port}"
        )

    game_dir = server_dir / "game"
    try:
        game_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"Cannot create game directory {game_dir}: {exc}") from exc

    bat_content = BAT_TEMPLATE.format(
        server_dir=str(server_dir),
        gslt_token=config.gslt_token,
        auth_key=config.auth_key,
        server_ip=config.server_ip,
        server_port=config.server_port,
        map=config.map,
        rcon_password=config.rcon_password,
    )
    try:
        (game_dir / "start_server.bat").write_text(bat_content, encoding="utf-8")
    except OSError as exc:
        raise OSError(f"Failed to write start_server.bat: {exc}") from exc

    cfg_dir = server_dir / "game" / "csgo" / "cfg"
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"Cannot create cfg directory {cfg_dir}: {exc}") from exc

    if cfg_template_path is None:
        cfg_template_path = base_dir / "server.cfg"

    if cfg_template_path.exists():
        try:
            shutil.copy2(cfg_template_path, cfg_dir / "server.cfg")
        except (OSError, shutil.SameFileError) as exc:
            raise OSError(f"Failed to copy server.cfg: {exc}") from exc


def write_databases_json(csgo_dir: Path, db: DatabaseConfig) -> None:
    configs_dir = csgo_dir / "addons" / "counterstrikesharp" / "configs"
    try:
        configs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"Cannot create CSSharp configs directory: {exc}") from exc

    target = configs_dir / "databases.json"
    payload = {
        "default": {
            "Host": db.host,
            "Port": db.port,
            "User": db.username,
            "Password": db.password,
            "Database": db.database,
        }
    }
    tmp = target.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise OSError(f"Failed to write databases.json: {exc}") from exc
