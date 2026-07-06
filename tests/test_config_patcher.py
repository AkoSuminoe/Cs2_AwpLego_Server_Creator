"""Tests for gameinfo.gi patching and CSSharp/BAT config generation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config_patcher import (
    METAMOD_CHECK,
    patch_gameinfo,
    write_databases_json,
    write_server_configs,
)
from models.schemas import DatabaseConfig, ServerConfig


_GAMEINFO_TEMPLATE = """\
"GameInfo"
{
    game    "Counter-Strike 2"
    FileSystem
    {
        SearchPaths
        {
            Game_LowViolence    csgo_lv
            Game                csgo
            Game                core
        }
    }
}
"""


def _write_gameinfo(csgo_dir: Path, content: str = _GAMEINFO_TEMPLATE) -> Path:
    csgo_dir.mkdir(parents=True, exist_ok=True)
    path = csgo_dir / "gameinfo.gi"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# patch_gameinfo — first-time injection
# ---------------------------------------------------------------------------

def test_patch_gameinfo_injects_metamod_entry_after_anchor(tmp_path: Path) -> None:
    """
    The first patch must return True, inject the metamod search path,
    and place it immediately after the Game_LowViolence anchor so the
    engine resolves it before the vanilla csgo path.
    """
    csgo_dir = tmp_path / "game" / "csgo"
    gameinfo = _write_gameinfo(csgo_dir)

    changed = patch_gameinfo(csgo_dir)

    assert changed is True
    patched = gameinfo.read_text(encoding="utf-8")
    assert METAMOD_CHECK in patched, (
        "Metamod search path was not present after patching"
    )

    lines = patched.splitlines()
    anchor_idx = next(i for i, ln in enumerate(lines) if "Game_LowViolence" in ln)
    injected_idx = next(i for i, ln in enumerate(lines) if METAMOD_CHECK in ln)
    assert injected_idx == anchor_idx + 1, (
        "Metamod entry was not inserted immediately after Game_LowViolence"
    )


# ---------------------------------------------------------------------------
# patch_gameinfo — idempotency (the critical guarantee)
# ---------------------------------------------------------------------------

def test_patch_gameinfo_is_idempotent_on_second_call(tmp_path: Path) -> None:
    """
    Re-running the installer must never corrupt gameinfo.gi:
      1. Second call returns False.
      2. Byte content is unchanged.
      3. The metamod marker appears exactly once.
    """
    csgo_dir = tmp_path / "game" / "csgo"
    gameinfo = _write_gameinfo(csgo_dir)

    first = patch_gameinfo(csgo_dir)
    after_first = gameinfo.read_bytes()

    second = patch_gameinfo(csgo_dir)
    after_second = gameinfo.read_bytes()

    assert first is True
    assert second is False, "Second patch call must return False"
    assert after_first == after_second, (
        "Idempotent patch corrupted the file on the second call"
    )
    assert after_second.decode("utf-8").count(METAMOD_CHECK) == 1, (
        "Metamod marker was duplicated"
    )


# ---------------------------------------------------------------------------
# patch_gameinfo — error paths
# ---------------------------------------------------------------------------

def test_patch_gameinfo_raises_when_file_missing(tmp_path: Path) -> None:
    """A missing gameinfo.gi must raise FileNotFoundError — never silently pass."""
    with pytest.raises(FileNotFoundError):
        patch_gameinfo(tmp_path / "empty_csgo")


def test_patch_gameinfo_raises_when_anchor_absent(tmp_path: Path) -> None:
    """A gameinfo.gi lacking the anchor line must raise ValueError."""
    csgo_dir = tmp_path / "game" / "csgo"
    _write_gameinfo(csgo_dir, content='"GameInfo" { game "cs2" }\n')

    with pytest.raises(ValueError):
        patch_gameinfo(csgo_dir)


# ---------------------------------------------------------------------------
# write_databases_json — CSSharp SQL binding
# ---------------------------------------------------------------------------

def test_write_databases_json_produces_expected_default_block(
    tmp_path: Path,
) -> None:
    """
    Every CSSharp SQL plugin reads the 'default' key on startup. The
    generated file must be valid JSON and match the connection block
    byte-for-byte with the DatabaseConfig input.
    """
    csgo_dir = tmp_path / "game" / "csgo"
    db = DatabaseConfig(
        host="10.0.0.5",
        port=3307,
        username="cs2_user",
        password="s3cret",
        database="cs2_ranks",
        enabled=True,
    )

    write_databases_json(csgo_dir, db)

    target = (
        csgo_dir / "addons" / "counterstrikesharp" / "configs" / "databases.json"
    )
    assert target.exists(), "databases.json was not written"

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {
        "default": {
            "Host": "10.0.0.5",
            "Port": 3307,
            "User": "cs2_user",
            "Password": "s3cret",
            "Database": "cs2_ranks",
        }
    }
    assert not target.with_suffix(".tmp").exists(), (
        "Temporary file was left behind — atomic write is broken"
    )


def test_write_databases_json_overwrites_existing_file(tmp_path: Path) -> None:
    """Regenerating with new credentials must fully replace the previous file."""
    csgo_dir = tmp_path / "game" / "csgo"

    write_databases_json(
        csgo_dir,
        DatabaseConfig(host="first-host", database="first_db", enabled=True),
    )
    write_databases_json(
        csgo_dir,
        DatabaseConfig(host="second-host", database="second_db", enabled=True),
    )

    target = (
        csgo_dir / "addons" / "counterstrikesharp" / "configs" / "databases.json"
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["default"]["Host"] == "second-host"
    assert payload["default"]["Database"] == "second_db"


# ---------------------------------------------------------------------------
# write_server_configs — start_server.bat generation
# ---------------------------------------------------------------------------

def test_write_server_configs_interpolates_every_parameter(
    tmp_path: Path,
) -> None:
    """
    start_server.bat must inject every ServerConfig field — GSLT, auth key,
    IP, port, map, and RCON password — so the server boots RCON-ready
    without post-edit.
    """
    base_dir = tmp_path / "base"
    server_dir = tmp_path / "cs2_server"
    base_dir.mkdir()
    server_dir.mkdir()

    config = ServerConfig(
        gslt_token="TOKEN_ABC",
        auth_key="AUTH_XYZ",
        server_ip="203.0.113.10",
        map="de_inferno",
        rcon_password="rc0n_pass",
        server_port=27020,
    )

    write_server_configs(
        base_dir=base_dir,
        server_dir=server_dir,
        config=config,
    )

    bat_path = server_dir / "game" / "start_server.bat"
    assert bat_path.exists(), "start_server.bat was not written"
    bat = bat_path.read_text(encoding="utf-8")

    for expected in (
        "TOKEN_ABC",
        "AUTH_XYZ",
        "203.0.113.10",
        "27020",
        "de_inferno",
        "rc0n_pass",
    ):
        assert expected in bat, f"BAT missing expected parameter: {expected}"


def test_write_server_configs_bat_includes_condebug_flag(
    tmp_path: Path,
) -> None:
    """
    The launch line must carry -condebug so the engine writes console.log —
    the capture channel the runtime verification scanner depends on.
    """
    base_dir = tmp_path / "base"
    server_dir = tmp_path / "cs2_server"
    base_dir.mkdir()
    server_dir.mkdir()

    config = ServerConfig(
        gslt_token="t",
        auth_key="a",
        server_ip="127.0.0.1",
    )

    write_server_configs(base_dir=base_dir, server_dir=server_dir, config=config)

    bat = (server_dir / "game" / "start_server.bat").read_text(encoding="utf-8")
    assert "-condebug" in bat, "console.log capture flag missing from launch line"


def test_write_server_configs_copies_supplied_cfg_template(
    tmp_path: Path,
) -> None:
    """When a server.cfg template is supplied, it must be copied into cfg/."""
    base_dir = tmp_path / "base"
    server_dir = tmp_path / "cs2_server"
    base_dir.mkdir()
    server_dir.mkdir()

    cfg_template = tmp_path / "server.cfg"
    cfg_template.write_text('hostname "Test Server"\n', encoding="utf-8")

    config = ServerConfig(
        gslt_token="t",
        auth_key="a",
        server_ip="127.0.0.1",
    )

    write_server_configs(
        base_dir=base_dir,
        server_dir=server_dir,
        config=config,
        cfg_template_path=cfg_template,
    )

    dest = server_dir / "game" / "csgo" / "cfg" / "server.cfg"
    assert dest.exists(), "server.cfg was not copied into cfg/"
    assert 'hostname "Test Server"' in dest.read_text(encoding="utf-8")
