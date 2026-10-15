"""
supervisor.py — keeps a CS2 server running 24/7.

  * starts cs2.exe in its own console window and restarts it after a crash
    (with backoff, giving up after repeated crashes in a short window);
  * once a day at a chosen time: warns players over RCON, stops the server,
    runs SteamCMD `app_update 730 validate`, re-applies the gameinfo.gi
    Metamod hook that updates wipe, and starts the server again;
  * restarts on request: other commands (e.g. --add-plugin) drop a
    `.restart-request` file next to the install.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
from collections import deque
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional

from core import config_patcher, steamcmd_wrapper
from core.rcon_manager import RCONAuthError, RCONClient, RCONConnectionError
from core.server_controller import ServerController, rcon_host_for
from core.validator import is_gameinfo_patched
from models.schemas import ServerConfig

REQUEST_FILE = ".restart-request"
STOP_FILE = ".stop-request"
STATE_FILE = ".supervisor.json"
LOG_FILE = "supervisor.log"

CRASH_BACKOFF = (5.0, 15.0, 60.0)
CRASH_LIMIT = 5
CRASH_WINDOW = dt.timedelta(minutes=10)


class SupervisorError(Exception):
    pass


def parse_hhmm(value: str) -> dt.time:
    """'00:00'–'23:59' → time; raises ValueError otherwise."""
    try:
        hour, minute = value.strip().split(":")
        return dt.time(int(hour), int(minute))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Expected HH:MM (24h), got {value!r}") from exc


def next_run(now: dt.datetime, at: dt.time) -> dt.datetime:
    """Next occurrence of `at` strictly after `now`."""
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    return candidate


def read_build_id(server_dir: Path) -> str:
    manifest = server_dir / "steamapps" / "appmanifest_730.acf"
    try:
        for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.replace('"', " ").split()
            if len(parts) == 2 and parts[0] == "buildid":
                return parts[1]
    except OSError:
        pass
    return "?"


def pid_alive(pid: int) -> bool:
    """Process liveness without side effects (os.kill(pid, 0) kills on Windows)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def supervisor_running(base_dir: Path) -> bool:
    try:
        data = json.loads((base_dir / STATE_FILE).read_text(encoding="utf-8"))
        return pid_alive(int(data.get("pid", 0)))
    except (OSError, ValueError, TypeError):
        return False


def request_restart(base_dir: Path, reason: str) -> None:
    (base_dir / REQUEST_FILE).write_text(reason, encoding="utf-8")


def request_stop(base_dir: Path) -> None:
    """Asks a running supervisor to stop the server and exit."""
    (base_dir / STOP_FILE).write_text("stop", encoding="utf-8")


Updater = Callable[[Path, Path], AsyncIterator]


class ServerSupervisor:
    def __init__(
        self,
        base_dir: Path,
        config: ServerConfig,
        restart_at: dt.time,
        *,
        warn_seconds: int = 60,
        poll_interval: float = 2.0,
        clock: Callable[[], dt.datetime] = dt.datetime.now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        controller_factory: Optional[Callable[[], ServerController]] = None,
        updater: Optional[Updater] = None,
        announcer: Optional[Callable[[str], Awaitable[None]]] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.base_dir = base_dir
        self.server_dir = base_dir / "cs2_server"
        self.csgo_dir = self.server_dir / "game" / "csgo"
        self.config = config
        self.restart_at = restart_at
        self.warn_seconds = warn_seconds
        self.poll_interval = poll_interval
        self._clock = clock
        self._sleep = sleep
        self._controller_factory = controller_factory or (
            lambda: ServerController(self.server_dir, config, visible_console=True)
        )
        self._updater = updater or steamcmd_wrapper.install_cs2
        self._announce = announcer or self._rcon_say
        self._log_sink = log
        self._controller: Optional[ServerController] = None
        self._crashes: deque[dt.datetime] = deque()
        self.next_restart: Optional[dt.datetime] = None

    # -- logging -----------------------------------------------------------

    def log(self, message: str) -> None:
        line = f"[{self._clock():%Y-%m-%d %H:%M:%S}] {message}"
        if self._log_sink:
            self._log_sink(line)
        try:
            with (self.base_dir / LOG_FILE).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    # -- server control ----------------------------------------------------

    async def _start(self) -> None:
        self._controller = self._controller_factory()
        await self._controller.start()
        self.log(f"Server started (build {read_build_id(self.server_dir)})")

    async def _stop(self) -> None:
        if self._controller is not None:
            await self._controller.stop(timeout=30.0)
            self.log("Server stopped")

    async def _rcon_say(self, message: str) -> None:
        if not self.config.rcon_password:
            return
        try:
            async with RCONClient(
                rcon_host_for(self.config.server_ip), self.config.server_port,
                self.config.rcon_password, timeout=5.0,
            ) as rcon:
                await rcon.execute(f"say {message}")
        except (RCONAuthError, RCONConnectionError):
            pass  # nobody to warn if RCON is down

    async def _warn(self, why: str) -> None:
        if self.warn_seconds <= 0:
            return
        await self._announce(f"[Server] {why} in {self.warn_seconds} seconds.")
        if self.warn_seconds > 10:
            await self._sleep(self.warn_seconds - 10)
            await self._announce(f"[Server] {why} in 10 seconds.")
            await self._sleep(10)
        else:
            await self._sleep(self.warn_seconds)

    async def restart(self, reason: str) -> None:
        self.log(f"Restarting: {reason}")
        await self._warn("Restarting")
        await self._stop()
        await self._start()

    async def daily_cycle(self) -> None:
        before = read_build_id(self.server_dir)
        self.log(f"Daily restart + update check (build {before})")
        await self._warn("Daily restart & update")
        await self._stop()
        steamcmd = self.base_dir / "steamcmd" / "steamcmd.exe"
        try:
            async for _event in self._updater(steamcmd, self.server_dir):
                pass
        except Exception as exc:  # never leave the server down over an update error
            self.log(f"Update failed, starting current build: {exc}")
        after = read_build_id(self.server_dir)
        if after != before:
            self.log(f"CS2 updated: build {before} -> {after}")
        else:
            self.log("No CS2 update")
        if not is_gameinfo_patched(self.csgo_dir):
            config_patcher.patch_gameinfo(self.csgo_dir)
            self.log("Re-applied gameinfo.gi Metamod patch")
        await self._start()

    def _record_crash(self) -> float:
        now = self._clock()
        self._crashes.append(now)
        while self._crashes and now - self._crashes[0] > CRASH_WINDOW:
            self._crashes.popleft()
        if len(self._crashes) >= CRASH_LIMIT:
            raise SupervisorError(
                f"Server crashed {len(self._crashes)} times within "
                f"{int(CRASH_WINDOW.total_seconds() // 60)} minutes — giving up."
            )
        return CRASH_BACKOFF[min(len(self._crashes) - 1, len(CRASH_BACKOFF) - 1)]

    # -- main loop ---------------------------------------------------------

    def _write_state(self) -> None:
        data = {
            "pid": os.getpid(),
            "started_at": self._clock().isoformat(timespec="seconds"),
            "next_restart": self.next_restart.isoformat(timespec="minutes") if self.next_restart else None,
        }
        try:
            (self.base_dir / STATE_FILE).write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    async def run(self, stop: Optional[asyncio.Event] = None) -> None:
        stop = stop or asyncio.Event()
        request = self.base_dir / REQUEST_FILE
        stop_file = self.base_dir / STOP_FILE
        request.unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)
        self.next_restart = next_run(self._clock(), self.restart_at)
        self._write_state()
        self.log(f"Supervisor up — daily restart + update at {self.restart_at:%H:%M} "
                 f"(next: {self.next_restart:%Y-%m-%d %H:%M})")
        try:
            await self._start()
            while not stop.is_set():
                if stop_file.exists():
                    stop_file.unlink(missing_ok=True)
                    self.log("Stop requested")
                    break
                if self._controller is not None and not self._controller.is_running:
                    delay = self._record_crash()
                    self.log(f"Server exited unexpectedly — restarting in {delay:.0f}s")
                    await self._sleep(delay)
                    await self._start()
                elif request.exists():
                    reason = request.read_text(encoding="utf-8", errors="replace").strip()
                    request.unlink(missing_ok=True)
                    await self.restart(reason or "restart requested")
                elif self._clock() >= self.next_restart:
                    await self.daily_cycle()
                    self.next_restart = next_run(self._clock(), self.restart_at)
                    self._write_state()
                    self.log(f"Next daily restart: {self.next_restart:%Y-%m-%d %H:%M}")
                await self._sleep(self.poll_interval)
        finally:
            await self._stop()
            (self.base_dir / STATE_FILE).unlink(missing_ok=True)
            self.log("Supervisor stopped")


# ---------------------------------------------------------------------------
# Start with Windows
# ---------------------------------------------------------------------------

def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def serve_script(python: str, main_py: Path, base_dir: Path, restart_at: dt.time) -> str:
    return (
        "@echo off\r\n"
        f'title CS2 Server ({base_dir.name})\r\n'
        f'cd /d "{main_py.parent}"\r\n'
        f'"{python}" "{main_py}" --serve --dir "{base_dir}" --restart-at {restart_at:%H:%M}\r\n'
        "pause\r\n"
    )


def startup_entry(base_dir: Path) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in base_dir.name)
    return startup_dir() / f"CS2 Server ({safe}).bat"


def install_autostart(python: str, main_py: Path, base_dir: Path, restart_at: dt.time) -> Path:
    """
    Writes <base>/serve.bat and drops a copy in the per-user Startup folder,
    so the supervisor starts at logon. No admin rights needed.
    """
    script = serve_script(python, main_py, base_dir, restart_at)
    (base_dir / "serve.bat").write_text(script, encoding="utf-8")
    entry = startup_entry(base_dir)
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(script, encoding="utf-8")
    return entry


def uninstall_autostart(base_dir: Path) -> bool:
    entry = startup_entry(base_dir)
    if entry.exists():
        entry.unlink()
        return True
    return False
