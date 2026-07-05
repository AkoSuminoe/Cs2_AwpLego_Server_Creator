from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Optional

from models.schemas import LockFile, PluginLockEntry


class LockFileManager:
    def __init__(self, lock_path: Path) -> None:
        self._path = lock_path
        self._tmp = lock_path.with_suffix(".tmp")

    def load(self) -> LockFile:
        if not self._path.exists():
            return LockFile()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            entries: dict = {}
            for key, val in raw.get("entries", {}).items():
                try:
                    entries[key] = PluginLockEntry(**val)
                except TypeError:
                    continue
            return LockFile(
                schema_version=raw.get("schema_version", 1),
                entries=entries,
            )
        except (json.JSONDecodeError, OSError):
            return LockFile()

    def record(self, entry: PluginLockEntry) -> None:
        lock = self.load()
        key = f"{entry.owner}/{entry.repo}"
        lock.entries[key] = entry
        self._save(lock)

    def remove(self, full_ref: str) -> bool:
        lock = self.load()
        if full_ref not in lock.entries:
            return False
        del lock.entries[full_ref]
        self._save(lock)
        return True

    def get(self, full_ref: str) -> Optional[PluginLockEntry]:
        return self.load().entries.get(full_ref)

    def all_entries(self) -> list[PluginLockEntry]:
        return list(self.load().entries.values())

    def _save(self, lock: LockFile) -> None:
        serialized_entries = {
            key: dataclasses.asdict(entry)
            for key, entry in lock.entries.items()
        }
        data = {
            "schema_version": lock.schema_version,
            "entries": serialized_entries,
        }
        try:
            self._tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(self._tmp, self._path)
        except OSError:
            try:
                self._tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
