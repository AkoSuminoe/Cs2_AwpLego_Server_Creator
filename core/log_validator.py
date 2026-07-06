"""
log_validator.py — Runtime log scanner for post-install plugin verification.

Only content produced after a captured baseline is considered: console.log is
read from a byte offset, CounterStrikeSharp log files are gated by mtime.
Scanning is plain file I/O with compiled regexes — no engine hooks, no polling.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import List

from models.schemas import LogScanBaseline, LogValidationResult

CONSOLE_LOG_NAME = "console.log"
CSSHARP_LOGS_SUBDIR = ("addons", "counterstrikesharp", "logs")

_MAX_REPORTED_ERRORS = 25
_MAX_ERROR_LINE_LENGTH = 300

_SUCCESS_VERBS = r"(?:loaded|loading|registered|initialized)"

# Fatal wherever they appear — ordinary chat or map-name text never contains
# this vocabulary, so no extra scoping is needed.
_GLOBAL_FATAL_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"Exception", re.IGNORECASE),
    re.compile(r"Failed to load plugin", re.IGNORECASE),
    re.compile(r"Unable to load plugin", re.IGNORECASE),
    re.compile(r"Could not load", re.IGNORECASE),
    re.compile(r"Missing dependency", re.IGNORECASE),
]

# Bracketed severity tags ("[Error]", "... ERR]"). Requiring the closing
# bracket keeps a bare "error" inside chat lines or map names from matching.
_LEVEL_TAG_RE = re.compile(r"\b(?:error|err|fatal|crit(?:ical)?)\]", re.IGNORECASE)
_CSSHARP_TOKEN_RE = re.compile(r"CounterStrikeSharp|CSSharp", re.IGNORECASE)


def _success_pattern(plugin_name: str) -> re.Pattern[str]:
    # A load verb and the plugin name on the same line, in either order.
    # CSSharp's exact wording varies between versions, so the match is loose.
    escaped = re.escape(plugin_name)
    return re.compile(rf"(?=.*\b{_SUCCESS_VERBS}\b)(?=.*{escaped})", re.IGNORECASE)


def _line_is_error(line: str, scoped_to_cssharp: bool) -> bool:
    """
    Severity-tag lines are unconditional inside CSSharp's own log directory;
    on console.log (mixed engine/chat content) they must also carry a CSSharp
    token to avoid flagging unrelated engine noise.
    """
    if any(p.search(line) for p in _GLOBAL_FATAL_PATTERNS):
        return True
    if _LEVEL_TAG_RE.search(line):
        return scoped_to_cssharp or bool(_CSSHARP_TOKEN_RE.search(line))
    return False


def capture_baseline(csgo_dir: Path) -> LogScanBaseline:
    """Records the console.log high-water mark just before a server launch."""
    console_path = csgo_dir / CONSOLE_LOG_NAME
    try:
        offset = console_path.stat().st_size
    except OSError:
        offset = 0
    return LogScanBaseline(console_log_offset=offset, captured_at_epoch=time.time())


def _read_console_tail(console_path: Path, offset: int) -> str:
    try:
        size = console_path.stat().st_size
        # -condebug may truncate/reopen the file per launch; a shrunken file
        # means the offset points past EOF, so fall back to the full content.
        start = offset if offset <= size else 0
        with console_path.open("rb") as fh:
            fh.seek(start)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def scan_runtime_logs(
    csgo_dir: Path,
    plugin_name: str,
    baseline: LogScanBaseline,
) -> LogValidationResult:
    """
    Scans console.log (from the baseline offset) and any CSSharp log file
    touched since the baseline. Never raises — unreadable files degrade to
    "nothing scanned", which classifies as inconclusive, not as an error.
    """
    success_re = _success_pattern(plugin_name)
    success_found = False
    errors: List[str] = []
    scanned: List[str] = []

    console_path = csgo_dir / CONSOLE_LOG_NAME
    if console_path.exists():
        scanned.append(str(console_path))
        tail = _read_console_tail(console_path, baseline.console_log_offset)
        for line in tail.splitlines():
            if success_re.search(line):
                success_found = True
            if _line_is_error(line, scoped_to_cssharp=False):
                errors.append(line.strip()[:_MAX_ERROR_LINE_LENGTH])

    logs_dir = csgo_dir.joinpath(*CSSHARP_LOGS_SUBDIR)
    if logs_dir.is_dir():
        for log_file in sorted(logs_dir.glob("*")):
            if not log_file.is_file():
                continue
            try:
                if log_file.stat().st_mtime < baseline.captured_at_epoch:
                    continue
                text = log_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned.append(str(log_file))
            for line in text.splitlines():
                if success_re.search(line):
                    success_found = True
                if _line_is_error(line, scoped_to_cssharp=True):
                    errors.append(line.strip()[:_MAX_ERROR_LINE_LENGTH])

    if len(errors) > _MAX_REPORTED_ERRORS:
        overflow = len(errors) - _MAX_REPORTED_ERRORS
        errors = errors[:_MAX_REPORTED_ERRORS]
        errors.append(f"... and {overflow} more error line(s) suppressed")

    return LogValidationResult(
        success=success_found and not errors,
        plugin_name=plugin_name,
        errors_detected=errors,
        log_files_scanned=scanned,
    )


async def scan_latest_logs(
    csgo_dir: Path,
    plugin_name: str,
    baseline: LogScanBaseline,
) -> LogValidationResult:
    """Non-blocking wrapper — file I/O runs in a worker thread."""
    return await asyncio.to_thread(scan_runtime_logs, csgo_dir, plugin_name, baseline)
