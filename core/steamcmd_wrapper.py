from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import AsyncGenerator

from models.schemas import SteamCMDEvent, SteamCMDPhase

STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
CS2_APP_ID = "730"

# Matches lines like:
#   Update state (0x61) downloading, progress: 47.89 (3232478 / 6750000)
PROGRESS_RE = re.compile(
    r"Update state \(0x(?P<state>\w+)\)\s+\w+,\s+progress:\s+(?P<pct>\d+\.\d+)"
)

_PHASE_MAP: dict[str, SteamCMDPhase] = {
    "61": SteamCMDPhase.DOWNLOADING,
    "81": SteamCMDPhase.COMMITTING,
    "05": SteamCMDPhase.VALIDATING,
}


class SteamCMDInstallError(Exception):
    """Raised when SteamCMD exits with a non-zero return code."""


async def download_steamcmd(steamcmd_dir: Path) -> None:
    """
    Downloads steamcmd.zip from Valve and extracts steamcmd.exe.
    Idempotent: returns immediately if steamcmd.exe already exists.

    Uses run_in_executor so the blocking urllib call doesn't freeze the event loop.
    """
    steamcmd_exe = steamcmd_dir / "steamcmd.exe"
    if steamcmd_exe.exists():
        return

    try:
        steamcmd_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SteamCMDInstallError(
            f"Cannot create SteamCMD directory {steamcmd_dir}: {exc}"
        ) from exc

    zip_path = steamcmd_dir / "steamcmd.zip"

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None, urllib.request.urlretrieve, STEAMCMD_URL, str(zip_path)
        )
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        zip_path.unlink(missing_ok=True)
        raise SteamCMDInstallError(
            f"Failed to download SteamCMD from {STEAMCMD_URL}: {exc}"
        ) from exc

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(steamcmd_dir)
    except (zipfile.BadZipFile, OSError) as exc:
        raise SteamCMDInstallError(
            f"Failed to extract steamcmd.zip: {exc}"
        ) from exc
    finally:
        zip_path.unlink(missing_ok=True)


async def install_cs2(
    steamcmd_exe: Path,
    server_dir: Path,
) -> AsyncGenerator[SteamCMDEvent, None]:
    """
    Async generator that launches SteamCMD and yields SteamCMDEvent for each
    parsed progress line. The event loop stays free between lines.

    Usage:
        async for event in install_cs2(steamcmd_exe, server_dir):
            progress_bar.update(event.percent)
    """
    if not steamcmd_exe.exists():
        raise SteamCMDInstallError(
            f"steamcmd.exe not found at {steamcmd_exe}. Run download_steamcmd() first."
        )

    try:
        server_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SteamCMDInstallError(
            f"Cannot create server directory {server_dir}: {exc}"
        ) from exc

    try:
        proc = await asyncio.create_subprocess_exec(
            str(steamcmd_exe),
            "+force_install_dir", str(server_dir),
            "+login", "anonymous",
            "+app_update", CS2_APP_ID, "validate",
            "+quit",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr so nothing is lost
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise SteamCMDInstallError(
            f"Failed to launch SteamCMD: {exc}"
        ) from exc

    assert proc.stdout is not None  # guaranteed by PIPE

    try:
        async for raw_line in proc.stdout:
            decoded = raw_line.decode("utf-8", errors="replace").rstrip()
            m = PROGRESS_RE.search(decoded)
            if m:
                try:
                    pct = float(m.group("pct"))
                except (ValueError, TypeError):
                    continue
                yield SteamCMDEvent(
                    phase=_classify_phase(m.group("state")),
                    percent=pct,
                    raw_line=decoded,
                )
    except asyncio.CancelledError:
        proc.terminate()
        await proc.wait()
        raise

    await proc.wait()

    if proc.returncode != 0:
        raise SteamCMDInstallError(
            f"SteamCMD exited with code {proc.returncode}. "
            "Check your internet connection or Steam's status and try again."
        )


def _classify_phase(hex_state: str) -> SteamCMDPhase:
    return _PHASE_MAP.get(hex_state.lower(), SteamCMDPhase.UNKNOWN)
