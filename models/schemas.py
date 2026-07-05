from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional


class ZipCase(Enum):
    DIRECT = auto()           # addons/ at root — Metamod, CSSharp
    WRAPPER_FLATTEN = auto()  # single wrapper folder — most GitHub plugin releases
    FLAT_DLL = auto()         # DLL files directly at root
    AMBIGUOUS = auto()        # none of the above matched


class SteamCMDPhase(Enum):
    DOWNLOADING = auto()
    COMMITTING = auto()
    VALIDATING = auto()
    UNKNOWN = auto()


@dataclass
class SteamCMDEvent:
    phase: SteamCMDPhase
    percent: float
    raw_line: str


@dataclass
class ProgressEvent:
    step: str
    percent: float
    message: str = ""


@dataclass
class ServerConfig:
    gslt_token: str
    auth_key: str
    server_ip: str
    map: str = "de_dust2"
    rcon_password: str = ""


@dataclass
class PluginRef:
    owner: str
    repo: str

    @property
    def display_name(self) -> str:
        return self.repo

    @property
    def full_ref(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass
class ModInstallResult:
    repo: str
    version: str
    zip_case: ZipCase
    files_written: List[str]
    target_dir: str
    download_url: str = ""
    commit_ref: str = ""


@dataclass
class SnapshotMeta:
    snapshot_id: str
    label: str
    created_at: str
    archive_path: str
    dirs_captured: List[str]


@dataclass
class PluginLockEntry:
    owner: str
    repo: str
    version: str
    commit_ref: str
    download_url: str
    asset_keyword: Optional[str]
    installed_at: str


@dataclass
class LockFile:
    schema_version: int = 1
    entries: dict = field(default_factory=dict)


@dataclass
class RCONCommand:
    command: str
    args: List[str] = field(default_factory=list)

    def render(self) -> str:
        return " ".join([self.command] + self.args)


@dataclass
class RCONResponse:
    body: str
    success: bool
