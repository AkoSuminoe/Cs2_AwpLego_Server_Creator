"""Offline tests for the CS2 process lifecycle and runtime verification cycle."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import pytest

from core import server_controller
from core.rcon_manager import RCONAuthError, RCONConnectionError
from core.server_controller import (
    ServerControlError,
    ServerController,
    _is_port_already_serving,
    classify_verification_outcome,
    rollback_with_retry,
    verify_plugin_runtime,
)
from core.snapshot import SnapshotError
from models.schemas import LogScanBaseline, LogValidationResult, ServerConfig, SnapshotMeta


def _config(**overrides: Any) -> ServerConfig:
    """Minimal valid ServerConfig for controller tests."""
    values: dict[str, Any] = {
        "gslt_token": "tok",
        "auth_key": "key",
        "server_ip": "0.0.0.0",
        "rcon_password": "pw",
        "server_port": 27015,
    }
    values.update(overrides)
    return ServerConfig(**values)


def _make_exe(server_dir: Path) -> Path:
    """Creates a placeholder cs2.exe so start() passes its existence check."""
    exe = server_dir / "game" / "bin" / "win64" / "cs2.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    return exe


class _FakeProcess:
    """asyncio.subprocess.Process stand-in with a controllable shutdown path."""

    def __init__(self, returncode: Optional[int] = None, hang: bool = False) -> None:
        self.returncode = returncode
        self.terminate_called = False
        self.kill_called = False
        self.call_order: list[str] = []
        self._hang = hang

    def terminate(self) -> None:
        self.terminate_called = True
        self.call_order.append("terminate")
        # A hanging process ignores terminate — escalation must reach kill().

    def kill(self) -> None:
        self.kill_called = True
        self.call_order.append("kill")
        self.returncode = -9
        self._hang = False

    async def wait(self) -> int:
        while self.returncode is None:
            if not self._hang:
                self.returncode = 0
                break
            await asyncio.sleep(0.001)
        return self.returncode


class _RCONScript:
    """
    RCONClient replacement. Each instantiation consumes the next scripted
    behavior ("ok" | "refuse" | "auth_fail"); when the script is exhausted the
    default applies. Executed command bodies are recorded across instances.
    """

    def __init__(self, behaviors: Optional[list[str]] = None, default: str = "ok") -> None:
        self._behaviors = list(behaviors or [])
        self._default = default
        self.instantiations = 0
        self.executed: list[str] = []

    def __call__(self, host: str, port: int, password: str, timeout: float = 5.0) -> Any:
        self.instantiations += 1
        behavior = self._behaviors.pop(0) if self._behaviors else self._default
        return _ScriptedRCONClient(behavior, self.executed)


class _ScriptedRCONClient:
    def __init__(self, behavior: str, executed: list[str]) -> None:
        self._behavior = behavior
        self._executed = executed

    async def connect(self) -> None:
        if self._behavior == "refuse":
            raise RCONConnectionError("connection refused")
        if self._behavior == "auth_fail":
            raise RCONAuthError("bad password")

    async def execute(self, command: str) -> str:
        self._executed.append(command)
        return ""

    async def close(self) -> None:
        return None


def _install_fake_exec(
    monkeypatch: pytest.MonkeyPatch,
    proc: _FakeProcess,
    calls: list[tuple[tuple, dict]],
    raises: Optional[BaseException] = None,
) -> None:
    """Patches asyncio.create_subprocess_exec with a recording spy."""
    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        calls.append((args, kwargs))
        if raises is not None:
            raise raises
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)


# ---------------------------------------------------------------------------
# ServerController.start — launch preconditions and argument wiring
# ---------------------------------------------------------------------------

def test_start_raises_when_exe_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launching without an installed cs2.exe must fail before any subprocess call."""
    calls: list[tuple[tuple, dict]] = []
    _install_fake_exec(monkeypatch, _FakeProcess(), calls)

    async def _run() -> None:
        controller = ServerController(tmp_path, _config())
        await controller.start()

    with pytest.raises(ServerControlError):
        asyncio.run(_run())
    assert calls == [], "Subprocess was launched despite the missing executable"


def test_start_launches_subprocess_with_expected_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The direct launch must mirror start_server.bat (including -condebug for
    console.log capture), run from the win64 directory, and silence stdio.
    """
    exe = _make_exe(tmp_path)
    calls: list[tuple[tuple, dict]] = []
    _install_fake_exec(monkeypatch, _FakeProcess(), calls)

    async def _run() -> None:
        controller = ServerController(tmp_path, _config(server_port=27020))
        await controller.start()

    asyncio.run(_run())

    (args, kwargs) = calls[0]
    assert args[0] == str(exe)
    assert "-condebug" in args
    assert "-port" in args and "27020" in args
    assert kwargs["cwd"] == str(exe.parent)
    assert kwargs["stdout"] is asyncio.subprocess.DEVNULL
    assert kwargs["stderr"] is asyncio.subprocess.DEVNULL


def test_start_wraps_launch_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OS-level launch failure must surface as ServerControlError."""
    _make_exe(tmp_path)
    calls: list[tuple[tuple, dict]] = []
    _install_fake_exec(monkeypatch, _FakeProcess(), calls, raises=PermissionError("denied"))

    async def _run() -> None:
        controller = ServerController(tmp_path, _config())
        await controller.start()

    with pytest.raises(ServerControlError):
        asyncio.run(_run())


def test_start_is_idempotent_while_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second start() on a live process must be a no-op, not a double launch."""
    _make_exe(tmp_path)
    calls: list[tuple[tuple, dict]] = []
    _install_fake_exec(monkeypatch, _FakeProcess(returncode=None), calls)

    async def _run() -> None:
        controller = ServerController(tmp_path, _config())
        await controller.start()
        await controller.start()

    asyncio.run(_run())

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# ServerController.wait_until_ready — RCON readiness polling
# ---------------------------------------------------------------------------

def test_wait_until_ready_true_when_rcon_connects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An immediately answering RCON port must report readiness."""
    _make_exe(tmp_path)
    _install_fake_exec(monkeypatch, _FakeProcess(returncode=None), [])
    monkeypatch.setattr(server_controller, "RCONClient", _RCONScript(default="ok"))

    async def _run() -> bool:
        controller = ServerController(tmp_path, _config())
        await controller.start()
        return await controller.wait_until_ready(timeout=0.05, poll_interval=0.01)

    assert asyncio.run(_run()) is True


def test_wait_until_ready_false_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port that never answers must produce False (inconclusive), not raise."""
    _make_exe(tmp_path)
    _install_fake_exec(monkeypatch, _FakeProcess(returncode=None), [])
    monkeypatch.setattr(server_controller, "RCONClient", _RCONScript(default="refuse"))

    async def _run() -> bool:
        controller = ServerController(tmp_path, _config())
        await controller.start()
        return await controller.wait_until_ready(timeout=0.05, poll_interval=0.01)

    assert asyncio.run(_run()) is False


def test_wait_until_ready_false_fast_on_early_process_death(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that died during boot must short-circuit without any RCON attempt."""
    _make_exe(tmp_path)
    _install_fake_exec(monkeypatch, _FakeProcess(returncode=1), [])
    script = _RCONScript(default="ok")
    monkeypatch.setattr(server_controller, "RCONClient", script)

    async def _run() -> bool:
        controller = ServerController(tmp_path, _config())
        await controller.start()
        return await controller.wait_until_ready(timeout=0.05, poll_interval=0.01)

    assert asyncio.run(_run()) is False
    assert script.instantiations == 0, "RCON was polled for a dead process"


def test_wait_until_ready_raises_before_start(tmp_path: Path) -> None:
    """Calling wait_until_ready() without start() is a programming error."""
    async def _run() -> None:
        controller = ServerController(tmp_path, _config())
        await controller.wait_until_ready(timeout=0.05)

    with pytest.raises(ServerControlError):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# ServerController.stop — graceful shutdown ladder
# ---------------------------------------------------------------------------

def test_stop_is_noop_when_never_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop() on a never-started controller must be silent and RCON-free."""
    script = _RCONScript(default="ok")
    monkeypatch.setattr(server_controller, "RCONClient", script)

    async def _run() -> None:
        controller = ServerController(tmp_path, _config())
        await controller.stop(timeout=0.05)

    asyncio.run(_run())

    assert script.instantiations == 0


def test_stop_sends_rcon_quit_then_waits_gracefully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A responsive server must be shut down via RCON quit — never terminated."""
    _make_exe(tmp_path)
    proc = _FakeProcess(returncode=None)
    _install_fake_exec(monkeypatch, proc, [])
    script = _RCONScript(default="ok")
    monkeypatch.setattr(server_controller, "RCONClient", script)

    async def _run() -> None:
        controller = ServerController(tmp_path, _config())
        await controller.start()
        await controller.stop(timeout=0.05)

    asyncio.run(_run())

    assert "quit" in script.executed
    assert proc.terminate_called is False
    assert proc.kill_called is False


def test_stop_escalates_terminate_then_kill_on_hang(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung process must walk the full ladder: RCON quit → terminate → kill."""
    _make_exe(tmp_path)
    proc = _FakeProcess(returncode=None, hang=True)
    _install_fake_exec(monkeypatch, proc, [])
    monkeypatch.setattr(server_controller, "RCONClient", _RCONScript(default="refuse"))

    async def _run() -> None:
        controller = ServerController(tmp_path, _config())
        await controller.start()
        await controller.stop(timeout=0.05)

    asyncio.run(_run())

    assert proc.call_order == ["terminate", "kill"]
    assert proc.returncode == -9


def test_stop_swallows_rcon_errors_and_still_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable RCON endpoint must not abort the process-level shutdown."""
    _make_exe(tmp_path)
    proc = _FakeProcess(returncode=None)
    _install_fake_exec(monkeypatch, proc, [])
    monkeypatch.setattr(server_controller, "RCONClient", _RCONScript(default="refuse"))

    async def _run() -> None:
        controller = ServerController(tmp_path, _config())
        await controller.start()
        await controller.stop(timeout=0.05)

    asyncio.run(_run())

    assert proc.returncode == 0, "Process was not reaped after RCON failure"


# ---------------------------------------------------------------------------
# _is_port_already_serving — pre-launch collision probe
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("behavior", "expected"),
    [
        ("ok", True),
        ("auth_fail", True),
        ("refuse", False),
    ],
    ids=["connect_ok", "auth_error_still_occupied", "connection_refused"],
)
def test_is_port_already_serving_classification(
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
    expected: bool,
) -> None:
    """Any live RCON responder — even with a wrong password — occupies the port."""
    monkeypatch.setattr(server_controller, "RCONClient", _RCONScript(default=behavior))

    async def _run() -> bool:
        return await _is_port_already_serving(27015, "pw", probe_timeout=0.01)

    assert asyncio.run(_run()) is expected


# ---------------------------------------------------------------------------
# rollback_with_retry — Windows file-handle backoff ladder
# ---------------------------------------------------------------------------

def _snapshot_meta() -> SnapshotMeta:
    return SnapshotMeta(
        snapshot_id="20260706T000000_before_test",
        label="before_test",
        created_at="2026-07-06T00:00:00+00:00",
        archive_path="snap.zip",
        dirs_captured=["addons"],
    )


def test_rollback_with_retry_succeeds_first_attempt_without_sleeping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean first rollback must not pay any backoff delay."""
    attempts: list[int] = []
    sleeps: list[float] = []

    def _fake_rollback(meta: SnapshotMeta, csgo_dir: Path) -> None:
        attempts.append(1)

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(server_controller.snapshot, "rollback", _fake_rollback)
    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    asyncio.run(rollback_with_retry(_snapshot_meta(), tmp_path))

    assert len(attempts) == 1
    assert sleeps == []


def test_rollback_with_retry_recovers_after_transient_lock_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Two sharing-violation failures followed by success must consume exactly
    the first two backoff delays — the Windows handle-lag scenario.
    """
    attempts: list[int] = []
    sleeps: list[float] = []

    def _fake_rollback(meta: SnapshotMeta, csgo_dir: Path) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise SnapshotError("The process cannot access the file (WinError 32)")

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(server_controller.snapshot, "rollback", _fake_rollback)
    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    asyncio.run(rollback_with_retry(_snapshot_meta(), tmp_path, delays=(0.2, 0.5, 1.0)))

    assert len(attempts) == 3
    assert sleeps == [0.2, 0.5]


def test_rollback_with_retry_reraises_after_exhausting_delays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent failure must attempt len(delays)+1 times, then re-raise."""
    attempts: list[int] = []

    def _fake_rollback(meta: SnapshotMeta, csgo_dir: Path) -> None:
        attempts.append(1)
        raise SnapshotError("still locked")

    monkeypatch.setattr(server_controller.snapshot, "rollback", _fake_rollback)

    async def _run() -> None:
        await rollback_with_retry(_snapshot_meta(), tmp_path, delays=(0.0, 0.0, 0.0))

    with pytest.raises(SnapshotError):
        asyncio.run(_run())
    assert len(attempts) == 4


# ---------------------------------------------------------------------------
# classify_verification_outcome — pure decision table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("success", "errors", "expected"),
    [
        (False, ["Exception in plugin"], "rolled_back"),
        (True, ["Exception in plugin"], "rolled_back"),
        (True, [], "passed"),
        (False, [], "inconclusive"),
    ],
    ids=["errors_fail", "errors_override_success", "clean_pass", "no_evidence"],
)
def test_classify_verification_outcome_decision_table(
    success: bool,
    errors: list[str],
    expected: str,
) -> None:
    """Error evidence must dominate; success without errors passes; neither is inconclusive."""
    result = LogValidationResult(
        success=success, plugin_name="MyPlugin", errors_detected=errors
    )
    assert classify_verification_outcome(result) == expected


# ---------------------------------------------------------------------------
# verify_plugin_runtime — full cycle wiring
# ---------------------------------------------------------------------------

def _pin_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the baseline to byte 0 / epoch 0 so pre-written logs are in scope."""
    monkeypatch.setattr(
        server_controller,
        "capture_baseline",
        lambda _csgo_dir: LogScanBaseline(console_log_offset=0, captured_at_epoch=0.0),
    )


def test_verify_plugin_runtime_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Probe refused (port free) → launch → RCON ready → stop → scan finds the
    clean load line → success.
    """
    _make_exe(tmp_path)
    (tmp_path / "console.log").write_text(
        "CounterStrikeSharp... Loaded plugin MyPlugin v1.0\n", encoding="utf-8"
    )
    _install_fake_exec(monkeypatch, _FakeProcess(returncode=None), [])
    monkeypatch.setattr(server_controller, "RCONClient", _RCONScript(["refuse"], default="ok"))
    _pin_baseline(monkeypatch)

    async def _run() -> LogValidationResult:
        return await verify_plugin_runtime(
            tmp_path, tmp_path, _config(), "MyPlugin",
            ready_timeout=0.05, poll_interval=0.01,
            load_grace_seconds=0.0, stop_timeout=0.05, probe_timeout=0.01,
        )

    result = asyncio.run(_run())

    assert result.success is True
    assert classify_verification_outcome(result) == "passed"


def test_verify_plugin_runtime_detects_errors_for_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fatal load failure in the fresh log content must classify as rolled_back."""
    _make_exe(tmp_path)
    (tmp_path / "console.log").write_text(
        "Failed to load plugin MyPlugin\n", encoding="utf-8"
    )
    _install_fake_exec(monkeypatch, _FakeProcess(returncode=None), [])
    monkeypatch.setattr(server_controller, "RCONClient", _RCONScript(["refuse"], default="ok"))
    _pin_baseline(monkeypatch)

    async def _run() -> LogValidationResult:
        return await verify_plugin_runtime(
            tmp_path, tmp_path, _config(), "MyPlugin",
            ready_timeout=0.05, poll_interval=0.01,
            load_grace_seconds=0.0, stop_timeout=0.05, probe_timeout=0.01,
        )

    result = asyncio.run(_run())

    assert result.errors_detected != []
    assert classify_verification_outcome(result) == "rolled_back"


def test_verify_plugin_runtime_skips_launch_when_port_occupied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live server on the target port must veto the launch entirely."""
    _make_exe(tmp_path)
    calls: list[tuple[tuple, dict]] = []
    _install_fake_exec(monkeypatch, _FakeProcess(), calls)
    monkeypatch.setattr(server_controller, "RCONClient", _RCONScript(default="ok"))

    async def _run() -> LogValidationResult:
        return await verify_plugin_runtime(
            tmp_path, tmp_path, _config(), "MyPlugin",
            ready_timeout=0.05, poll_interval=0.01,
            load_grace_seconds=0.0, stop_timeout=0.05, probe_timeout=0.01,
        )

    result = asyncio.run(_run())

    assert calls == [], "cs2.exe was launched despite an occupied port"
    assert result.success is False
    assert result.errors_detected == []
    assert classify_verification_outcome(result) == "inconclusive"


def test_verify_plugin_runtime_propagates_launch_precondition_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the port free but no cs2.exe on disk, the precondition error escapes."""
    monkeypatch.setattr(server_controller, "RCONClient", _RCONScript(default="refuse"))

    async def _run() -> None:
        await verify_plugin_runtime(
            tmp_path, tmp_path, _config(), "MyPlugin",
            ready_timeout=0.05, poll_interval=0.01,
            load_grace_seconds=0.0, stop_timeout=0.05, probe_timeout=0.01,
        )

    with pytest.raises(ServerControlError):
        asyncio.run(_run())
