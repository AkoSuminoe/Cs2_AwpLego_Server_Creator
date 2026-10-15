"""Offline tests for the read-only health check (static + live)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core import health_check
from core.health_check import has_failures, parse_start_bat, run_live_check, run_static_checks
from models.schemas import ServerConfig

BAT = """\
@echo off
cd /d "X:\\srv\\cs2_server\\game\\bin\\win64"
start /wait cs2.exe -dedicated -usercon -console -condebug ^
+game_type 3 +game_mode 0 ^
+sv_logfile 1 -serverlogging ^
+sv_setsteamaccount ABCDEF0123456789ABCDEF0123456789 ^
-ip 0.0.0.0 ^
-port 27020 ^
+map awp_lego_2 ^
+exec server.cfg ^
+rcon_password s3cret ^
+sv_cheats 0
"""


def _build_install(base: Path, *, metamod: str = "cs2", bat: str = BAT) -> Path:
    """Creates a minimal on-disk install that passes every static check."""
    (base / "steamcmd").mkdir(parents=True)
    (base / "steamcmd" / "steamcmd.exe").write_bytes(b"MZ")
    server = base / "cs2_server"
    exe = server / "game" / "bin" / "win64" / "cs2.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    (server / "steamapps").mkdir()
    (server / "steamapps" / "appmanifest_730.acf").write_text(
        '"AppState"\n{\n\t"StateFlags"\t\t"4"\n\t"buildid"\t\t"123"\n}\n', encoding="utf-8"
    )
    csgo = server / "game" / "csgo"
    win64 = csgo / "addons" / "metamod" / "bin" / "win64"
    win64.mkdir(parents=True)
    (win64 / ("metamod.2.cs2.dll" if metamod == "cs2" else "metamod.2.css.dll")).write_bytes(b"")
    (csgo / "addons" / "metamod" / "counterstrikesharp.vdf").write_text('"Metamod Plugin" {}', encoding="utf-8")
    css = csgo / "addons" / "counterstrikesharp"
    (css / "bin" / "win64").mkdir(parents=True)
    (css / "bin" / "win64" / "counterstrikesharp.dll").write_bytes(b"")
    (css / "dotnet").mkdir()
    (css / "plugins" / "WeaponPaints").mkdir(parents=True)
    (css / "plugins" / "WeaponPaints" / "WeaponPaints.dll").write_bytes(b"")
    (csgo / "gameinfo.gi").write_text(
        "Game_LowViolence\tcsgo_lv\n\t\t\tGame\tcsgo/addons/metamod\n", encoding="utf-8"
    )
    (csgo / "cfg").mkdir()
    (csgo / "cfg" / "server.cfg").write_text('hostname "x"\n', encoding="utf-8")
    (server / "game" / "start_server.bat").write_text(bat, encoding="utf-8")
    return base


def _by_name(items: list) -> dict[str, Any]:
    return {i.name: i for i in items}


# ---------------------------------------------------------------------------
# parse_start_bat
# ---------------------------------------------------------------------------

def test_parse_start_bat_recovers_launch_settings(tmp_path: Path) -> None:
    bat = tmp_path / "start_server.bat"
    bat.write_text(BAT, encoding="utf-8")
    cfg = parse_start_bat(bat)
    assert cfg is not None
    assert cfg.gslt_token == "ABCDEF0123456789ABCDEF0123456789"
    assert cfg.server_ip == "0.0.0.0"
    assert cfg.server_port == 27020
    assert cfg.map == "awp_lego_2"
    assert cfg.rcon_password == "s3cret"
    assert cfg.auth_key == ""


def test_parse_start_bat_accepts_legacy_rcon_flag(tmp_path: Path) -> None:
    bat = tmp_path / "start_server.bat"
    bat.write_text(BAT.replace("+rcon_password", "-rcon_password"), encoding="utf-8")
    assert parse_start_bat(bat).rcon_password == "s3cret"


def test_parse_start_bat_missing_returns_none(tmp_path: Path) -> None:
    assert parse_start_bat(tmp_path / "nope.bat") is None


# ---------------------------------------------------------------------------
# run_static_checks
# ---------------------------------------------------------------------------

def test_static_checks_all_ok_on_healthy_install(tmp_path: Path) -> None:
    items = run_static_checks(_build_install(tmp_path))
    assert not has_failures(items)
    assert all(i.status == "ok" for i in items), [(i.name, i.detail) for i in items]
    assert "WeaponPaints" in _by_name(items)["Plugins"].detail


def test_static_checks_flag_source1_metamod(tmp_path: Path) -> None:
    items = _by_name(run_static_checks(_build_install(tmp_path, metamod="source1")))
    assert items["Metamod"].status == "fail"
    assert "1.x" in items["Metamod"].detail


def test_static_checks_stop_when_cs2_missing(tmp_path: Path) -> None:
    (tmp_path / "steamcmd").mkdir()
    items = run_static_checks(tmp_path)
    assert has_failures(items)
    assert items[-1].name == "CS2 Server"


def test_static_checks_flag_missing_gameinfo_patch(tmp_path: Path) -> None:
    base = _build_install(tmp_path)
    (base / "cs2_server" / "game" / "csgo" / "gameinfo.gi").write_text("Game csgo\n", encoding="utf-8")
    assert _by_name(run_static_checks(base))["gameinfo.gi"].status == "fail"


def test_static_checks_warn_on_bat_problems(tmp_path: Path) -> None:
    bat = (
        BAT.replace("-ip 0.0.0.0", "-ip 127.0.0.1")
        .replace("+rcon_password", "-rcon_password")
    )
    item = _by_name(run_static_checks(_build_install(tmp_path, bat=bat)))["start_server.bat"]
    assert item.status == "warn"
    assert "127.0.0.1" in item.detail
    assert "-rcon_password" in item.detail


def test_static_checks_warn_on_empty_plugin_folder(tmp_path: Path) -> None:
    base = _build_install(tmp_path)
    (base / "cs2_server/game/csgo/addons/counterstrikesharp/plugins/Broken").mkdir()
    item = _by_name(run_static_checks(base))["Plugins"]
    assert item.status == "warn" and "Broken" in item.detail


# ---------------------------------------------------------------------------
# run_live_check — controller and RCON replaced with fakes
# ---------------------------------------------------------------------------

def _config(**overrides: Any) -> ServerConfig:
    values: dict[str, Any] = {
        "gslt_token": "", "auth_key": "", "server_ip": "0.0.0.0",
        "rcon_password": "pw", "server_port": 27015,
    }
    values.update(overrides)
    return ServerConfig(**values)


class _FakeController:
    instances: list["_FakeController"] = []
    ready = True

    def __init__(self, server_dir: Path, config: ServerConfig) -> None:
        self.started = self.stopped = False
        _FakeController.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def wait_until_ready(self, timeout: float, poll_interval: float) -> bool:
        return _FakeController.ready

    async def stop(self, timeout: float) -> None:
        self.stopped = True


def _fake_rcon(responses: dict[str, str]):
    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def execute(self, command: str) -> str:
            return responses.get(command, "")

    return _Client


@pytest.fixture
def live_env(monkeypatch: pytest.MonkeyPatch):
    _FakeController.instances = []
    _FakeController.ready = True

    async def _not_serving(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(health_check, "ServerController", _FakeController)
    monkeypatch.setattr(health_check, "_is_port_already_serving", _not_serving)
    return monkeypatch


def _run(server_dir: Path, config: ServerConfig):
    return asyncio.run(run_live_check(server_dir, config, load_grace_seconds=0))


def test_live_check_passes_when_both_loaders_answer(tmp_path: Path, live_env) -> None:
    live_env.setattr(health_check, "RCONClient", _fake_rcon({
        "meta version": "Metamod:Source version 2.0.0-dev+1481\nLoaded As: Valve Metamod",
        "css_plugins list": 'Loaded plugins:\n[#1:LOADED]: "WeaponPaints" (1.0)\n',
    }))
    items = _by_name(_run(tmp_path, _config()))
    assert items["Live boot"].status == "ok"
    assert items["Metamod (runtime)"].status == "ok"
    assert "2.0.0" in items["Metamod (runtime)"].detail
    assert items["CounterStrikeSharp (runtime)"].status == "ok"
    assert "1 plugin" in items["CounterStrikeSharp (runtime)"].detail
    assert _FakeController.instances[0].stopped


def test_live_check_fails_when_commands_unknown(tmp_path: Path, live_env) -> None:
    live_env.setattr(health_check, "RCONClient", _fake_rcon({
        "meta version": "Unknown command 'meta'!",
        "css_plugins list": "Unknown command 'css_plugins'!",
    }))
    items = _by_name(_run(tmp_path, _config()))
    assert items["Metamod (runtime)"].status == "fail"
    assert items["CounterStrikeSharp (runtime)"].status == "fail"


def test_live_check_falls_back_to_console_log(tmp_path: Path, live_env) -> None:
    csgo = tmp_path / "game" / "csgo"
    csgo.mkdir(parents=True)
    log = csgo / "console.log"
    log.write_text("old boot: Metamod:Source version 1.12.0\n", encoding="utf-8")

    class _WritingController(_FakeController):
        async def start(self) -> None:
            with log.open("a", encoding="utf-8") as fh:
                fh.write("Metamod:Source version 2.0.0-dev+1481\nCounterStrikeSharp core loaded\n")

    live_env.setattr(health_check, "ServerController", _WritingController)
    live_env.setattr(health_check, "RCONClient", _fake_rcon({}))
    items = _by_name(_run(tmp_path, _config()))
    # Only lines written after the baseline offset count — the stale 1.12 line is ignored.
    assert items["Metamod (runtime)"].status == "ok"
    assert items["CounterStrikeSharp (runtime)"].status == "ok"


def test_live_check_reports_boot_timeout_and_still_stops(tmp_path: Path, live_env) -> None:
    _FakeController.ready = False
    items = _run(tmp_path, _config())
    assert [i.status for i in items] == ["fail"]
    assert _FakeController.instances[0].stopped


def test_live_check_requires_rcon_password(tmp_path: Path, live_env) -> None:
    items = _run(tmp_path, _config(rcon_password=""))
    assert items[0].status == "fail"
    assert not _FakeController.instances


def test_live_check_refuses_occupied_port(tmp_path: Path, live_env) -> None:
    async def _serving(*args: Any, **kwargs: Any) -> bool:
        return True

    live_env.setattr(health_check, "_is_port_already_serving", _serving)
    items = _run(tmp_path, _config())
    assert items[0].status == "fail" and "already" in items[0].detail
    assert not _FakeController.instances


def test_static_checks_flag_missing_cssharp_registration(tmp_path: Path) -> None:
    base = _build_install(tmp_path)
    (base / "cs2_server/game/csgo/addons/metamod/counterstrikesharp.vdf").unlink()
    item = _by_name(run_static_checks(base))["CounterStrikeSharp"]
    assert item.status == "fail" and "counterstrikesharp.vdf" in item.detail


def test_live_check_reads_metamod_version_cvar_from_patched_log_path(tmp_path: Path, live_env) -> None:
    """With gameinfo patched, -condebug writes into addons/metamod/ (the first Game path)."""
    csgo = tmp_path / "game" / "csgo"
    (csgo / "addons" / "metamod").mkdir(parents=True)
    (csgo / "gameinfo.gi").write_text("Game\tcsgo/addons/metamod\n", encoding="utf-8")
    log = csgo / "addons" / "metamod" / "console.log"

    class _WritingController(_FakeController):
        async def start(self) -> None:
            log.write_text(
                'L 10/15/2026: server_cvar: "metamod_version" "2.0.0-dev+1481"\n',
                encoding="utf-8",
            )

    live_env.setattr(health_check, "ServerController", _WritingController)
    live_env.setattr(health_check, "RCONClient", _fake_rcon({
        "meta version": " Metamod:Source Version Information\n",
        "css_plugins list": "Unknown command 'css_plugins'!",
    }))
    items = _by_name(_run(tmp_path, _config()))
    assert items["Metamod (runtime)"].status == "ok"
    assert "2.0.0-dev+1481" in items["Metamod (runtime)"].detail
    assert items["CounterStrikeSharp (runtime)"].status == "fail"



def test_live_check_explains_sourcehook_mismatch(tmp_path: Path, live_env) -> None:
    csgo = tmp_path / "game" / "csgo"
    csgo.mkdir(parents=True)
    log = csgo / "console.log"

    class _WritingController(_FakeController):
        async def start(self) -> None:
            log.write_text(
                '"metamod_version" = "2.0.0-dev+1481"\n'
                "[META] Failed to load plugin addons/counterstrikesharp/bin/win64/counterstrikesharp: "
                "Plugin uses old SourceHook Metamod build, probably 1.12.x or an early 2.0 version (17 < 18).\n",
                encoding="utf-8",
            )

    live_env.setattr(health_check, "ServerController", _WritingController)
    live_env.setattr(health_check, "RCONClient", _fake_rcon({
        "css_plugins list": "Unknown command 'css_plugins'!",
    }))
    item = _by_name(_run(tmp_path, _config()))["CounterStrikeSharp (runtime)"]
    assert item.status == "fail"
    assert "SourceHook" in item.detail


def test_live_check_accepts_cssharp_own_log(tmp_path: Path, live_env) -> None:
    logs = tmp_path / "game" / "csgo" / "addons" / "counterstrikesharp" / "logs"
    logs.mkdir(parents=True)

    class _WritingController(_FakeController):
        async def start(self) -> None:
            (logs / "log-cssharp20261015.txt").write_text(
                "[INFO] (cssharp:Core) CounterStrikeSharp is starting up...\n", encoding="utf-8"
            )

    live_env.setattr(health_check, "ServerController", _WritingController)
    live_env.setattr(health_check, "RCONClient", _fake_rcon({
        "meta version": "Metamod:Source version 2.0.0-dev+1481",
    }))
    item = _by_name(_run(tmp_path, _config()))["CounterStrikeSharp (runtime)"]
    assert item.status == "ok"
