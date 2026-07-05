"""Tests for the atomic snapshot / self-healing rollback engine."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.snapshot import (
    SnapshotError,
    cleanup_old_snapshots,
    list_snapshots,
    rollback,
    take_snapshot,
)
from models.schemas import SnapshotMeta


ORIGINAL_CONTENT = b"original plugin bytes\x00\x01\x02"
CORRUPTED_CONTENT = b"CORRUPTED_OVERWRITE_FROM_BROKEN_INSTALL"


@pytest.fixture()
def csgo_dir(tmp_path: Path) -> Path:
    """
    Builds an isolated `game/csgo/` directory tree containing a plugin
    DLL, mimicking the real CS2 layout without touching the host system.
    """
    root = tmp_path / "game" / "csgo"
    plugin_dir = root / "addons" / "counterstrikesharp" / "plugins" / "sample"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "test_plugin.dll").write_bytes(ORIGINAL_CONTENT)
    return root


def _dll_path(csgo_dir: Path) -> Path:
    return (
        csgo_dir
        / "addons"
        / "counterstrikesharp"
        / "plugins"
        / "sample"
        / "test_plugin.dll"
    )


# ---------------------------------------------------------------------------
# Snapshot creation
# ---------------------------------------------------------------------------

def test_take_snapshot_creates_archive_and_metadata(
    csgo_dir: Path,
    tmp_path: Path,
) -> None:
    """
    take_snapshot() must write both the .zip archive and its companion
    .json metadata file, and the returned SnapshotMeta must point at them.
    """
    snapshot_dir = tmp_path / "snapshots"

    meta = take_snapshot(csgo_dir, snapshot_dir, label="pre_install")

    archive = Path(meta.archive_path)
    assert archive.exists(), f"Snapshot archive missing: {archive}"
    assert archive.stat().st_size > 0, "Snapshot archive was written empty"
    assert "addons" in meta.dirs_captured, (
        f"'addons' not tracked in captured dirs: {meta.dirs_captured}"
    )

    meta_json = snapshot_dir / f"{meta.snapshot_id}.json"
    assert meta_json.exists(), "Snapshot metadata JSON was not written"


# ---------------------------------------------------------------------------
# Self-healing rollback
# ---------------------------------------------------------------------------

def test_rollback_restores_corrupted_file_to_original(
    csgo_dir: Path,
    tmp_path: Path,
) -> None:
    """
    Self-healing guarantee: after take_snapshot() captures state and a
    broken install corrupts the DLL, rollback() must restore byte-exact
    original content.
    """
    snapshot_dir = tmp_path / "snapshots"
    dll_path = _dll_path(csgo_dir)
    assert dll_path.read_bytes() == ORIGINAL_CONTENT

    meta = take_snapshot(csgo_dir, snapshot_dir, label="pre_install")

    # Simulate a broken install overwriting the plugin binary
    dll_path.write_bytes(CORRUPTED_CONTENT)
    assert dll_path.read_bytes() == CORRUPTED_CONTENT

    rollback(meta, csgo_dir)

    assert dll_path.exists(), "Rollback did not recreate the plugin file"
    assert dll_path.read_bytes() == ORIGINAL_CONTENT, (
        "Rollback failed to restore the original plugin bytes"
    )


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------

def test_rollback_raises_when_snapshot_archive_missing(tmp_path: Path) -> None:
    """
    A SnapshotMeta whose archive_path does not exist must trigger
    SnapshotError — silent failure would defeat the self-healing contract.
    """
    fake_meta = SnapshotMeta(
        snapshot_id="20250101T000000_missing",
        label="missing",
        created_at="2025-01-01T00:00:00+00:00",
        archive_path=str(tmp_path / "does_not_exist.zip"),
        dirs_captured=["addons"],
    )
    csgo_dir = tmp_path / "csgo"
    csgo_dir.mkdir()

    with pytest.raises(SnapshotError):
        rollback(fake_meta, csgo_dir)


# ---------------------------------------------------------------------------
# list_snapshots — chronological ordering
# ---------------------------------------------------------------------------

def _write_fake_meta(
    snapshot_dir: Path,
    snapshot_id: str,
    created_at: str,
    *,
    with_archive: bool = False,
) -> None:
    """Write a companion .json metadata file (and optional .zip archive)."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / f"{snapshot_id}.json").write_text(
        json.dumps({
            "snapshot_id": snapshot_id,
            "label": snapshot_id,
            "created_at": created_at,
            "archive_path": str(snapshot_dir / f"{snapshot_id}.zip"),
            "dirs_captured": ["addons"],
        }),
        encoding="utf-8",
    )
    if with_archive:
        (snapshot_dir / f"{snapshot_id}.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)


def test_list_snapshots_sorted_ascending(tmp_path: Path) -> None:
    """
    list_snapshots() must return SnapshotMeta objects ordered by
    created_at ascending — oldest first — so cleanup can trim the head
    without recomputing timestamps.
    """
    snapshot_dir = tmp_path / "snapshots"

    _write_fake_meta(snapshot_dir, "20250703T090000_c", "2025-07-03T09:00:00+00:00")
    _write_fake_meta(snapshot_dir, "20250701T090000_a", "2025-07-01T09:00:00+00:00")
    _write_fake_meta(snapshot_dir, "20250702T090000_b", "2025-07-02T09:00:00+00:00")

    metas = list_snapshots(snapshot_dir)

    assert [m.snapshot_id for m in metas] == [
        "20250701T090000_a",
        "20250702T090000_b",
        "20250703T090000_c",
    ], "list_snapshots() did not sort by created_at ascending"


def test_list_snapshots_returns_empty_when_directory_missing(tmp_path: Path) -> None:
    """A non-existent snapshot directory must return an empty list, not raise."""
    assert list_snapshots(tmp_path / "never_created") == []


# ---------------------------------------------------------------------------
# cleanup_old_snapshots — retention policy
# ---------------------------------------------------------------------------

def test_cleanup_old_snapshots_keeps_latest_and_deletes_the_rest(
    tmp_path: Path,
) -> None:
    """
    cleanup_old_snapshots(snapshot_dir, keep=2) must delete every snapshot
    beyond the newest 2 — both the .zip archive and its .json companion —
    and report the deleted count so the caller can log it.
    """
    snapshot_dir = tmp_path / ".snapshots"
    ordered = [
        ("20250701T000000_v1", "2025-07-01T00:00:00+00:00"),
        ("20250702T000000_v2", "2025-07-02T00:00:00+00:00"),
        ("20250703T000000_v3", "2025-07-03T00:00:00+00:00"),
        ("20250704T000000_v4", "2025-07-04T00:00:00+00:00"),
        ("20250705T000000_v5", "2025-07-05T00:00:00+00:00"),
    ]
    for sid, created in ordered:
        _write_fake_meta(snapshot_dir, sid, created, with_archive=True)

    deleted = cleanup_old_snapshots(snapshot_dir, keep=2)

    assert deleted == 3, f"Expected 3 deletions, got {deleted}"

    surviving_json = {p.stem for p in snapshot_dir.glob("*.json")}
    surviving_zip = {p.stem for p in snapshot_dir.glob("*.zip")}
    expected_survivors = {"20250704T000000_v4", "20250705T000000_v5"}

    assert surviving_json == expected_survivors, (
        f"Wrong .json survivors: {surviving_json}"
    )
    assert surviving_zip == expected_survivors, (
        f"Wrong .zip survivors: {surviving_zip}"
    )


def test_cleanup_old_snapshots_noop_when_under_keep_threshold(
    tmp_path: Path,
) -> None:
    """When there are fewer snapshots than `keep`, nothing must be deleted."""
    snapshot_dir = tmp_path / ".snapshots"
    _write_fake_meta(
        snapshot_dir, "20250701T000000_only", "2025-07-01T00:00:00+00:00",
        with_archive=True,
    )

    deleted = cleanup_old_snapshots(snapshot_dir, keep=5)

    assert deleted == 0
    assert (snapshot_dir / "20250701T000000_only.json").exists()
    assert (snapshot_dir / "20250701T000000_only.zip").exists()
