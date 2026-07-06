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
        return zip_assets[0]["browser_download_url"], version, commit_ref

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

    if "addons" in top_level:
        return ZipCase.DIRECT, None

    # A name is a real directory if at least one entry starts with "name/"
    real_dirs = {
        name for name in top_level
        if any(p.startswith(name + "/") for p in namelist)
    }

    if len(real_dirs) == 1:
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
                if rest and rest.split("/", 1)[0] == "addons":
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

            case, wrapper_prefix = _classify_zip(namelist)

            if case == ZipCase.AMBIGUOUS:
                raise UnrecognizedZipStructureError(
                    f"Cannot determine ZIP layout for '{repo}'.\n"
                    f"Archive contents (first 30 entries): {namelist[:30]}\n"
                    "Expected: 'addons/' at root, a single wrapper folder, "
                    "or .dll files at root."
                )

            dest_dir = target_dir
            if direct_target_dir is not None and _routes_to_addons(namelist, case, wrapper_prefix):
                dest_dir = direct_target_dir

            try:
                files_written = _extract_zip(zip_path, dest_dir, case, wrapper_prefix)
            except (OSError, zipfile.BadZipFile) as exc:
                raise UnrecognizedZipStructureError(
                    f"Failed to extract '{repo}' into {dest_dir}: {exc}"
                ) from exc

        return ModInstallResult(
            repo=repo,
            version=version,
            zip_case=case,
            files_written=files_written,
            target_dir=str(dest_dir),
            download_url=download_url,
            commit_ref=commit_ref,
        )

    finally:
        if _close_client:
            await http_client.aclose()
