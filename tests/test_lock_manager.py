"""Tests for LockFileManager atomic persistence and lifecycle."""
from __future__ import annotations

import json
from pathlib import Path

from core.lock_manager import LockFileManager
from models.schemas import PluginLockEntry


def _build_entry(
    owner: str = "cssjunkie",
    repo: str = "SamplePlugin",
    version: str = "v1.0.0",
) -> PluginLockEntry:
    return PluginLockEntry(
        owner=owner,
        repo=repo,
        version=version,
        commit_ref="main",
        download_url=(
            f"https://github.com/{owner}/{repo}/releases/download/{version}/{repo}.zip"
        ),
        asset_keyword=None,
        installed_at="2025-07-05T12:00:00Z",
    )


# ---------------------------------------------------------------------------
# Default state
# ---------------------------------------------------------------------------

def test_load_returns_empty_lockfile_when_path_missing(tmp_path: Path) -> None:
    """A never-written lock file must load as a default empty LockFile."""
    mgr = LockFileManager(tmp_path / "cs2-plugins.lock")

    lock = mgr.load()

    assert lock.schema_version == 1
    assert lock.entries == {}


def test_load_returns_empty_lockfile_when_json_is_corrupt(tmp_path: Path) -> None:
    """
    Corrupt JSON must not crash — the manager falls back to a default
    LockFile so the installer can rebuild state from scratch.
    """
    lock_path = tmp_path / "cs2-plugins.lock"
    lock_path.write_text("{{{ not valid json", encoding="utf-8")

    lock = LockFileManager(lock_path).load()

    assert lock.entries == {}


# ---------------------------------------------------------------------------
# record() — atomic upsert
# ---------------------------------------------------------------------------

def test_record_persists_entry_atomically(tmp_path: Path) -> None:
    """
    record() must produce a valid JSON file with no lingering .tmp
    — proof that the atomic os.replace() write completed cleanly.
    """
    lock_path = tmp_path / "cs2-plugins.lock"
    mgr = LockFileManager(lock_path)
    entry = _build_entry()

    mgr.record(entry)

    assert lock_path.exists(), "Lock file was not created"
    assert not lock_path.with_suffix(".tmp").exists(), (
        "Temporary file was left behind — atomic write is broken"
    )

    raw = json.loads(lock_path.read_text(encoding="utf-8"))
    key = f"{entry.owner}/{entry.repo}"
    assert raw["schema_version"] == 1
    assert key in raw["entries"]
    assert raw["entries"][key]["version"] == "v1.0.0"


def test_record_upserts_existing_ref_without_duplication(tmp_path: Path) -> None:
    """
    Recording the same owner/repo twice must overwrite, not duplicate —
    the lock file is the single source of truth per plugin.
    """
    mgr = LockFileManager(tmp_path / "cs2-plugins.lock")

    mgr.record(_build_entry(version="v1.0.0"))
    mgr.record(_build_entry(version="v2.0.0"))

    entries = mgr.all_entries()
    assert len(entries) == 1
    assert entries[0].version == "v2.0.0"


# ---------------------------------------------------------------------------
# load() / get() round-trip
# ---------------------------------------------------------------------------

def test_get_returns_deserialized_entry(tmp_path: Path) -> None:
    """A recorded entry must round-trip back as a proper PluginLockEntry."""
    mgr = LockFileManager(tmp_path / "cs2-plugins.lock")
    entry = _build_entry(repo="ExamplePlugin", version="v2.1.3")
    mgr.record(entry)

    fetched = mgr.get(f"{entry.owner}/{entry.repo}")

    assert fetched is not None
    assert isinstance(fetched, PluginLockEntry)
    assert fetched.repo == "ExamplePlugin"
    assert fetched.version == "v2.1.3"
    assert fetched.commit_ref == "main"


def test_get_returns_none_for_unknown_ref(tmp_path: Path) -> None:
    """get() on a missing key returns None — never raises KeyError."""
    mgr = LockFileManager(tmp_path / "cs2-plugins.lock")
    assert mgr.get("ghost/plugin") is None


# ---------------------------------------------------------------------------
# remove()
# ---------------------------------------------------------------------------

def test_remove_deletes_entry_and_returns_true(tmp_path: Path) -> None:
    """remove() deletes the entry and reports True on success."""
    mgr = LockFileManager(tmp_path / "cs2-plugins.lock")
    entry = _build_entry()
    mgr.record(entry)

    removed = mgr.remove(f"{entry.owner}/{entry.repo}")

    assert removed is True
    assert mgr.get(f"{entry.owner}/{entry.repo}") is None
    assert mgr.all_entries() == []


def test_remove_returns_false_for_unknown_ref(tmp_path: Path) -> None:
    """remove() on a non-existent key returns False without raising."""
    mgr = LockFileManager(tmp_path / "cs2-plugins.lock")
    assert mgr.remove("ghost/plugin") is False
