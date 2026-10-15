from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional


class ZipCase(Enum):
    DIRECT = auto()           # addons/ at root — Metamod, CSSharp
    WRAPPER_FLATTEN = auto()  # single wrapper folder — most GitHub plugin releases
    FLAT_DLL = auto()         # DLL files directly at root
    ANCHORED = auto()         # addons/ or counterstrikesharp/ found deeper; routed onto csgo/
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
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    username: str = "root"
    password: str = ""
    database: str = "cs2_server"
    enabled: bool = False


@dataclass
class ServerConfig:
    gslt_token: str
    auth_key: str
    server_ip: str
    map: str = "de_dust2"
    rcon_password: str = ""
    server_port: int = 27015
    db_config: Optional[DatabaseConfig] = None
    workshop_map: str = ""         # Steam Workshop file id → +host_workshop_map
    workshop_collection: str = ""  # collection id → +host_workshop_collection

    def map_args(self) -> list[str]:
        """Launch args selecting the start map (workshop takes precedence)."""
        if self.workshop_collection:
            args = ["+host_workshop_collection", self.workshop_collection]
            if self.workshop_map:
                args += ["+host_workshop_map", self.workshop_map]
            return args
        if self.workshop_map:
            return ["+host_workshop_map", self.workshop_map]
        return ["+map", self.map]


@dataclass
class PluginRef:
    owner: str
    repo: str
    local_path: Optional[str] = None  # folder or .zip on disk instead of GitHub

    @property
    def display_name(self) -> str:
        return self.repo

    @property
    def full_ref(self) -> str:
        if self.local_path:
            return f"local:{self.repo}"
        return f"{self.owner}/{self.repo}"

    @property
    def source(self) -> str:
        return self.local_path or self.full_ref


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
class LogScanBaseline:
    console_log_offset: int   # console.log byte size at capture (0 if absent)
    captured_at_epoch: float  # capture time — mtime gate for CSSharp log files


@dataclass
class LogValidationResult:
    success: bool             # load signature seen AND zero error signatures
    plugin_name: str
    errors_detected: List[str] = field(default_factory=list)
    log_files_scanned: List[str] = field(default_factory=list)


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
