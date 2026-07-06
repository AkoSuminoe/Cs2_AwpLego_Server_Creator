"""Tests for gameinfo.gi patching and CSSharp/BAT config generation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config_patcher import (
    METAMOD_CHECK,
    is_safe_batch_value,
    is_valid_gslt,
    is_valid_port,
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("cleanPass123", True),
        ("de_dust2", True),
        ("has space", False),
        ("amp&ersand", False),
        ("pipe|char", False),
        ("redirect>out", False),
        ("caret^escape", False),
        ("percent%var", False),
        ("bang!delayed", False),
        ('quo"te', False),
    ],
    ids=[
        "alnum_ok", "map_name_ok", "space", "ampersand", "pipe",
        "redirect", "caret", "percent", "bang", "quote",
    ],
)
def test_is_safe_batch_value_classification(value: str, expected: bool) -> None:
    """cmd.exe metacharacters and whitespace must be rejected; plain values pass."""
    assert is_safe_batch_value(value) is expected


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6", True),
        ("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", True),
        ("  A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6  ", True),
        ("A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D", False),
        ("G1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6", False),
        ("", False),
    ],
    ids=["upper_hex", "lower_hex", "stripped", "too_short", "non_hex", "empty"],
)
def test_is_valid_gslt_format(token: str, expected: bool) -> None:
    """A GSLT is exactly 32 hex characters, surrounding whitespace ignored."""
    assert is_valid_gslt(token) is expected


@pytest.mark.parametrize(
    ("port", "expected"),
    [(1024, True), (27015, True), (65535, True), (1023, False), (65536, False), (0, False)],
    ids=["lower_bound", "default", "upper_bound", "below_range", "above_range", "zero"],
)
def test_is_valid_port_range(port: int, expected: bool) -> None:
    """Only the unprivileged addressable range must be accepted."""
    assert is_valid_port(port) is expected


def test_write_server_configs_rejects_unsafe_rcon_password(tmp_path: Path) -> None:
    """A password that would break cmd parsing must fail before any file is written."""
    base_dir = tmp_path / "base"
    server_dir = tmp_path / "cs2_server"
    base_dir.mkdir()
    server_dir.mkdir()

    config = ServerConfig(
        gslt_token="t",
        auth_key="a",
        server_ip="127.0.0.1",
        rcon_password="bad pass&word",
    )

    with pytest.raises(ValueError, match="rcon_password"):
        write_server_configs(base_dir=base_dir, server_dir=server_dir, config=config)
    assert not (server_dir / "game" / "start_server.bat").exists()


def test_write_server_configs_rejects_out_of_range_port(tmp_path: Path) -> None:
    """A privileged or impossible port must be refused at generation time."""
    base_dir = tmp_path / "base"
    server_dir = tmp_path / "cs2_server"
    base_dir.mkdir()
    server_dir.mkdir()

    config = ServerConfig(
        gslt_token="t",
        auth_key="a",
        server_ip="127.0.0.1",
        server_port=80,
    )

    with pytest.raises(ValueError, match="port"):
        write_server_configs(base_dir=base_dir, server_dir=server_dir, config=config)


def test_write_server_configs_omits_empty_credential_flags(tmp_path: Path) -> None:
    """
    Empty credentials must remove their flags entirely: an empty value would
    make the engine consume the next flag as the argument (e.g.
    `+sv_setsteamaccount -authkey` silently eats -authkey).
    """
    base_dir = tmp_path / "base"
    server_dir = tmp_path / "cs2_server"
    base_dir.mkdir()
    server_dir.mkdir()

    config = ServerConfig(
        gslt_token="",
        auth_key="",
        server_ip="127.0.0.1",
        rcon_password="",
    )

    write_server_configs(base_dir=base_dir, server_dir=server_dir, config=config)

    bat = (server_dir / "game" / "start_server.bat").read_text(encoding="utf-8")
    assert "+sv_setsteamaccount" not in bat
    assert "-authkey" not in bat
    assert "-rcon_password" not in bat
    assert "-port 27015" in bat, "Positional flags must survive credential omission"
    assert "-ip 127.0.0.1" in bat


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
