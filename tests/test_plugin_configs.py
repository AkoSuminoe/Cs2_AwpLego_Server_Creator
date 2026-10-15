"""Offline tests for locating plugin config files and the config browser."""
from __future__ import annotations

from pathlib import Path

import pytest

from cli import app as cli_app
from core.plugin_configs import list_config_locations


def _plugin(csgo: Path, name: str, extra: dict[str, str] | None = None) -> Path:
    d = csgo / "addons" / "counterstrikesharp" / "plugins" / name
    d.mkdir(parents=True)
    (d / f"{name}.dll").write_bytes(b"")
    (d / f"{name}.deps.json").write_text("{}")
    for rel, content in (extra or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(content)
    return d


@pytest.fixture
def csgo(tmp_path: Path) -> Path:
    csgo = tmp_path / "csgo"
    (csgo / "cfg").mkdir(parents=True)
    (csgo / "cfg" / "server.cfg").write_text('hostname "x"')
    configs = csgo / "addons" / "counterstrikesharp" / "configs"
    (configs / "plugins").mkdir(parents=True)
    (configs / "core.json").write_text("{}")
    (configs / "core.example.json").write_text("{}")
    return csgo


def _by_name(locs):
    return {loc.name: loc for loc in locs}


def test_generated_config_folder_wins(csgo: Path) -> None:
    _plugin(csgo, "AutoRestart", {"lang/en.json": "{}"})
    gen = csgo / "addons/counterstrikesharp/configs/plugins/AutoRestart"
    gen.mkdir()
    (gen / "AutoRestart.json").write_text("{}")

    loc = _by_name(list_config_locations(csgo))["AutoRestart"]
    assert loc.folder == gen
    assert [f.name for f in loc.files] == ["AutoRestart.json", "en.json"]
    assert "plugin folder" in loc.note


def test_generated_folder_matched_case_insensitively(csgo: Path) -> None:
    _plugin(csgo, "WeaponPaints")
    gen = csgo / "addons/counterstrikesharp/configs/plugins/weaponpaints"
    gen.mkdir()
    (gen / "WeaponPaints.json").write_text("{}")
    assert _by_name(list_config_locations(csgo))["WeaponPaints"].folder == gen


def test_bundled_settings_point_to_plugin_folder(csgo: Path) -> None:
    plugin = _plugin(csgo, "BotRandomizer", {"cosmetic_catalog.json": "{}"})
    loc = _by_name(list_config_locations(csgo))["BotRandomizer"]
    assert loc.folder == plugin
    assert [f.name for f in loc.files] == ["cosmetic_catalog.json"]


def test_build_artefacts_and_examples_are_not_configs(csgo: Path) -> None:
    _plugin(csgo, "Bare", {"Bare.runtimeconfig.json": "{}"})
    locs = _by_name(list_config_locations(csgo))
    assert locs["Bare"].files == []
    assert "start the server" in locs["Bare"].note
    assert [f.name for f in locs["CounterStrikeSharp core"].files] == ["core.json"]


def test_server_and_core_rows_come_first(csgo: Path) -> None:
    _plugin(csgo, "Zeta")
    names = [loc.name for loc in list_config_locations(csgo)]
    assert names[:2] == ["Server (server.cfg)", "CounterStrikeSharp core"]
    assert names[2:] == ["Zeta"]


def test_folder_without_dll_is_ignored(csgo: Path) -> None:
    (csgo / "addons/counterstrikesharp/plugins/disabled").mkdir(parents=True)
    (csgo / "addons/counterstrikesharp/plugins/notes").mkdir()
    assert len(list_config_locations(csgo)) == 2


def test_browse_configs_opens_selected_folder(csgo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plugin(csgo, "AutoRestart")
    answers = iter(["99", "3", ""])  # invalid → valid → exit
    monkeypatch.setattr(cli_app.Prompt, "ask", lambda *a, **k: next(answers))
    opened: list[Path] = []

    cli_app.browse_configs(list_config_locations(csgo), csgo, opened.append)

    assert opened == [csgo / "addons/counterstrikesharp/plugins/AutoRestart"]


def test_short_path_drops_cssharp_prefix(csgo: Path) -> None:
    p = csgo / "addons" / "counterstrikesharp" / "configs" / "plugins" / "X"
    assert cli_app._short(p, csgo) == "configs/plugins/X"
    assert cli_app._short(csgo / "cfg", csgo) == "cfg"
