"""Workshop map / collection support across prompt, bat, launch args and bat parsing."""
from __future__ import annotations

from pathlib import Path

import pytest

from cli.app import parse_map_input
from core.config_patcher import write_server_configs
from core.health_check import parse_start_bat
from core.server_controller import ServerController
from models.schemas import ServerConfig


@pytest.mark.parametrize("raw, expected", [
    ("de_mirage", ("de_mirage", "", "")),
    ("3070244462", ("de_dust2", "3070244462", "")),
    ("https://steamcommunity.com/sharedfiles/filedetails/?id=3070244462", ("de_dust2", "3070244462", "")),
    ("https://steamcommunity.com/sharedfiles/filedetails/?l=turkish&id=123", ("de_dust2", "123", "")),
    ("c:3086555291", ("de_dust2", "", "3086555291")),
    ("collection:https://steamcommunity.com/sharedfiles/filedetails/?id=99", ("de_dust2", "", "99")),
])
def test_parse_map_input(raw: str, expected: tuple[str, str, str]) -> None:
    assert parse_map_input(raw) == expected


@pytest.mark.parametrize("raw", ["", "c:abc", "de dust2", "map&calc"])
def test_parse_map_input_rejects(raw: str) -> None:
    assert parse_map_input(raw) is None


def _cfg(**kw) -> ServerConfig:
    base = dict(gslt_token="", auth_key="KEY", server_ip="0.0.0.0", rcon_password="pw")
    base.update(kw)
    return ServerConfig(**base)


def test_map_args_precedence() -> None:
    assert _cfg(map="de_inferno").map_args() == ["+map", "de_inferno"]
    assert _cfg(workshop_map="1").map_args() == ["+host_workshop_map", "1"]
    assert _cfg(workshop_collection="2", workshop_map="1").map_args() == [
        "+host_workshop_collection", "2", "+host_workshop_map", "1",
    ]


def test_bat_and_launch_args_agree_and_round_trip(tmp_path: Path) -> None:
    cfg = _cfg(workshop_map="3070244462")
    server = tmp_path / "cs2_server"
    write_server_configs(base_dir=tmp_path, server_dir=server, config=cfg)
    bat = (server / "game" / "start_server.bat").read_text(encoding="utf-8")

    assert "+host_workshop_map 3070244462 ^" in bat
    assert "+map " not in bat
    assert "+sv_lan 0 ^" in bat

    args = ServerController(server, cfg)._build_launch_args()
    assert args[args.index("+host_workshop_map") + 1] == "3070244462"
    assert args[args.index("+sv_lan") + 1] == "0"

    parsed = parse_start_bat(server / "game" / "start_server.bat")
    assert parsed.workshop_map == "3070244462"
    assert parsed.map_args() == cfg.map_args()
