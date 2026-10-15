"""
mod_manager.py — GitHub release fetcher and Smart Unzip engine.

The core challenge: every plugin ZIP has a different internal layout.
_classify_zip() inspects the namelist before extracting a single byte,
then _extract_zip() applies the correct strategy. No intermediate temp
directories for the WRAPPER_FLATTEN case — members are piped directly
to their destination paths.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Optional

import httpx

from models.schemas import ModInstallResult, ProgressEvent, ZipCase
from utils.http_client import GITHUB_API_BASE, build_async_client, github_retry

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class UnrecognizedZipStructureError(Exception):
    """ZIP layout doesn't match any known case. Includes namelist in message."""


class GitHubRateLimitError(Exception):
    """GitHub API returned HTTP 403 — rate limit reached."""


class InvalidRepoReferenceError(Exception):
    """Input string cannot be parsed as an owner/repo reference."""


# ---------------------------------------------------------------------------
# Repo string parsing
# ---------------------------------------------------------------------------

_REPO_PATTERNS = [
    re.compile(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$"),
    re.compile(r"^([^/]+)/([^/]+)$"),
]


def parse_repo_string(repo: str) -> tuple[str, str]:
    """
    Accepts 'owner/repo' or 'https://github.com/owner/repo'.
    Returns (owner, repo_name). Raises InvalidRepoReferenceError otherwise.
    """
    repo = repo.strip()
    for pattern in _REPO_PATTERNS:
        m = pattern.match(repo)
        if m:
            return m.group(1), m.group(2)
    raise InvalidRepoReferenceError(
        f"Cannot parse '{repo}' as a GitHub repo. "
        "Use 'owner/repo' or 'https://github.com/owner/repo'."
    )


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

_WINDOWS_ASSET_RE = re.compile(r"(?:^|[^a-z])win(?:dows|64|32)?(?:[^a-z]|$)", re.IGNORECASE)
_SIDE_ARCHIVE_RE = re.compile(
    r"(?:^|[^a-z])(?:website|web|src|source|docs?|examples?|debug|symbols)(?:[^a-z]|$)",
    re.IGNORECASE,
)


def _asset_score(name: str, repo_name: str) -> int:
    """Higher is better: Windows plugin build > neutral > Linux / side archives."""
    lower = name.lower()
    score = 0
    if _WINDOWS_ASSET_RE.search(name):
        score += 4
    if "linux" in lower:
        score -= 4
    if _SIDE_ARCHIVE_RE.search(name):
        score -= 3
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    core = _norm(re.sub(r"^(?:cs2|css|cs)[-_.]?", "", repo_name, flags=re.IGNORECASE))
    if core and core in _norm(name):
        score += 1
    return score


@github_retry()
async def resolve_latest_asset_url(
    repo: str,
    asset_keyword: Optional[str],
    http_client: httpx.AsyncClient,
) -> tuple[str, str, str]:
    """
    Fetches the latest release from the GitHub API and returns
    (download_url, tag_name, commit_ref). Prefers assets matching asset_keyword,
    then .zip files, then the first available asset.
    """
    owner, repo_name = parse_repo_string(repo)
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo_name}/releases/latest"

    response = await http_client.get(url)

    if response.status_code == 403:
        raise GitHubRateLimitError(
            "GitHub API rate limit reached. Wait a few minutes and try again.\n"
            f"URL: {url}"
        )
    if response.status_code == 404:
        raise InvalidRepoReferenceError(
            f"Repository '{owner}/{repo_name}' not found or has no published releases."
        )
    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as exc:
        raise InvalidRepoReferenceError(
            f"GitHub returned invalid JSON for '{owner}/{repo_name}': {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise InvalidRepoReferenceError(
            f"Unexpected GitHub response shape for '{owner}/{repo_name}'."
        )

    version: str = data.get("tag_name", "unknown") or "unknown"
    commit_ref: str = data.get("target_commitish", "") or ""
    assets_raw = data.get("assets", [])
    assets: list[dict] = [a for a in assets_raw if isinstance(a, dict) and a.get("browser_download_url") and a.get("name")]

    if not assets:
        raise InvalidRepoReferenceError(
            f"'{owner}/{repo_name}' latest release has no downloadable assets."
        )

    if asset_keyword:
        matching = [a for a in assets if asset_keyword.lower() in a["name"].lower()]
        if matching:
            return matching[0]["browser_download_url"], version, commit_ref

    zip_assets = [a for a in assets if a["name"].lower().endswith(".zip")]
    if zip_assets:
        # Releases often ship per-OS builds and side archives (a website, the
        # source). Rank instead of taking the first zip; ties keep list order.
        best = max(zip_assets, key=lambda a: _asset_score(a["name"], repo_name))
        return best["browser_download_url"], version, commit_ref

    return assets[0]["browser_download_url"], version, commit_ref


async def download_asset(
    url: str,
    dest_path: Path,
    http_client: httpx.AsyncClient,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> None:
    """
    Streams the download to dest_path in 64 KB chunks.
    Calls on_progress(bytes_downloaded, total_bytes) after each chunk.
    total_bytes is -1 when Content-Length is absent.
    """
    async with http_client.stream("GET", url) as response:
        response.raise_for_status()
        try:
            total = int(response.headers.get("content-length", -1))
        except (TypeError, ValueError):
            total = -1
        downloaded = 0

        try:
            with dest_path.open("wb") as fh:
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        try:
                            on_progress(downloaded, total)
                        except Exception:
                            pass
        except OSError as exc:
            try:
                dest_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise OSError(
                f"Failed to write downloaded asset to {dest_path}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Smart Unzip engine
# ---------------------------------------------------------------------------

_SKIP_PREFIXES = {"__MACOSX", ".DS_Store"}


def _classify_zip(namelist: list[str]) -> tuple[ZipCase, Optional[str]]:
    """
    Pure function — inspects a ZIP namelist and returns (ZipCase, wrapper_prefix).

    Decision tree:
      1. If 'addons' is a top-level name → DIRECT (Metamod / CSSharp layout)
      2. If exactly ONE top-level directory exists → WRAPPER_FLATTEN
         (strip that directory's name from all paths)
      3. If any .dll files exist at the root level → FLAT_DLL
      4. Otherwise → AMBIGUOUS (raises in the caller)

    wrapper_prefix is set only for WRAPPER_FLATTEN (e.g. 'MyPlugin-v1.2/').
    """
    # Build set of top-level names, filtering macOS artifacts
    top_level = {
        path.split("/")[0]
        for path in namelist
        if path and not any(path.startswith(skip) for skip in _SKIP_PREFIXES)
    }

    # Case-insensitive: Windows extraction works either way, but a
    # case-sensitive miss here would flatten "Addons/" as a wrapper folder
    # and destroy the plugin's directory structure.
    if any(name.lower() == "addons" for name in top_level):
        return ZipCase.DIRECT, None

    # A name is a real directory if at least one entry starts with "name/"
    real_dirs = {
        name for name in top_level
        if any(p.startswith(name + "/") for p in namelist)
    }

    # Files beside the single directory mean it is a subfolder (e.g. lang/),
    # not a wrapper — flattening it would drop the root-level DLLs.
    root_files = [
        p for p in namelist
        if p and "/" not in p and not any(p.startswith(skip) for skip in _SKIP_PREFIXES)
    ]

    if len(real_dirs) == 1 and not root_files:
        wrapper = next(iter(real_dirs))
        return ZipCase.WRAPPER_FLATTEN, wrapper + "/"

    # Any DLL files sitting directly at the archive root?
    root_dlls = [
        p for p in namelist
        if "/" not in p.rstrip("/") and p.lower().endswith(".dll")
    ]
    if root_dlls:
        return ZipCase.FLAT_DLL, None

    return ZipCase.AMBIGUOUS, None


def _routes_to_addons(
    namelist: list[str],
    case: ZipCase,
    wrapper_prefix: Optional[str],
) -> bool:
    """
    True when the archive's effective content root is an addons/ tree.

    Such content must land in csgo_dir regardless of the per-plugin target
    directory — otherwise it nests under plugins/<repo>/addons/ and the
    engine never loads it. Covers both DIRECT archives and WRAPPER_FLATTEN
    archives whose single wrapper folder contains addons/.
    """
    if case == ZipCase.DIRECT:
        return True
    if case == ZipCase.WRAPPER_FLATTEN and wrapper_prefix:
        for name in namelist:
            if name.startswith(wrapper_prefix):
                rest = name[len(wrapper_prefix):]
                if rest and rest.split("/", 1)[0].lower() == "addons":
                    return True
    return False


def _extract_zip(
    zip_path: Path,
    target_dir: Path,
    case: ZipCase,
    wrapper_prefix: Optional[str],
) -> list[str]:
    """
    Extracts a ZIP according to its classified case.

    WRAPPER_FLATTEN writes member data directly to the target path by piping
    zip.open(info) → dest.open('wb'), so no intermediate temp directory is needed.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    files_written: list[str] = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename

            # Skip macOS artifacts and directory entries
            if any(name.startswith(skip) for skip in _SKIP_PREFIXES):
                continue
            if info.is_dir():
                continue

            if case == ZipCase.WRAPPER_FLATTEN:
                if wrapper_prefix and not name.startswith(wrapper_prefix):
                    continue  # root-level files (e.g. LICENSE) are not plugin content
                relative = name[len(wrapper_prefix):] if wrapper_prefix else name
                if not relative:
                    continue
                dest = target_dir / relative
            else:
                # DIRECT or FLAT_DLL — extract as-is
                dest = target_dir / name

            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, dest.open("wb") as dst:
                dst.write(src.read())
            files_written.append(str(dest))

    return files_written


# ---------------------------------------------------------------------------
# High-level public API
# ---------------------------------------------------------------------------

async def install_mod(
    repo: str,
    target_dir: Path,
    asset_keyword: Optional[str] = None,
    http_client: Optional[httpx.AsyncClient] = None,
    on_progress: Optional[Callable[[ProgressEvent], None]] = None,
    direct_target_dir: Optional[Path] = None,
) -> ModInstallResult:
    """
    Full pipeline for one mod installation:
      1. Resolve latest release asset URL via GitHub API
      2. Download ZIP into a temp directory
      3. Classify ZIP structure
      4. Extract into target_dir using the appropriate strategy
      5. Clean up temp directory (always, via context manager)

    If http_client is None, a new one is created and closed on exit.
    When direct_target_dir is given, archives whose effective content root
    is addons/ are extracted there instead of target_dir — the redirect
    that keeps DIRECT-layout plugin releases loadable by the engine.
    """
    _close_client = False
    if http_client is None:
        http_client = build_async_client()
        _close_client = True

    try:
        download_url, version, commit_ref = await resolve_latest_asset_url(
            repo, asset_keyword, http_client
        )
        return await _install_from_url(
            download_url, version, commit_ref, repo, target_dir,
            http_client, on_progress, direct_target_dir,
        )
    finally:
        if _close_client:
            await http_client.aclose()


async def _install_from_url(
    download_url: str,
    version: str,
    commit_ref: str,
    repo: str,
    target_dir: Path,
    http_client: httpx.AsyncClient,
    on_progress: Optional[Callable[[ProgressEvent], None]] = None,
    direct_target_dir: Optional[Path] = None,
) -> ModInstallResult:
    """Download → classify → extract for an already-resolved asset URL."""
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "asset.zip"

        def _on_chunk(downloaded: int, total: int) -> None:
            if on_progress and total > 0:
                pct = (downloaded / total) * 100.0
                on_progress(ProgressEvent(step=repo, percent=pct, message="Downloading"))

        await download_asset(download_url, zip_path, http_client, _on_chunk)

        if on_progress:
            try:
                on_progress(ProgressEvent(step=repo, percent=100.0, message="Extracting"))
            except Exception:
                pass

        try:
            with zipfile.ZipFile(zip_path) as zf:
                namelist = zf.namelist()
        except zipfile.BadZipFile as exc:
            raise UnrecognizedZipStructureError(
                f"Downloaded asset for '{repo}' is not a valid ZIP: {exc}"
            ) from exc

        case, dest_dir, files_written = _extract_archive(
            zip_path, namelist, repo, target_dir, direct_target_dir
        )

    return ModInstallResult(
        repo=repo,
        version=version,
        zip_case=case,
        files_written=files_written,
        target_dir=str(dest_dir),
        download_url=download_url,
        commit_ref=commit_ref,
    )


# Game content folders that belong directly under csgo/ when a release ships
# them beside its addons tree (e.g. LevelsRanks: Plugin/addons + Sounds/sounds).
_CONTENT_DIRS = {
    "sounds", "soundevents", "materials", "models", "maps", "particles",
    "panorama", "resource", "cfg", "scripts",
}
_CSS_CHILDREN = {"plugins", "shared", "configs", "gamedata"}


def _anchored_plan(namelist: list[str]) -> Optional[tuple[str, str]]:
    """
    Finds where the engine tree starts inside an archive, at any depth:
      .../addons/<x>/...                       → (prefix, "")         mounted on csgo/
      .../counterstrikesharp/{plugins,shared…} → (prefix, "addons/")  mounted on csgo/addons/
    Returns the shallowest (strip_prefix, mount) or None.
    """
    best: Optional[tuple[int, str, str]] = None
    for name in namelist:
        parts = name.split("/")
        for i, seg in enumerate(parts[:-1]):
            low = seg.lower()
            nxt = parts[i + 1].lower() if i + 1 < len(parts) else ""
            if low == "addons" and nxt:
                mount = ""
            elif low == "counterstrikesharp" and nxt in _CSS_CHILDREN and (
                i == 0 or parts[i - 1].lower() != "addons"
            ):
                mount = "addons/"
            else:
                continue
            prefix = "/".join(parts[:i]) + ("/" if i else "")
            if best is None or i < best[0]:
                best = (i, prefix, mount)
            break
    return (best[1], best[2]) if best else None


def _extract_anchored(
    zip_path: Path, csgo_dir: Path, prefix: str, mount: str
) -> list[str]:
    """Extracts the engine tree under prefix onto csgo_dir/mount, plus any
    recognised game-content folders found elsewhere in the archive."""
    files_written: list[str] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or any(name.startswith(skip) for skip in _SKIP_PREFIXES):
                continue
            if name.startswith(prefix) and name[len(prefix):].split("/", 1)[0].lower() in (
                {"addons"} if mount == "" else {"counterstrikesharp"}
            ):
                dest = csgo_dir / mount / name[len(prefix):]
            else:
                parts = name.split("/")
                # "Sounds/sounds/..." — the outer folder is just a wrapper.
                idx = next(
                    (j for j, seg in enumerate(parts[:-1])
                     if seg.lower() in _CONTENT_DIRS and parts[j + 1].lower() != seg.lower()),
                    None,
                )
                if idx is None:
                    continue  # READMEs, screenshots, source — not server content
                dest = csgo_dir.joinpath(*parts[idx:])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, dest.open("wb") as dst:
                dst.write(src.read())
            files_written.append(str(dest))
    return files_written


def _extract_archive(
    zip_path: Path,
    namelist: list[str],
    label: str,
    target_dir: Path,
    direct_target_dir: Optional[Path],
) -> tuple[ZipCase, Path, list[str]]:
    """Classify → route → extract → align, shared by GitHub and local installs."""
    case, wrapper_prefix = _classify_zip(namelist)

    if direct_target_dir is not None:
        plan = _anchored_plan(namelist)
        simple = case == ZipCase.DIRECT or (
            plan is not None and plan[1] == "" and plan[0] == (wrapper_prefix or "")
        )
        if plan is not None and not simple:
            try:
                files = _extract_anchored(zip_path, direct_target_dir, *plan)
            except (OSError, zipfile.BadZipFile) as exc:
                raise UnrecognizedZipStructureError(
                    f"Failed to extract '{label}' into {direct_target_dir}: {exc}"
                ) from exc
            return ZipCase.ANCHORED, direct_target_dir, files

    if case == ZipCase.AMBIGUOUS:
        raise UnrecognizedZipStructureError(
            f"Cannot determine ZIP layout for '{label}'.\n"
            f"Archive contents (first 30 entries): {namelist[:30]}\n"
            "Expected: 'addons/' or 'counterstrikesharp/' somewhere, a single "
            "wrapper folder, or .dll files at root."
        )

    dest_dir = target_dir
    if direct_target_dir is not None and _routes_to_addons(namelist, case, wrapper_prefix):
        dest_dir = direct_target_dir
    try:
        files_written = _extract_zip(zip_path, dest_dir, case, wrapper_prefix)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UnrecognizedZipStructureError(
            f"Failed to extract '{label}' into {dest_dir}: {exc}"
        ) from exc
    if dest_dir == target_dir:
        dest_dir = align_plugin_folder(dest_dir)
    return case, dest_dir, files_written


def align_plugin_folder(plugin_dir: Path) -> Path:
    """
    CounterStrikeSharp only loads plugins/<Name>/<Name>.dll. Releases are
    installed under the repo name (e.g. CS2-Bot-Randomizer) while the assembly
    is often named differently (BotRandomizer.dll), so the folder is renamed
    to the assembly that owns the single <Name>.deps.json. Returns the final
    directory; leaves anything ambiguous untouched.
    """
    if plugin_dir.parent.name.lower() != "plugins" or not plugin_dir.is_dir():
        return plugin_dir
    if (plugin_dir / f"{plugin_dir.name}.dll").is_file():
        return plugin_dir
    names = [
        p.name[: -len(".deps.json")]
        for p in plugin_dir.glob("*.deps.json")
        if (plugin_dir / f"{p.name[: -len('.deps.json')]}.dll").is_file()
    ]
    if len(names) != 1:
        return plugin_dir
    dest = plugin_dir.parent / names[0]
    if dest.exists():
        # Reinstall over an aligned copy: overwrite files, keep extra configs.
        shutil.copytree(plugin_dir, dest, dirs_exist_ok=True)
        shutil.rmtree(plugin_dir)
    else:
        plugin_dir.rename(dest)
    return dest


def install_local(
    source: Path,
    target_dir: Path,
    direct_target_dir: Optional[Path] = None,
) -> ModInstallResult:
    """
    Installs a plugin from a local .zip or folder. A folder is zipped first so
    both go through the same classify → route → extract pipeline as GitHub
    downloads, and therefore land in exactly the same place.
    """
    source = Path(source).expanduser()
    if not source.exists():
        raise InvalidRepoReferenceError(f"Local plugin path not found: {source}")

    with tempfile.TemporaryDirectory() as tmp:
        if source.is_dir():
            zip_path = Path(shutil.make_archive(str(Path(tmp) / "local"), "zip", root_dir=source))
        elif source.suffix.lower() == ".zip":
            zip_path = source
        else:
            raise InvalidRepoReferenceError(
                f"Local plugin must be a folder or a .zip file: {source}"
            )

        try:
            with zipfile.ZipFile(zip_path) as zf:
                namelist = zf.namelist()
        except zipfile.BadZipFile as exc:
            raise UnrecognizedZipStructureError(f"'{source}' is not a valid ZIP: {exc}") from exc

        case, dest_dir, files_written = _extract_archive(
            zip_path, namelist, str(source), target_dir, direct_target_dir
        )

    return ModInstallResult(
        repo=source.stem,
        version="local",
        zip_case=case,
        files_written=files_written,
        target_dir=str(dest_dir),
        download_url=str(source),
        commit_ref="",
    )


# ---------------------------------------------------------------------------
# Metamod:Source 2.0 (CS2)
# ---------------------------------------------------------------------------

# GitHub's "latest" release of alliedmodders/metamod-source is the 1.x stable
# branch, which only supports Source 1 games. CS2 builds (2.0 dev) are
# published exclusively on the AlliedModders drop server.
MMS_DROP_BASE = "https://mms.alliedmods.net/mmsdrop/2.0"
MMS_LATEST_POINTER = f"{MMS_DROP_BASE}/mmsource-latest-windows"
_MMS_FILENAME_RE = re.compile(r"^mmsource-(2\.\d+\.\d+-git\d+)-windows\.zip$")


@github_retry()
async def resolve_metamod_cs2_url(http_client: httpx.AsyncClient) -> tuple[str, str]:
    """
    Reads the drop server's latest-pointer file and returns
    (download_url, version), e.g. ('.../mmsource-2.0.0-git1380-windows.zip',
    '2.0.0-git1380').
    """
    response = await http_client.get(MMS_LATEST_POINTER)
    response.raise_for_status()
    filename = response.text.strip()
    m = _MMS_FILENAME_RE.match(filename)
    if not m:
        raise InvalidRepoReferenceError(
            f"Unexpected Metamod 2.0 pointer content from {MMS_LATEST_POINTER}: {filename[:100]!r}"
        )
    return f"{MMS_DROP_BASE}/{filename}", m.group(1)


async def install_metamod_cs2(
    target_dir: Path,
    http_client: Optional[httpx.AsyncClient] = None,
    on_progress: Optional[Callable[[ProgressEvent], None]] = None,
) -> ModInstallResult:
    """Installs the latest Metamod:Source 2.0 Windows build into target_dir (csgo/)."""
    _close_client = False
    if http_client is None:
        http_client = build_async_client()
        _close_client = True
    try:
        download_url, version = await resolve_metamod_cs2_url(http_client)
        return await _install_from_url(
            download_url, version, "", "metamod-source-2.0", target_dir,
            http_client, on_progress,
        )
    finally:
        if _close_client:
            await http_client.aclose()
