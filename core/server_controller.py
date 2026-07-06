"""
server_controller.py — CS2 process lifecycle for runtime plugin verification.

Launches cs2.exe directly (mirroring start_server.bat plus -condebug), polls
RCON for readiness, and guarantees the process is fully down before any
rollback touches plugin files. Verification failures degrade to inconclusive
results — only a confirmed error signature in the logs justifies a rollback.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal, Optional, Sequence

from core import snapshot
from core.log_validator import capture_baseline, scan_latest_logs
from core.rcon_manager import RCONAuthError, RCONClient, RCONConnectionError
from models.schemas import LogValidationResult, ServerConfig, SnapshotMeta

RCON_HOST = "127.0.0.1"
CS2_EXE_RELATIVE = Path("game") / "bin" / "win64" / "cs2.exe"

VerificationOutcome = Literal["passed", "rolled_back", "inconclusive"]


class ServerControlError(Exception):
    pass


class ServerController:
    def __init__(self, server_dir: Path, config: ServerConfig) -> None:
        self._server_dir = server_dir
        self._config = config
        self._proc: Optional[asyncio.subprocess.Process] = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def _build_launch_args(self) -> list[str]:
        # Mirrors BAT_TEMPLATE in config_patcher, plus -condebug so the engine
        # writes console.log for the post-launch scan.
        cfg = self._config
        return [
            "-dedicated", "-usercon", "-console", "-condebug",
            "+game_type", "3", "+game_mode", "0",
            "+sv_logfile", "1", "-serverlogging",
            "+sv_setsteamaccount", cfg.gslt_token,
            "-authkey", cfg.auth_key,
            "-ip", cfg.server_ip,
            "-port", str(cfg.server_port),
            "+map", cfg.map,
            "+exec", "server.cfg",
            "-rcon_password", cfg.rcon_password,
            "+sv_kick_players_with_cooldown", "0",
            "+sv_cheats", "0",
        ]

    async def start(self) -> None:
        if self.is_running:
            return
        exe_path = self._server_dir / CS2_EXE_RELATIVE
        if not exe_path.exists():
            raise ServerControlError(f"cs2.exe not found at {exe_path}")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                str(exe_path),
                *self._build_launch_args(),
                cwd=str(exe_path.parent),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise ServerControlError(f"Failed to launch cs2.exe: {exc}") from exc

    async def wait_until_ready(
        self,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
    ) -> bool:
        """
        Polls RCON until the server answers or the deadline passes. Returns
        False on timeout or early process death — inconclusive, not an error.
        """
        if self._proc is None:
            raise ServerControlError("start() must be called before wait_until_ready().")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._proc.returncode is not None:
                return False  # process died during boot — no point polling
            client = RCONClient(
                RCON_HOST,
                self._config.server_port,
                self._config.rcon_password,
                timeout=poll_interval,
            )
            try:
                await client.connect()
                await client.close()
                return True
            except (RCONAuthError, RCONConnectionError):
                await asyncio.sleep(poll_interval)
        return False

    async def stop(self, timeout: float = 15.0) -> None:
        """
        Graceful shutdown ladder: RCON quit → wait → terminate → kill.
        Idempotent when the server is not running.
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            self._proc = None
            return

        client = RCONClient(
            RCON_HOST,
            self._config.server_port,
            self._config.rcon_password,
            timeout=5.0,
        )
        try:
            await client.connect()
            try:
                await client.execute("quit")
            finally:
                await client.close()
        except (RCONAuthError, RCONConnectionError):
            pass  # best-effort; process-level shutdown follows regardless

        # Escalation waits inherit the caller's budget so a tiny timeout in
        # tests (or an impatient caller) never blocks on a hardcoded value.
        grace = min(5.0, timeout)
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=grace)
                except asyncio.TimeoutError:
                    pass
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass
        self._proc = None


async def _is_port_already_serving(
    server_port: int,
    rcon_password: str,
    probe_timeout: float = 2.0,
) -> bool:
    """
    True when a live server already answers RCON on the target port — an auth
    failure still proves the port is occupied by a running instance.
    """
    client = RCONClient(RCON_HOST, server_port, rcon_password, timeout=probe_timeout)
    try:
        await client.connect()
    except RCONAuthError:
        return True
    except RCONConnectionError:
        return False
    await client.close()
    return True


async def rollback_with_retry(
    snap: SnapshotMeta,
    csgo_dir: Path,
    delays: Sequence[float] = (0.2, 0.5, 1.0),
) -> None:
    """
    Rollback with a short backoff ladder. Windows can hold DLL file handles
    for a few hundred milliseconds after process exit, which surfaces as
    sharing-violation SnapshotErrors from the rmtree inside snapshot.rollback.
    """
    for delay in (*delays, None):
        try:
            snapshot.rollback(snap, csgo_dir)
            return
        except snapshot.SnapshotError:
            if delay is None:
                raise
            await asyncio.sleep(delay)


async def verify_plugin_runtime(
    server_dir: Path,
    csgo_dir: Path,
    config: ServerConfig,
    plugin_name: str,
    *,
    ready_timeout: float = 120.0,
    poll_interval: float = 3.0,
    load_grace_seconds: float = 5.0,
    stop_timeout: float = 15.0,
    probe_timeout: float = 2.0,
) -> LogValidationResult:
    """
    Full verification cycle: launch the server, wait for RCON, give plugins a
    short load window, shut down, then scan the logs produced in between.

    Returns an inconclusive result (success False, no errors) whenever the
    runtime cannot be observed — an occupied port, a boot timeout, or missing
    logs. Only ServerControlError from launch preconditions escapes.
    """
    if await _is_port_already_serving(config.server_port, config.rcon_password, probe_timeout):
        # A live server already owns the port; a second cs2.exe would collide.
        return LogValidationResult(success=False, plugin_name=plugin_name)

    baseline = capture_baseline(csgo_dir)
    controller = ServerController(server_dir, config)
    await controller.start()
    try:
        ready = await controller.wait_until_ready(
            timeout=ready_timeout, poll_interval=poll_interval
        )
        if ready and load_grace_seconds > 0:
            await asyncio.sleep(load_grace_seconds)
    finally:
        # The process must be fully down before any rollback touches addons/.
        await controller.stop(timeout=stop_timeout)

    return await scan_latest_logs(csgo_dir, plugin_name, baseline)


def classify_verification_outcome(result: LogValidationResult) -> VerificationOutcome:
    """Maps a scan result onto the single install-loop decision."""
    if result.errors_detected:
        return "rolled_back"
    if result.success:
        return "passed"
    return "inconclusive"
