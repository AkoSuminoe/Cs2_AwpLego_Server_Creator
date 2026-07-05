"""Tests for filesystem install predicates and StateManager persistence."""
from __future__ import annotations

import json
from pathlib import Path

from core.validator import (
    StateManager,
    is_cs2_installed,
    is_cssharp_installed,
    is_metamod_installed,
    is_steamcmd_installed,
)


# ---------------------------------------------------------------------------
# Filesystem predicates
# ---------------------------------------------------------------------------

def test_is_steamcmd_installed_true_when_exe_exists(tmp_path: Path) -> None:
    (tmp_path / "steamcmd.exe").write_bytes(b"MZ\x00\x00")
    assert is_steamcmd_installed(tmp_path) is True


def test_is_steamcmd_installed_false_when_directory_empty(tmp_path: Path) -> None:
    assert is_steamcmd_installed(tmp_path) is False


def test_is_cs2_installed_true_when_binary_present(tmp_path: Path) -> None:
    exe = tmp_path / "game" / "bin" / "win64" / "cs2.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ\x00\x00")
    assert is_cs2_installed(tmp_path) is True


def test_is_cs2_installed_false_when_binary_missing(tmp_path: Path) -> None:
    (tmp_path / "game" / "bin" / "win64").mkdir(parents=True)
    assert is_cs2_installed(tmp_path) is False


def test_is_metamod_installed_reflects_directory_presence(tmp_path: Path) -> None:
    assert is_metamod_installed(tmp_path) is False
    (tmp_path / "addons" / "metamod").mkdir(parents=True)
    assert is_metamod_installed(tmp_path) is True


def test_is_cssharp_installed_reflects_directory_presence(tmp_path: Path) -> None:
    assert is_cssharp_installed(tmp_path) is False
    (tmp_path / "addons" / "counterstrikesharp").mkdir(parents=True)
    assert is_cssharp_installed(tmp_path) is True


# ---------------------------------------------------------------------------
# StateManager — default state
# ---------------------------------------------------------------------------

def test_state_manager_load_returns_default_when_missing(tmp_path: Path) -> None:
    """
    A never-written state file must load as an empty default —
    the installer treats this as 'nothing done yet, start fresh'.
    """
    mgr = StateManager(tmp_path / "install_state.json")
    state = mgr.load()
    assert state.schema_version == 1
    assert state.steps == {}


def test_state_manager_load_returns_default_when_json_is_corrupt(
    tmp_path: Path,
) -> None:
    """Corrupt JSON must not crash — silently reset to default."""
    state_path = tmp_path / "install_state.json"
    state_path.write_text("garbage-not-json", encoding="utf-8")

    state = StateManager(state_path).load()

    assert state.steps == {}


# ---------------------------------------------------------------------------
# mark_complete
# ---------------------------------------------------------------------------

def test_state_manager_mark_complete_persists_step_atomically(
    tmp_path: Path,
) -> None:
    """
    mark_complete() must write to disk atomically — the tmp file must not
    survive the operation and the entry must include a status timestamp.
    """
    state_path = tmp_path / "install_state.json"
    mgr = StateManager(state_path)

    mgr.mark_complete("steamcmd_downloaded")

    assert state_path.exists(), "State file was not created"
    assert not state_path.with_suffix(".tmp").exists(), (
        "Temporary file was left behind — atomic write is broken"
    )
    entry = mgr.get_step("steamcmd_downloaded")
    assert entry is not None
    assert entry["status"] == "complete"
    assert "completed_at" in entry


def test_state_manager_mark_complete_merges_metadata(tmp_path: Path) -> None:
    """Extra metadata must be merged into the step entry."""
    mgr = StateManager(tmp_path / "install_state.json")
    mgr.mark_complete("metamod_installed", metadata={"version": "2.0.1"})

    entry = mgr.get_step("metamod_installed")
    assert entry is not None
    assert entry["status"] == "complete"
    assert entry["version"] == "2.0.1"


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------

def test_state_manager_mark_failed_records_error(tmp_path: Path) -> None:
    """
    mark_failed() must capture status, error message, and timestamp so
    operators can diagnose which phase went wrong without console scroll.
    """
    mgr = StateManager(tmp_path / "install_state.json")
    mgr.mark_failed("cs2_install", error="SteamCMD exit code 8")

    entry = mgr.get_step("cs2_install")
    assert entry is not None
    assert entry["status"] == "failed"
    assert entry["error"] == "SteamCMD exit code 8"
    assert "failed_at" in entry


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_state_manager_roundtrip_across_instances(tmp_path: Path) -> None:
    """
    State saved by one manager must be readable by a freshly constructed
    manager instance — the JSON is the durable interface between runs.
    """
    state_path = tmp_path / "install_state.json"

    first = StateManager(state_path)
    first.mark_complete("step_a", metadata={"foo": "bar"})
    first.mark_failed("step_b", error="boom")

    second = StateManager(state_path)
    state = second.load()

    assert state.steps["step_a"]["status"] == "complete"
    assert state.steps["step_a"]["foo"] == "bar"
    assert state.steps["step_b"]["status"] == "failed"
    assert state.steps["step_b"]["error"] == "boom"

    parsed = json.loads(state_path.read_text(encoding="utf-8"))
    assert "step_a" in parsed["steps"]
    assert "step_b" in parsed["steps"]
