"""Offline tests for SteamCMD progress parsing, retry logic, and preflight checks."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import pytest

from core.steamcmd_wrapper import (
    PROGRESS_RE,
    SteamCMDInstallError,
    _classify_phase,
    ensure_disk_space,
    install_cs2,
)
from models.schemas import SteamCMDEvent, SteamCMDPhase


# ---------------------------------------------------------------------------
# _classify_phase — hex state → phase lookup
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("hex_state", "expected"),
    [
        ("61", SteamCMDPhase.DOWNLOADING),
        ("81", SteamCMDPhase.COMMITTING),
        ("05", SteamCMDPhase.VALIDATING),
        ("61", SteamCMDPhase.DOWNLOADING),  # sanity: case-insensitive lookup
    ],
    ids=["downloading", "committing", "validating", "lowercase_stable"],
)
def test_classify_phase_maps_known_states(
    hex_state: str,
    expected: SteamCMDPhase,
) -> None:
    """Every documented SteamCMD state code must map to the correct phase."""
    assert _classify_phase(hex_state) is expected


@pytest.mark.parametrize(
    "hex_state",
    ["ff", "", "0", "61aa", "xyz"],
    ids=["ff_unknown", "empty_string", "single_digit", "trailing_garbage", "alpha"],
)
def test_classify_phase_falls_back_to_unknown(hex_state: str) -> None:
    """Unrecognized codes must fall back to UNKNOWN — never raise KeyError."""
    assert _classify_phase(hex_state) is SteamCMDPhase.UNKNOWN


# ---------------------------------------------------------------------------
# PROGRESS_RE — SteamCMD stdout line parser
# ---------------------------------------------------------------------------

def test_progress_regex_extracts_state_and_percent() -> None:
    """
    The regex must pull the hex state code and the float percentage
    out of a canonical SteamCMD progress line.
    """
    line = "Update state (0x61) downloading, progress: 47.89 (3232478 / 6750000)"
    match = PROGRESS_RE.search(line)

    assert match is not None
    assert match.group("state") == "61"
    assert float(match.group("pct")) == 47.89


def test_progress_regex_captures_validating_state() -> None:
    """The regex must also match validation phases (state code 0x05)."""
    line = "Update state (0x05) validating, progress: 99.10 (100 / 100)"
    match = PROGRESS_RE.search(line)

    assert match is not None
    assert match.group("state") == "05"


def test_progress_regex_ignores_non_progress_lines() -> None:
    """Any line without the canonical Update state pattern must not match."""
    assert PROGRESS_RE.search("Logging in user...") is None
    assert PROGRESS_RE.search("Success! App 730 fully installed.") is None
    assert PROGRESS_RE.search("") is None


# ---------------------------------------------------------------------------
# ensure_disk_space — install preflight
# ---------------------------------------------------------------------------

def test_ensure_disk_space_passes_with_tiny_requirement(tmp_path: Path) -> None:
    """A requirement of one byte must always clear on a writable volume."""
    ensure_disk_space(tmp_path, required_bytes=1)


def test_ensure_disk_space_raises_when_volume_too_small(tmp_path: Path) -> None:
    """An impossible requirement must fail fast with a readable GB message."""
    with pytest.raises(SteamCMDInstallError, match="Insufficient disk space"):
        ensure_disk_space(tmp_path, required_bytes=10**18)


def test_ensure_disk_space_climbs_to_existing_ancestor(tmp_path: Path) -> None:
    """
    The target directory does not exist yet at preflight time — the check
    must walk up to the nearest existing ancestor instead of raising.
    """
    ensure_disk_space(tmp_path / "not" / "yet" / "created", required_bytes=1)


# ---------------------------------------------------------------------------
# install_cs2 — self-update retry ladder
# ---------------------------------------------------------------------------

_PROGRESS_LINE = "Update state (0x61) downloading, progress: 47.89 (3232478 / 6750000)"


class _FakeStdout:
    """Async line iterator matching asyncio subprocess stdout semantics."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode("utf-8") for line in lines]

    def __aiter__(self) -> "_FakeStdout":
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeSteamCMDProc:
    def __init__(self, returncode: int, lines: Optional[list[str]] = None) -> None:
        self.returncode = returncode
        self.stdout = _FakeStdout(lines or [])

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        return None


def _install_proc_sequence(
    monkeypatch: pytest.MonkeyPatch,
    procs: list[_FakeSteamCMDProc],
) -> list[int]:
    """Feeds one fake process per launch; returns the launch counter list."""
    launches: list[int] = []

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _FakeSteamCMDProc:
        launches.append(1)
        return procs.pop(0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    return launches


def _run_install(tmp_path: Path) -> list[SteamCMDEvent]:
    exe = tmp_path / "steamcmd.exe"
    if not exe.exists():
        exe.write_bytes(b"MZ")

    async def _collect() -> list[SteamCMDEvent]:
        return [event async for event in install_cs2(exe, tmp_path / "server")]

    return asyncio.run(_collect())


def test_install_cs2_retries_once_on_self_update_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Exit code 7 on the first run is SteamCMD's self-update restart signal —
    the wrapper must relaunch exactly once and succeed on the second pass.
    """
    launches = _install_proc_sequence(
        monkeypatch,
        [
            _FakeSteamCMDProc(returncode=7, lines=[_PROGRESS_LINE]),
            _FakeSteamCMDProc(returncode=0, lines=[_PROGRESS_LINE]),
        ],
    )

    events = _run_install(tmp_path)

    assert len(launches) == 2, "Retryable exit code did not trigger a relaunch"
    assert len(events) == 2
    assert events[0].phase is SteamCMDPhase.DOWNLOADING


def test_install_cs2_raises_when_retryable_code_repeats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second consecutive benign exit code means a real failure — no retry loop."""
    launches = _install_proc_sequence(
        monkeypatch,
        [_FakeSteamCMDProc(returncode=7), _FakeSteamCMDProc(returncode=7)],
    )

    with pytest.raises(SteamCMDInstallError, match="exited with code 7"):
        _run_install(tmp_path)
    assert len(launches) == 2


def test_install_cs2_raises_immediately_on_fatal_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-retryable exit codes must fail on the first launch without relaunching."""
    launches = _install_proc_sequence(monkeypatch, [_FakeSteamCMDProc(returncode=8)])

    with pytest.raises(SteamCMDInstallError, match="exited with code 8"):
        _run_install(tmp_path)
    assert len(launches) == 1
