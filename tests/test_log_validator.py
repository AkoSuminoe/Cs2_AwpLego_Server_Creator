"""Offline tests for the runtime log baseline/scan contract."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from core.log_validator import (
    CONSOLE_LOG_NAME,
    CSSHARP_LOGS_SUBDIR,
    _MAX_REPORTED_ERRORS,
    capture_baseline,
    scan_latest_logs,
    scan_runtime_logs,
)
from models.schemas import LogScanBaseline, LogValidationResult


# ---------------------------------------------------------------------------
# capture_baseline — console.log high-water mark
# ---------------------------------------------------------------------------

def test_capture_baseline_offset_zero_when_console_log_absent(tmp_path: Path) -> None:
    """No console.log yet (first-ever launch) must start scanning from byte 0."""
    baseline = capture_baseline(tmp_path)
    assert baseline.console_log_offset == 0


def test_capture_baseline_offset_equals_file_size_when_present(tmp_path: Path) -> None:
    """An existing console.log must be treated as pre-baseline noise, in full."""
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text("some prior startup log content\n", encoding="utf-8")

    baseline = capture_baseline(tmp_path)

    assert baseline.console_log_offset == console.stat().st_size


def test_capture_baseline_captured_at_epoch_is_recent(tmp_path: Path) -> None:
    """captured_at_epoch must be a real wall-clock reading taken during the call."""
    before = time.time()
    baseline = capture_baseline(tmp_path)
    after = time.time()

    assert before <= baseline.captured_at_epoch <= after


# ---------------------------------------------------------------------------
# scan_runtime_logs — success / error line detection
# ---------------------------------------------------------------------------

def test_clean_load_line_reports_success_with_no_errors(tmp_path: Path) -> None:
    """A plain CSSharp load line with no fatal vocabulary must count as success."""
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text(
        "CounterStrikeSharp... Loaded plugin MyPlugin v1.0\n", encoding="utf-8"
    )
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.success is True
    assert result.errors_detected == []


def test_exception_line_reports_failure_with_errors(tmp_path: Path) -> None:
    """A raw exception line must be picked up by the unconditional Exception pattern."""
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text(
        "System.NullReferenceException: Object reference not set\n", encoding="utf-8"
    )
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.success is False
    assert result.errors_detected != []


def test_load_line_and_exception_line_together_keep_success_false(tmp_path: Path) -> None:
    """A detected error must veto success even when the load signature also appears."""
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text(
        "CounterStrikeSharp... Loaded plugin MyPlugin v1.0\n"
        "System.NullReferenceException: Object reference not set\n",
        encoding="utf-8",
    )
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.success is False, (
        "An error signature must override a co-occurring success signature"
    )
    assert result.errors_detected != []


@pytest.mark.parametrize(
    "phrase",
    [
        "Failed to load plugin MyPlugin",
        "Unable to load plugin MyPlugin",
        "Could not load MyPlugin dependencies",
        "Missing dependency for MyPlugin",
    ],
    ids=["failed_to_load", "unable_to_load", "could_not_load", "missing_dependency"],
)
def test_global_fatal_phrases_are_flagged_as_errors(tmp_path: Path, phrase: str) -> None:
    """Each global-fatal phrase must be flagged regardless of surrounding context."""
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text(phrase + "\n", encoding="utf-8")
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.errors_detected != [], f"Phrase was not flagged as an error: {phrase}"
    assert result.success is False


# ---------------------------------------------------------------------------
# scan_runtime_logs — baseline offset contract
# ---------------------------------------------------------------------------

def test_baseline_offset_excludes_pre_existing_console_errors(tmp_path: Path) -> None:
    """
    Content written before the baseline offset (an earlier crash) must never
    surface in a later scan, even though the file still contains it physically.
    """
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text(
        "NullReferenceException during a prior crash\n", encoding="utf-8"
    )

    baseline = capture_baseline(tmp_path)

    with console.open("a", encoding="utf-8") as fh:
        fh.write("CounterStrikeSharp... Loaded plugin MyPlugin v1.0\n")

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.success is True
    assert result.errors_detected == [], (
        "Error content written before the baseline offset leaked into the scan"
    )


# ---------------------------------------------------------------------------
# scan_runtime_logs — CSSharp logs directory mtime gate
# ---------------------------------------------------------------------------

def test_cssharp_logs_dir_mtime_gate_filters_files_older_than_baseline(
    tmp_path: Path,
) -> None:
    """
    Log files from a previous run (mtime before the baseline) must be ignored
    entirely; only files touched at/after the baseline are scanned.
    """
    logs_dir = tmp_path.joinpath(*CSSHARP_LOGS_SUBDIR)
    logs_dir.mkdir(parents=True)

    baseline_epoch = 1_700_000_000.0
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=baseline_epoch)

    old_file = logs_dir / "log-old.txt"
    old_file.write_text("NullReferenceException in old session\n", encoding="utf-8")
    old_mtime = baseline_epoch - 100
    os.utime(old_file, (old_mtime, old_mtime))

    new_file = logs_dir / "log-new.txt"
    new_file.write_text(
        "CounterStrikeSharp... Loaded plugin MyPlugin v1.0\n", encoding="utf-8"
    )
    new_mtime = baseline_epoch + 10
    os.utime(new_file, (new_mtime, new_mtime))

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.success is True
    assert result.errors_detected == []
    assert str(old_file) not in result.log_files_scanned, (
        "A log file older than the baseline must not be scanned"
    )
    assert str(new_file) in result.log_files_scanned


# ---------------------------------------------------------------------------
# scan_runtime_logs — inconclusive shape / plugin name matching
# ---------------------------------------------------------------------------

def test_scan_runtime_logs_inconclusive_when_nothing_present(tmp_path: Path) -> None:
    """No console.log and no CSSharp logs dir must degrade to inconclusive, not error."""
    baseline = capture_baseline(tmp_path)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.success is False
    assert result.errors_detected == []
    assert result.log_files_scanned == []


def test_scan_runtime_logs_plugin_name_match_is_case_insensitive(tmp_path: Path) -> None:
    """The success pattern must match the plugin name regardless of letter case."""
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text("loaded plugin myplugin successfully\n", encoding="utf-8")
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.success is True


# ---------------------------------------------------------------------------
# scan_runtime_logs — severity tag scoping
# ---------------------------------------------------------------------------

def test_severity_tag_without_cssharp_token_in_console_log_not_flagged(
    tmp_path: Path,
) -> None:
    """
    A bracketed severity tag on console.log with no CSSharp token must be
    treated as unrelated engine noise, not a plugin error.
    """
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text("12:00:00 [Error] SomeEngineThing failed\n", encoding="utf-8")
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.errors_detected == []


def test_severity_tag_with_cssharp_token_in_console_log_is_flagged(
    tmp_path: Path,
) -> None:
    """The same severity tag, once a CSSharp token appears on the line, must be flagged."""
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text(
        "12:00:00 [Error] CSSharp SomeEngineThing failed\n", encoding="utf-8"
    )
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert len(result.errors_detected) == 1


def test_severity_tag_inside_cssharp_logs_dir_flagged_without_token(
    tmp_path: Path,
) -> None:
    """Inside CSSharp's own log directory, a severity tag needs no extra token."""
    logs_dir = tmp_path.joinpath(*CSSHARP_LOGS_SUBDIR)
    logs_dir.mkdir(parents=True)
    (logs_dir / "log-20260706.txt").write_text(
        "[Error] SomeEngineThing failed\n", encoding="utf-8"
    )
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert len(result.errors_detected) == 1


def test_bare_word_error_without_bracket_not_flagged(tmp_path: Path) -> None:
    """Chat text mentioning 'error' with no severity bracket must not be flagged."""
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text(
        "player says: this map has an error in it\n", encoding="utf-8"
    )
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.errors_detected == []


# ---------------------------------------------------------------------------
# scan_runtime_logs — robustness (truncation, encoding, error cap)
# ---------------------------------------------------------------------------

def test_scan_runtime_logs_clamps_offset_larger_than_file_size(tmp_path: Path) -> None:
    """
    A baseline offset past EOF (console.log truncated/reopened by -condebug)
    must fall back to reading from byte 0 instead of raising.
    """
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text(
        "CounterStrikeSharp... Loaded plugin MyPlugin v1.0\n", encoding="utf-8"
    )
    baseline = LogScanBaseline(console_log_offset=999_999, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.success is True


def test_scan_runtime_logs_handles_invalid_utf8_bytes(tmp_path: Path) -> None:
    """Invalid byte sequences must decode with replacement, never raise."""
    console = tmp_path / CONSOLE_LOG_NAME
    content = (
        b"\xff\xfe garbled bytes\n"
        + "CounterStrikeSharp... Loaded plugin MyPlugin v1.0\n".encode("utf-8")
    )
    console.write_bytes(content)
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.success is True


def test_scan_runtime_logs_caps_error_count_with_summary_line(tmp_path: Path) -> None:
    """
    A flood of error lines must be capped so a broken plugin cannot spam the
    caller — the cap is followed by a single summary line of the overflow.
    """
    logs_dir = tmp_path.joinpath(*CSSHARP_LOGS_SUBDIR)
    logs_dir.mkdir(parents=True)
    lines = "\n".join(f"[Error] distinct failure number {i}" for i in range(30))
    (logs_dir / "log-flood.txt").write_text(lines + "\n", encoding="utf-8")
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert len(result.errors_detected) == _MAX_REPORTED_ERRORS + 1
    assert "more error line" in result.errors_detected[-1]


# ---------------------------------------------------------------------------
# scan_runtime_logs — success pattern co-occurrence
# ---------------------------------------------------------------------------

def test_success_requires_verb_and_name_on_same_line(tmp_path: Path) -> None:
    """A load verb on one line and the plugin name on another must not match."""
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text(
        "MyPlugin is running fine today\n"
        "Loaded plugin OtherPlugin v2.0\n",
        encoding="utf-8",
    )
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    assert result.success is False


# ---------------------------------------------------------------------------
# scan_latest_logs — async wrapper parity
# ---------------------------------------------------------------------------

def test_scan_latest_logs_matches_sync_scan_runtime_logs(tmp_path: Path) -> None:
    """The asyncio.to_thread wrapper must return exactly what the sync scan returns."""
    console = tmp_path / CONSOLE_LOG_NAME
    console.write_text(
        "CounterStrikeSharp... Loaded plugin MyPlugin v1.0\n", encoding="utf-8"
    )
    baseline = LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0)

    direct_result = scan_runtime_logs(tmp_path, "MyPlugin", baseline)

    async def _run() -> LogValidationResult:
        return await scan_latest_logs(tmp_path, "MyPlugin", baseline)

    async_result = asyncio.run(_run())

    assert async_result == direct_result
