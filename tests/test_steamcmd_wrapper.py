"""Offline tests for SteamCMD progress parsing and phase classification."""
from __future__ import annotations

import pytest

from core.steamcmd_wrapper import PROGRESS_RE, _classify_phase
from models.schemas import SteamCMDPhase


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
