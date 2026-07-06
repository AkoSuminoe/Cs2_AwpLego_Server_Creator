"""Tests for the Smart Unzip classification engine and exception boundary."""
from __future__ import annotations

import asyncio
import shutil
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

from core import mod_manager
from core.mod_manager import (
    GitHubRateLimitError,
    InvalidRepoReferenceError,
    UnrecognizedZipStructureError,
    _classify_zip,
    download_asset,
    install_mod,
    parse_repo_string,
    resolve_latest_asset_url,
)
from models.schemas import ZipCase


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Build an offline AsyncClient whose responses are produced by `handler`."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Decision tree — pure, in-memory tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("namelist", "expected_case", "expected_prefix"),
    [
        (
            ["addons/", "addons/metamod/", "addons/metamod/bin.so"],
            ZipCase.DIRECT,
            None,
        ),
        (
            ["MyPlugin-v1.2/", "MyPlugin-v1.2/MyPlugin.dll"],
            ZipCase.WRAPPER_FLATTEN,
            "MyPlugin-v1.2/",
        ),
        (
            ["MyPlugin.dll", "README.md"],
            ZipCase.FLAT_DLL,
            None,
        ),
    ],
    ids=["direct_addons_root", "wrapper_flatten_single_dir", "flat_dll_root"],
)
def test_classify_zip_recognizes_valid_layouts(
    namelist: list[str],
    expected_case: ZipCase,
    expected_prefix: Optional[str],
) -> None:
    """
    The classifier must return the correct ZipCase and wrapper prefix for
    every supported archive layout. Runs entirely in memory — no disk,
    no network, no fixtures.
    """
    case, prefix = _classify_zip(namelist)

    assert case is expected_case, (
        f"Expected case {expected_case.name}, got {case.name}"
    )
    assert prefix == expected_prefix, (
        f"Expected prefix {expected_prefix!r}, got {prefix!r}"
    )


def test_classify_zip_flags_unrecognized_layout_as_ambiguous() -> None:
    """
    An archive that fits none of the known layouts must be classified as
    AMBIGUOUS so the install pipeline can convert it into a public-facing
    UnrecognizedZipStructureError at the API boundary.
    """
    namelist = ["README.md", "LICENSE"]

    case, prefix = _classify_zip(namelist)

    assert case is ZipCase.AMBIGUOUS
    assert prefix is None


# ---------------------------------------------------------------------------
# Exception boundary — install_mod converts AMBIGUOUS into the public error
# ---------------------------------------------------------------------------

def test_install_mod_raises_when_zip_layout_is_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    End-to-end guarantee: install_mod() converts an AMBIGUOUS classification
    into UnrecognizedZipStructureError. The GitHub API and download layer
    are monkeypatched so the test stays fully offline.
    """
    ambiguous_zip = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(ambiguous_zip, "w") as zf:
        zf.writestr("dir_a/notes.txt", b"alpha")
        zf.writestr("dir_b/notes.txt", b"beta")

    async def _fake_resolve(*_: Any, **__: Any) -> tuple[str, str, str]:
        return "https://fake.local/ambiguous.zip", "v0.0.1", "main"

    async def _fake_download(
        _url: str,
        dest_path: Path,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        shutil.copyfile(ambiguous_zip, dest_path)

    monkeypatch.setattr(mod_manager, "resolve_latest_asset_url", _fake_resolve)
    monkeypatch.setattr(mod_manager, "download_asset", _fake_download)

    target_dir = tmp_path / "target"

    async def _invoke() -> None:
        await install_mod(
            repo="fake/repo",
            target_dir=target_dir,
            http_client=object(),  # type: ignore[arg-type]
        )

    with pytest.raises(UnrecognizedZipStructureError):
        asyncio.run(_invoke())


# ---------------------------------------------------------------------------
# parse_repo_string — pure regex-based parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("owner/repo", ("owner", "repo")),
        ("cssjunkie/CS2-Plugin", ("cssjunkie", "CS2-Plugin")),
        ("https://github.com/owner/repo", ("owner", "repo")),
        ("https://github.com/owner/repo/", ("owner", "repo")),
        ("https://github.com/owner/repo.git", ("owner", "repo")),
        ("http://github.com/owner/repo", ("owner", "repo")),
        ("  owner/repo  ", ("owner", "repo")),
    ],
    ids=[
        "slug",
        "slug_with_hyphen",
        "https_url",
        "https_url_trailing_slash",
        "https_url_git_suffix",
        "http_url",
        "whitespace_stripped",
    ],
)
def test_parse_repo_string_accepts_valid_forms(
    raw: str,
    expected: tuple[str, str],
) -> None:
    """Every supported input form must decode into a clean (owner, repo) tuple."""
    assert parse_repo_string(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "just-a-string", "owner/", "/repo", "https://gitlab.com/owner/repo"],
    ids=["empty", "no_slash", "empty_repo", "empty_owner", "wrong_host"],
)
def test_parse_repo_string_rejects_invalid_input(raw: str) -> None:
    """Anything that doesn't match a known form must raise InvalidRepoReferenceError."""
    with pytest.raises(InvalidRepoReferenceError):
        parse_repo_string(raw)


# ---------------------------------------------------------------------------
# resolve_latest_asset_url — GitHub API via httpx.MockTransport (offline)
# ---------------------------------------------------------------------------

def test_resolve_latest_asset_url_prefers_matching_keyword_asset() -> None:
    """When an asset name contains the keyword, it must be selected first."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "tag_name": "v1.4.2",
            "target_commitish": "main",
            "assets": [
                {"name": "plugin-linux.zip",
                 "browser_download_url": "https://fake/linux.zip"},
                {"name": "plugin-with-runtime-windows.zip",
                 "browser_download_url": "https://fake/windows.zip"},
            ],
        })

    async def _run() -> tuple[str, str, str]:
        async with _mock_client(_handler) as client:
            return await resolve_latest_asset_url(
                "owner/repo", "with-runtime-windows", client
            )

    url, version, commit_ref = asyncio.run(_run())
    assert url == "https://fake/windows.zip"
    assert version == "v1.4.2"
    assert commit_ref == "main"


def test_resolve_latest_asset_url_falls_back_to_zip_when_no_keyword_match() -> None:
    """With no keyword or no matching asset, the first .zip must win."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "tag_name": "v1.0.0",
            "target_commitish": "main",
            "assets": [
                {"name": "plugin.tar.gz",
                 "browser_download_url": "https://fake/plugin.tar.gz"},
                {"name": "plugin.zip",
                 "browser_download_url": "https://fake/plugin.zip"},
            ],
        })

    async def _run() -> str:
        async with _mock_client(_handler) as client:
            url, _, _ = await resolve_latest_asset_url("owner/repo", None, client)
            return url

    assert asyncio.run(_run()) == "https://fake/plugin.zip"


def test_resolve_latest_asset_url_raises_rate_limit_on_403() -> None:
    """HTTP 403 must surface as the dedicated GitHubRateLimitError."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limit exceeded"})

    async def _run() -> None:
        async with _mock_client(_handler) as client:
            await resolve_latest_asset_url("owner/repo", None, client)

    with pytest.raises(GitHubRateLimitError):
        asyncio.run(_run())


def test_resolve_latest_asset_url_raises_invalid_ref_on_404() -> None:
    """HTTP 404 must translate into InvalidRepoReferenceError."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    async def _run() -> None:
        async with _mock_client(_handler) as client:
            await resolve_latest_asset_url("ghost/repo", None, client)

    with pytest.raises(InvalidRepoReferenceError):
        asyncio.run(_run())


def test_resolve_latest_asset_url_raises_when_release_has_no_assets() -> None:
    """A release with an empty assets array must raise InvalidRepoReferenceError."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "tag_name": "v1.0.0",
            "target_commitish": "main",
            "assets": [],
        })

    async def _run() -> None:
        async with _mock_client(_handler) as client:
            await resolve_latest_asset_url("owner/repo", None, client)

    with pytest.raises(InvalidRepoReferenceError):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# download_asset — streaming
# ---------------------------------------------------------------------------

def test_download_asset_writes_response_body_to_dest(tmp_path: Path) -> None:
    """download_asset() must stream the full response body into dest_path."""
    payload = b"BINARY-PAYLOAD" * 4096  # ~57 KB

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"content-length": str(len(payload))},
        )

    dest = tmp_path / "download.bin"

    async def _run() -> None:
        async with _mock_client(_handler) as client:
            await download_asset("https://fake/file", dest, client)

    asyncio.run(_run())

    assert dest.exists()
    assert dest.read_bytes() == payload


def test_download_asset_invokes_progress_callback_with_totals(tmp_path: Path) -> None:
    """
    The on_progress callback must fire at least once and the final call
    must report bytes_downloaded == total.
    """
    payload = b"y" * 8000

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"content-length": str(len(payload))},
        )

    dest = tmp_path / "download.bin"
    calls: list[tuple[int, int]] = []

    def _on_progress(downloaded: int, total: int) -> None:
        calls.append((downloaded, total))

    async def _run() -> None:
        async with _mock_client(_handler) as client:
            await download_asset("https://fake/file", dest, client, _on_progress)

    asyncio.run(_run())

    assert calls, "on_progress was never invoked"
    last_downloaded, last_total = calls[-1]
    assert last_downloaded == len(payload)
    assert last_total == len(payload)


# ---------------------------------------------------------------------------
# resolve_latest_asset_url — malformed responses and remaining selection arms
# ---------------------------------------------------------------------------

def test_resolve_latest_asset_url_raises_on_invalid_json() -> None:
    """A 200 response whose body is not JSON must become InvalidRepoReferenceError."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    async def _run() -> None:
        async with _mock_client(_handler) as client:
            await resolve_latest_asset_url("owner/repo", None, client)

    with pytest.raises(InvalidRepoReferenceError):
        asyncio.run(_run())


def test_resolve_latest_asset_url_raises_on_non_dict_payload() -> None:
    """A JSON array (not an object) is an unexpected shape → InvalidRepoReferenceError."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected", "list"])

    async def _run() -> None:
        async with _mock_client(_handler) as client:
            await resolve_latest_asset_url("owner/repo", None, client)

    with pytest.raises(InvalidRepoReferenceError):
        asyncio.run(_run())


def test_resolve_latest_asset_url_raises_for_status_on_server_error() -> None:
    """A 5xx that is neither 403 nor 404 must propagate via raise_for_status()."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "internal error"})

    async def _run() -> None:
        async with _mock_client(_handler) as client:
            await resolve_latest_asset_url("owner/repo", None, client)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_run())


def test_resolve_latest_asset_url_falls_back_to_first_asset_and_defaults() -> None:
    """
    With no keyword match and no .zip asset, the first asset wins. Missing
    tag_name / target_commitish must default to 'unknown' and '' respectively.
    """
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "assets": [
                {"name": "plugin.tar.gz",
                 "browser_download_url": "https://fake/plugin.tar.gz"},
                {"name": "installer.exe",
                 "browser_download_url": "https://fake/installer.exe"},
            ],
        })

    async def _run() -> tuple[str, str, str]:
        async with _mock_client(_handler) as client:
            return await resolve_latest_asset_url("owner/repo", None, client)

    url, version, commit_ref = asyncio.run(_run())
    assert url == "https://fake/plugin.tar.gz"
    assert version == "unknown"
    assert commit_ref == ""


def test_resolve_latest_asset_url_keyword_miss_falls_back_to_zip() -> None:
    """A keyword that matches no asset must fall through to the first .zip file."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "tag_name": "v3.1.0",
            "target_commitish": "release",
            "assets": [
                {"name": "plugin.tar.gz",
                 "browser_download_url": "https://fake/plugin.tar.gz"},
                {"name": "plugin.zip",
                 "browser_download_url": "https://fake/plugin.zip"},
            ],
        })

    async def _run() -> str:
        async with _mock_client(_handler) as client:
            url, _, _ = await resolve_latest_asset_url(
                "owner/repo", "no-such-keyword", client
            )
            return url

    assert asyncio.run(_run()) == "https://fake/plugin.zip"


# ---------------------------------------------------------------------------
# download_asset — streaming edge cases
# ---------------------------------------------------------------------------

def test_download_asset_reports_negative_total_when_content_length_absent(
    tmp_path: Path,
) -> None:
    """
    When the server streams without a Content-Length header, total must be
    reported as -1 while the full body is still written to disk.
    """
    chunks = [b"alpha", b"beta", b"gamma"]

    async def _stream() -> Any:
        for chunk in chunks:
            yield chunk

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_stream())

    dest = tmp_path / "stream.bin"
    calls: list[tuple[int, int]] = []

    def _on_progress(downloaded: int, total: int) -> None:
        calls.append((downloaded, total))

    async def _run() -> None:
        async with _mock_client(_handler) as client:
            await download_asset("https://fake/file", dest, client, _on_progress)

    asyncio.run(_run())

    assert dest.read_bytes() == b"".join(chunks)
    assert calls, "on_progress was never invoked"
    assert calls[-1][1] == -1, "total must be -1 when Content-Length is absent"


def test_download_asset_cleans_up_and_raises_on_write_error(tmp_path: Path) -> None:
    """
    A write failure (here: a destination whose parent directory does not exist)
    must raise OSError and leave no partial file behind.
    """
    payload = b"payload"

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"content-length": str(len(payload))},
        )

    dest = tmp_path / "missing_dir" / "file.bin"

    async def _run() -> None:
        async with _mock_client(_handler) as client:
            await download_asset("https://fake/file", dest, client)

    with pytest.raises(OSError):
        asyncio.run(_run())

    assert not dest.exists()


def test_download_asset_swallows_progress_callback_errors(tmp_path: Path) -> None:
    """A raising on_progress callback must not abort the download."""
    payload = b"z" * 4096

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"content-length": str(len(payload))},
        )

    dest = tmp_path / "download.bin"

    def _on_progress(downloaded: int, total: int) -> None:
        raise RuntimeError("callback blew up")

    async def _run() -> None:
        async with _mock_client(_handler) as client:
            await download_asset("https://fake/file", dest, client, _on_progress)

    asyncio.run(_run())

    assert dest.read_bytes() == payload


# ---------------------------------------------------------------------------
# install_mod — full success pipeline and remaining error boundaries
# ---------------------------------------------------------------------------

def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    source_zip: Path,
    version: str = "v1.0.0",
    commit_ref: str = "main",
) -> None:
    """Route resolve + download to a prebuilt local ZIP, keeping install offline."""
    async def _fake_resolve(*_: Any, **__: Any) -> tuple[str, str, str]:
        return "https://fake.local/asset.zip", version, commit_ref

    async def _fake_download(
        _url: str,
        dest_path: Path,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        shutil.copyfile(source_zip, dest_path)

    monkeypatch.setattr(mod_manager, "resolve_latest_asset_url", _fake_resolve)
    monkeypatch.setattr(mod_manager, "download_asset", _fake_download)


def test_install_mod_direct_layout_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DIRECT (addons/ at root) archive must extract as-is and report DIRECT."""
    source_zip = tmp_path / "direct.zip"
    with zipfile.ZipFile(source_zip, "w") as zf:
        zf.writestr("addons/metamod/metaplugins.ini", b"config")
        zf.writestr("addons/counterstrikesharp/plugin.dll", b"dll-bytes")

    _patch_pipeline(monkeypatch, source_zip, version="v2.0.0", commit_ref="abc123")
    target = tmp_path / "server"

    async def _invoke() -> Any:
        return await install_mod(
            repo="owner/repo",
            target_dir=target,
            http_client=object(),  # type: ignore[arg-type]
        )

    result = asyncio.run(_invoke())

    assert result.version == "v2.0.0"
    assert result.commit_ref == "abc123"
    assert result.zip_case is ZipCase.DIRECT
    assert result.files_written, "No files were reported as written"
    assert (target / "addons" / "counterstrikesharp" / "plugin.dll").exists()


def test_install_mod_wrapper_flatten_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single wrapper directory must be stripped during extraction."""
    source_zip = tmp_path / "wrapped.zip"
    with zipfile.ZipFile(source_zip, "w") as zf:
        zf.writestr("MyPlugin-v1.0/plugin.dll", b"dll")
        zf.writestr("MyPlugin-v1.0/config/settings.json", b"{}")

    _patch_pipeline(monkeypatch, source_zip)
    target = tmp_path / "server"

    async def _invoke() -> Any:
        return await install_mod(
            repo="owner/repo",
            target_dir=target,
            http_client=object(),  # type: ignore[arg-type]
        )

    result = asyncio.run(_invoke())

    assert result.zip_case is ZipCase.WRAPPER_FLATTEN
    assert (target / "plugin.dll").exists()
    assert (target / "config" / "settings.json").exists()
    assert not (target / "MyPlugin-v1.0").exists(), "Wrapper prefix was not stripped"


def test_install_mod_emits_download_and_extract_progress_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    install_mod must translate byte-level progress into a 'Downloading'
    ProgressEvent and emit a final 'Extracting' event at 100%.
    """
    source_zip = tmp_path / "direct.zip"
    with zipfile.ZipFile(source_zip, "w") as zf:
        zf.writestr("addons/plugin.dll", b"dll")

    async def _fake_resolve(*_: Any, **__: Any) -> tuple[str, str, str]:
        return "https://fake.local/asset.zip", "v1.0.0", "main"

    async def _fake_download(
        _url: str,
        dest_path: Path,
        _client: Any,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        shutil.copyfile(source_zip, dest_path)
        if on_progress:
            on_progress(50, 100)

    monkeypatch.setattr(mod_manager, "resolve_latest_asset_url", _fake_resolve)
    monkeypatch.setattr(mod_manager, "download_asset", _fake_download)

    events: list[Any] = []

    def _on_progress(event: Any) -> None:
        events.append(event)

    async def _invoke() -> None:
        await install_mod(
            repo="owner/repo",
            target_dir=tmp_path / "server",
            http_client=object(),  # type: ignore[arg-type]
            on_progress=_on_progress,
        )

    asyncio.run(_invoke())

    reported = [(e.message, e.percent) for e in events]
    assert ("Downloading", 50.0) in reported
    assert ("Extracting", 100.0) in reported


def test_install_mod_raises_on_bad_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A downloaded asset that is not a valid ZIP must raise the public error."""
    async def _fake_resolve(*_: Any, **__: Any) -> tuple[str, str, str]:
        return "https://fake.local/asset.zip", "v1.0.0", "main"

    async def _fake_download(
        _url: str,
        dest_path: Path,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        dest_path.write_bytes(b"this is definitely not a zip archive")

    monkeypatch.setattr(mod_manager, "resolve_latest_asset_url", _fake_resolve)
    monkeypatch.setattr(mod_manager, "download_asset", _fake_download)

    async def _invoke() -> None:
        await install_mod(
            repo="owner/repo",
            target_dir=tmp_path / "server",
            http_client=object(),  # type: ignore[arg-type]
        )

    with pytest.raises(UnrecognizedZipStructureError):
        asyncio.run(_invoke())


def test_install_mod_builds_and_closes_client_when_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When no http_client is supplied, install_mod must build one and close it
    on exit — verified via a fake client that records its aclose() call.
    """
    source_zip = tmp_path / "direct.zip"
    with zipfile.ZipFile(source_zip, "w") as zf:
        zf.writestr("addons/plugin.dll", b"dll")

    class _FakeClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    fake_client = _FakeClient()
    monkeypatch.setattr(mod_manager, "build_async_client", lambda: fake_client)
    _patch_pipeline(monkeypatch, source_zip)

    async def _invoke() -> Any:
        return await install_mod(repo="owner/repo", target_dir=tmp_path / "server")

    result = asyncio.run(_invoke())

    assert result.zip_case is ZipCase.DIRECT
    assert fake_client.closed is True, "Auto-created client was not closed"


# ---------------------------------------------------------------------------
# install_mod — DIRECT-layout routing to csgo_dir (regression for the
# plugins/<repo>/addons/ double-nesting bug)
# ---------------------------------------------------------------------------

def test_install_mod_direct_layout_routes_to_direct_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A plugin release shipping addons/ at the archive root must land in
    csgo_dir — nesting it under plugins/<repo>/addons/ makes the engine
    silently ignore the plugin.
    """
    source_zip = tmp_path / "direct_plugin.zip"
    with zipfile.ZipFile(source_zip, "w") as zf:
        zf.writestr("addons/counterstrikesharp/plugins/MyPlugin/MyPlugin.dll", b"dll")

    _patch_pipeline(monkeypatch, source_zip)
    csgo_dir = tmp_path / "csgo"
    plugin_target = tmp_path / "plugins" / "repo"

    async def _invoke() -> Any:
        return await install_mod(
            repo="owner/repo",
            target_dir=plugin_target,
            http_client=object(),  # type: ignore[arg-type]
            direct_target_dir=csgo_dir,
        )

    result = asyncio.run(_invoke())

    routed = csgo_dir / "addons" / "counterstrikesharp" / "plugins" / "MyPlugin" / "MyPlugin.dll"
    assert routed.exists(), "DIRECT content was not redirected to csgo_dir"
    assert not (plugin_target / "addons").exists(), (
        "DIRECT content was double-nested under the per-plugin target"
    )
    assert result.target_dir == str(csgo_dir)


def test_install_mod_wrapped_addons_layout_routes_to_direct_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single wrapper folder containing addons/ must flatten into csgo_dir."""
    source_zip = tmp_path / "wrapped_direct.zip"
    with zipfile.ZipFile(source_zip, "w") as zf:
        zf.writestr(
            "MyPlugin-v2.0/addons/counterstrikesharp/plugins/MyPlugin/MyPlugin.dll",
            b"dll",
        )

    _patch_pipeline(monkeypatch, source_zip)
    csgo_dir = tmp_path / "csgo"
    plugin_target = tmp_path / "plugins" / "repo"

    async def _invoke() -> Any:
        return await install_mod(
            repo="owner/repo",
            target_dir=plugin_target,
            http_client=object(),  # type: ignore[arg-type]
            direct_target_dir=csgo_dir,
        )

    result = asyncio.run(_invoke())

    routed = csgo_dir / "addons" / "counterstrikesharp" / "plugins" / "MyPlugin" / "MyPlugin.dll"
    assert routed.exists(), "Wrapped addons/ content was not redirected to csgo_dir"
    assert result.zip_case is ZipCase.WRAPPER_FLATTEN


def test_install_mod_flat_dll_stays_in_plugin_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare-DLL archives belong in the per-plugin folder, never in csgo_dir."""
    source_zip = tmp_path / "flat.zip"
    with zipfile.ZipFile(source_zip, "w") as zf:
        zf.writestr("MyPlugin.dll", b"dll")

    _patch_pipeline(monkeypatch, source_zip)
    csgo_dir = tmp_path / "csgo"
    plugin_target = tmp_path / "plugins" / "repo"

    async def _invoke() -> Any:
        return await install_mod(
            repo="owner/repo",
            target_dir=plugin_target,
            http_client=object(),  # type: ignore[arg-type]
            direct_target_dir=csgo_dir,
        )

    result = asyncio.run(_invoke())

    assert (plugin_target / "MyPlugin.dll").exists()
    assert not csgo_dir.exists(), "FLAT_DLL content leaked into csgo_dir"
    assert result.zip_case is ZipCase.FLAT_DLL


def test_install_mod_without_direct_target_keeps_legacy_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers that never pass direct_target_dir see the original extraction path."""
    source_zip = tmp_path / "direct.zip"
    with zipfile.ZipFile(source_zip, "w") as zf:
        zf.writestr("addons/metamod/metaplugins.ini", b"config")

    _patch_pipeline(monkeypatch, source_zip)
    target = tmp_path / "target"

    async def _invoke() -> Any:
        return await install_mod(
            repo="owner/repo",
            target_dir=target,
            http_client=object(),  # type: ignore[arg-type]
        )

    result = asyncio.run(_invoke())

    assert (target / "addons" / "metamod" / "metaplugins.ini").exists()
    assert result.target_dir == str(target)
