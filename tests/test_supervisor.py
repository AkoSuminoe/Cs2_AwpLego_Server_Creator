"""Offline tests for the 24/7 supervisor: schedule math, daily cycle, crashes, requests."""
from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from core import supervisor as sup
from core.supervisor import (
    ServerSupervisor,
    SupervisorError,
    next_run,
    parse_hhmm,
    serve_script,
)
from models.schemas import ServerConfig


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("now, at, expected", [
    ("2026-10-15 13:00", "00:00", "2026-10-16 00:00"),   # later today has passed → tomorrow
    ("2026-10-15 23:59", "00:00", "2026-10-16 00:00"),   # one minute to midnight
    ("2026-10-15 00:00", "00:00", "2026-10-16 00:00"),   # exactly at → next day
    ("2026-10-15 04:00", "05:30", "2026-10-15 05:30"),   # still ahead today
    ("2026-12-31 22:00", "03:00", "2027-01-01 03:00"),   # year rollover
])
def test_next_run(now: str, at: str, expected: str) -> None:
    fmt = "%Y-%m-%d %H:%M"
    assert next_run(dt.datetime.strptime(now, fmt), parse_hhmm(at)) == dt.datetime.strptime(expected, fmt)


@pytest.mark.parametrize("bad", ["24:00", "7", "ab:cd", "12:60", ""])
def test_parse_hhmm_rejects_bad_values(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_hhmm(bad)


def test_serve_script_quotes_paths_with_spaces() -> None:
    script = serve_script(
        r"C:\Program Files\Python\python.exe",
        Path(r"C:\Users\Me\My Tools\main.py"),
        Path(r"D:\CS2 Servers\awp"),
        dt.time(4, 30),
    )
    assert r'"C:\Program Files\Python\python.exe" "C:\Users\Me\My Tools\main.py"' in script
    assert r'--serve --dir "D:\CS2 Servers\awp" --restart-at 04:30' in script


def test_read_build_id(tmp_path: Path) -> None:
    (tmp_path / "steamapps").mkdir()
    (tmp_path / "steamapps" / "appmanifest_730.acf").write_text(
        '"AppState"\n{\n\t"buildid"\t\t"25000182"\n}\n', encoding="utf-8"
    )
    assert sup.read_build_id(tmp_path) == "25000182"
    assert sup.read_build_id(tmp_path / "missing") == "?"


def test_supervisor_running_ignores_stale_state(tmp_path: Path) -> None:
    assert sup.supervisor_running(tmp_path) is False
    (tmp_path / sup.STATE_FILE).write_text('{"pid": 999999999}', encoding="utf-8")
    assert sup.supervisor_running(tmp_path) is False


# ---------------------------------------------------------------------------
# Loop harness: fake clock, controller, updater, announcer
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self, start: dt.datetime) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now


class _FakeController:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.running = False

    @property
    def is_running(self) -> bool:
        return self.running

    async def start(self) -> None:
        self.running = True
        self.events.append("start")

    async def stop(self, timeout: float) -> None:
        if self.running:
            self.events.append("stop")
        self.running = False


def _make(tmp_path: Path, *, start: dt.datetime, max_ticks: int, on_tick=None):
    (tmp_path / "cs2_server" / "game" / "csgo").mkdir(parents=True)
    (tmp_path / "cs2_server" / "game" / "csgo" / "gameinfo.gi").write_text(
        "Game_LowViolence\tcsgo_lv\n", encoding="utf-8"
    )
    events: list[str] = []
    clock = _Clock(start)
    stop = asyncio.Event()
    controllers: list[_FakeController] = []
    ticks = {"n": 0}

    async def fake_sleep(seconds: float) -> None:
        clock.now += dt.timedelta(seconds=seconds)
        if seconds == 2.0:  # poll tick
            ticks["n"] += 1
            if on_tick:
                on_tick(ticks["n"], controllers, clock)
            if ticks["n"] >= max_ticks:
                stop.set()

    def factory() -> _FakeController:
        c = _FakeController(events)
        controllers.append(c)
        return c

    async def updater(steamcmd: Path, server_dir: Path):
        events.append("update")
        if False:
            yield None

    async def announce(msg: str) -> None:
        events.append(f"say:{msg}")

    cfg = ServerConfig(gslt_token="", auth_key="", server_ip="0.0.0.0", rcon_password="pw")
    s = ServerSupervisor(
        tmp_path, cfg, dt.time(0, 0), warn_seconds=60, poll_interval=2.0,
        clock=clock, sleep=fake_sleep, controller_factory=factory,
        updater=updater, announcer=announce, log=lambda line: None,
    )
    return s, events, stop, controllers, clock


def test_daily_cycle_warns_stops_updates_patches_and_starts(tmp_path: Path) -> None:
    start = dt.datetime(2026, 10, 15, 23, 59, 50)
    s, events, stop, _, _ = _make(tmp_path, start=start, max_ticks=8)
    asyncio.run(s.run(stop))

    assert events[:7] == [
        "start",
        "say:[Server] Daily restart & update in 60 seconds.",
        "say:[Server] Daily restart & update in 10 seconds.",
        "stop", "update", "start", "stop",  # final stop = shutdown at loop end
    ]
    gameinfo = (tmp_path / "cs2_server/game/csgo/gameinfo.gi").read_text(encoding="utf-8")
    assert "csgo/addons/metamod" in gameinfo, "update must re-apply the Metamod hook"
    assert s.next_restart == dt.datetime(2026, 10, 17, 0, 0)
    assert not (tmp_path / sup.STATE_FILE).exists(), "state file removed on exit"


def test_crash_restarts_with_backoff(tmp_path: Path) -> None:
    def crash_once(tick: int, controllers, clock) -> None:
        if tick == 2:
            controllers[-1].running = False

    s, events, stop, controllers, _ = _make(
        tmp_path, start=dt.datetime(2026, 10, 15, 12, 0), max_ticks=5, on_tick=crash_once
    )
    asyncio.run(s.run(stop))
    assert events.count("start") == 2
    assert len(controllers) == 2


def test_gives_up_after_repeated_crashes(tmp_path: Path) -> None:
    def always_crash(tick: int, controllers, clock) -> None:
        controllers[-1].running = False

    s, _, stop, _, _ = _make(
        tmp_path, start=dt.datetime(2026, 10, 15, 12, 0), max_ticks=50, on_tick=always_crash
    )
    with pytest.raises(SupervisorError, match="giving up"):
        asyncio.run(s.run(stop))


def test_restart_request_file_triggers_restart_without_update(tmp_path: Path) -> None:
    def drop_request(tick: int, controllers, clock) -> None:
        if tick == 1:
            sup.request_restart(tmp_path, "plugins changed")

    s, events, stop, _, _ = _make(
        tmp_path, start=dt.datetime(2026, 10, 15, 12, 0), max_ticks=4, on_tick=drop_request
    )
    asyncio.run(s.run(stop))
    assert "update" not in events
    assert events[:4] == [
        "start",
        "say:[Server] Restarting in 60 seconds.",
        "say:[Server] Restarting in 10 seconds.",
        "stop",
    ]
    assert events.count("start") == 2
    assert not (tmp_path / sup.REQUEST_FILE).exists()
    log = (tmp_path / sup.LOG_FILE).read_text(encoding="utf-8")
    assert "Restarting: plugins changed" in log


def test_update_failure_still_restarts_server(tmp_path: Path) -> None:
    s, events, stop, _, _ = _make(
        tmp_path, start=dt.datetime(2026, 10, 15, 23, 59, 50), max_ticks=8
    )

    async def broken_updater(steamcmd: Path, server_dir: Path):
        raise RuntimeError("steam down")
        yield None

    s._updater = broken_updater
    asyncio.run(s.run(stop))
    assert events.count("start") == 2
    assert "Update failed" in (tmp_path / sup.LOG_FILE).read_text(encoding="utf-8")


def test_stop_file_shuts_down_cleanly(tmp_path: Path) -> None:
    def drop_stop(tick: int, controllers, clock) -> None:
        if tick == 1:
            sup.request_stop(tmp_path)

    s, events, stop, _, _ = _make(
        tmp_path, start=dt.datetime(2026, 10, 15, 12, 0), max_ticks=50, on_tick=drop_stop
    )
    asyncio.run(s.run(stop))
    assert events == ["start", "stop"]
    assert not (tmp_path / sup.STOP_FILE).exists()
