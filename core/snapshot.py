from __future__ import annotations

import datetime
import json
import os
import shutil
import zipfile
from pathlib import Path

from models.schemas import SnapshotMeta

_SNAPSHOT_DIRS = ("addons", "cfg")
_MAX_SNAPSHOTS = 5


class SnapshotError(Exception):
    pass


def take_snapshot(csgo_dir: Path, snapshot_dir: Path, label: str = "") -> SnapshotMeta:
    if not csgo_dir.is_dir():
        raise SnapshotError(f"csgo_dir does not exist: {csgo_dir}")

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_id = _build_snapshot_id(label)
    archive_path = snapshot_dir / f"{snapshot_id}.zip"
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    dirs_captured: list[str] = []
    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for d in _SNAPSHOT_DIRS:
                src = csgo_dir / d
                if not src.is_dir():
                    continue
                dirs_captured.append(d)
                for file in src.rglob("*"):
                    if file.is_file():
                        arcname = d + "/" + file.relative_to(src).as_posix()
                        zf.write(file, arcname)
    except OSError as exc:
        raise SnapshotError(f"Failed to create snapshot archive: {exc}") from exc

    meta = SnapshotMeta(
        snapshot_id=snapshot_id,
        label=label,
        created_at=created_at,
        archive_path=str(archive_path),
        dirs_captured=dirs_captured,
    )

    _write_meta(snapshot_dir / f"{snapshot_id}.json", meta)
    return meta


def rollback(meta: SnapshotMeta, csgo_dir: Path) -> None:
    archive = Path(meta.archive_path)
    if not archive.exists():
        raise SnapshotError(f"Snapshot archive not found: {archive}")

    for d in meta.dirs_captured:
        target = csgo_dir / d
        shutil.rmtree(target, ignore_errors=True)

    try:
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(csgo_dir)
    except (zipfile.BadZipFile, OSError) as exc:
        raise SnapshotError(f"Failed to extract snapshot: {exc}") from exc


def list_snapshots(snapshot_dir: Path) -> list[SnapshotMeta]:
    if not snapshot_dir.is_dir():
        return []

    metas: list[SnapshotMeta] = []
    for json_file in snapshot_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            metas.append(SnapshotMeta(**data))
        except (json.JSONDecodeError, TypeError, KeyError, OSError):
            continue

    metas.sort(key=lambda m: m.created_at)
    return metas


def cleanup_old_snapshots(snapshot_dir: Path, keep: int = _MAX_SNAPSHOTS) -> int:
    metas = list_snapshots(snapshot_dir)
    to_delete = metas[: max(0, len(metas) - keep)]
    deleted = 0
    for meta in to_delete:
        for suffix in (".zip", ".json"):
            path = snapshot_dir / f"{meta.snapshot_id}{suffix}"
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        deleted += 1
    return deleted


def _build_snapshot_id(label: str) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    if label:
        slug = label.replace(" ", "_").replace("/", "-")[:32]
        return f"{ts}_{slug}"
    return ts


def _write_meta(json_path: Path, meta: SnapshotMeta) -> None:
    tmp = json_path.with_suffix(".tmp")
    data = {
        "snapshot_id": meta.snapshot_id,
        "label": meta.label,
        "created_at": meta.created_at,
        "archive_path": meta.archive_path,
        "dirs_captured": meta.dirs_captured,
    }
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, json_path)
