"""
plugin_configs.py — finds where each installed plugin keeps its settings.

There is no single convention: CounterStrikeSharp writes a plugin's config to
configs/plugins/<Name>/<Name>.json on first load, while other plugins ship
editable JSON next to their DLL. Both places are reported so users can open
the right folder instead of guessing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_SUFFIXES = {".json", ".jsonc", ".toml", ".ini", ".cfg", ".yml", ".yaml", ".txt"}
# Build artefacts that look like config but must never be edited.
_IGNORED_SUFFIXES = (".deps.json", ".runtimeconfig.json")


@dataclass
class ConfigLocation:
    name: str
    folder: Path              # the folder to open
    files: list[Path] = field(default_factory=list)
    note: str = ""


def _config_files(folder: Path, recursive: bool) -> list[Path]:
    if not folder.is_dir():
        return []
    pattern = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(
        p for p in pattern
        if p.is_file()
        and p.suffix.lower() in CONFIG_SUFFIXES
        and not p.name.lower().endswith(_IGNORED_SUFFIXES)
        and p.name.lower() != "readme.txt"
        and ".example." not in p.name.lower()
    )


def _find_ci(parent: Path, name: str) -> Optional[Path]:
    """Case-insensitive child lookup (CSSharp uses the module name's casing)."""
    try:
        for child in parent.iterdir():
            if child.is_dir() and child.name.lower() == name.lower():
                return child
    except OSError:
        pass
    return None


def list_config_locations(csgo_dir: Path) -> list[ConfigLocation]:
    css = csgo_dir / "addons" / "counterstrikesharp"
    configs_root = css / "configs" / "plugins"
    locations = [
        ConfigLocation("Server (server.cfg)", csgo_dir / "cfg",
                       [p for p in [csgo_dir / "cfg" / "server.cfg"] if p.is_file()]),
        ConfigLocation("CounterStrikeSharp core", css / "configs",
                       _config_files(css / "configs", recursive=False)),
    ]

    plugins_dir = css / "plugins"
    try:
        plugin_dirs = sorted(
            d for d in plugins_dir.iterdir()
            if d.is_dir() and d.name.lower() != "disabled" and any(d.glob("*.dll"))
        )
    except OSError:
        plugin_dirs = []

    for plugin_dir in plugin_dirs:
        generated = _find_ci(configs_root, plugin_dir.name)
        generated_files = _config_files(generated, recursive=True) if generated else []
        bundled_files = _config_files(plugin_dir, recursive=True)
        if generated_files:
            folder, note = generated, ""
            if bundled_files:
                note = f"+ {len(bundled_files)} file(s) in plugin folder"
        elif bundled_files:
            folder, note = plugin_dir, "settings shipped with the plugin"
        else:
            folder, note = plugin_dir, "no config files (new install? start the server once)"
        locations.append(ConfigLocation(
            plugin_dir.name, folder, generated_files + bundled_files, note
        ))
    return locations


def open_folder(path: Path) -> None:
    """Opens a folder in the OS file manager (Explorer on Windows)."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
