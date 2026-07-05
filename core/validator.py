from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class InstallState:
    schema_version: int = 1
    base_dir: str = ""
    steps: dict = field(default_factory=dict)


class StateManager:
    """
    Thin wrapper around install_state.json.

    Filesystem predicates are always authoritative (checked before each step).
    This JSON is an optimisation layer — deleting it forces a full re-check
    without corrupting anything.
    """

    def __init__(self, state_file: Path) -> None:
        self._path = state_file
        self._tmp = state_file.with_suffix(".tmp")

    def load(self) -> InstallState:
        if not self._path.exists():
            return InstallState()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return InstallState(
                schema_version=data.get("schema_version", 1),
                base_dir=data.get("base_dir", ""),
                steps=data.get("steps", {}),
            )
        except (json.JSONDecodeError, KeyError, OSError):
            return InstallState()

    def save(self, state: InstallState) -> None:
        data = {
            "schema_version": state.schema_version,
            "base_dir": state.base_dir,
            "steps": state.steps,
        }
        # Atomic write: write to .tmp then rename — prevents half-written JSON
        self._tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(self._tmp, self._path)

    def mark_complete(self, step: str, metadata: Optional[dict] = None) -> None:
        state = self.load()
        entry: dict = {
            "status": "complete",
            "completed_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        if metadata:
            entry.update(metadata)
        state.steps[step] = entry
        self.save(state)

    def mark_failed(self, step: str, error: str) -> None:
        state = self.load()
        state.steps[step] = {
            "status": "failed",
            "error": error,
            "failed_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        self.save(state)

    def get_step(self, step: str) -> Optional[dict]:
        return self.load().steps.get(step)


# ---------------------------------------------------------------------------
# Filesystem predicates — always ground truth, used by the orchestrator
# ---------------------------------------------------------------------------

def is_steamcmd_installed(steamcmd_dir: Path) -> bool:
    return (steamcmd_dir / "steamcmd.exe").exists()


def is_cs2_installed(server_dir: Path) -> bool:
    return (server_dir / "game" / "bin" / "win64" / "cs2.exe").exists()


def is_metamod_installed(csgo_dir: Path) -> bool:
    return (csgo_dir / "addons" / "metamod").is_dir()


def is_cssharp_installed(csgo_dir: Path) -> bool:
    return (csgo_dir / "addons" / "counterstrikesharp").is_dir()


def is_gameinfo_patched(csgo_dir: Path) -> bool:
    gameinfo = csgo_dir / "gameinfo.gi"
    if not gameinfo.exists():
        return False
    content = gameinfo.read_text(encoding="utf-8")
    return "csgo/addons/metamod" in content


def is_plugin_installed(plugins_dir: Path, plugin_name: str) -> bool:
    plugin_dir = plugins_dir / plugin_name
    if not plugin_dir.is_dir():
        return False
    return any(plugin_dir.iterdir())
