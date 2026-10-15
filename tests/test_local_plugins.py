"""Offline tests for local plugin installs and Windows-aware asset selection."""
from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import httpx
import pytest

from cli.app import validate_plugin_input
from core.mod_manager import (
    InvalidRepoReferenceError,
    install_local,
    resolve_latest_asset_url,
)
from models.schemas import ZipCase


# ---------------------------------------------------------------------------
# Windows-aware asset pick
# ---------------------------------------------------------------------------

def _resolve(asset_names: list[str]) -> str:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "tag_name": "v1",
            "assets": [
                {"name": n, "browser_download_url": f"https://fake/{n}"} for n in asset_names
            ],
        })

    async def _run() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            url, _, _ = await resolve_latest_asset_url("owner/repo", None, client)
            return url

    return asyncio.run(_run())


def test_windows_zip_beats_linux_zip_listed_first() -> None:
    url = _resolve(["BotHider-Linux-0.5.1.zip", "BotHider-Windows-0.5.1.zip"])
    assert url.endswith("BotHider-Windows-0.5.1.zip")


def test_os_neutral_zip_beats_linux_zip() -> None:
    assert _resolve(["plugin-linux.zip", "plugin.zip"]).endswith("/plugin.zip")


def test_win64_suffix_counts_as_windows() -> None:
    assert _resolve(["plugin.zip", "plugin-win64.zip"]).endswith("plugin-win64.zip")


def test_darwin_is_not_mistaken_for_windows() -> None:
    assert _resolve(["plugin-darwin.zip", "plugin-windows.zip"]).endswith("plugin-windows.zip")


def test_single_linux_zip_is_still_used_as_last_resort() -> None:
    assert _resolve(["plugin-linux.zip"]).endswith("plugin-linux.zip")


# ---------------------------------------------------------------------------
# install_local
# ---------------------------------------------------------------------------

def _write_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_install_local_zip_with_flat_dll_lands_in_plugin_dir(tmp_path: Path) -> None:
    src = _write_zip(tmp_path / "MyPlugin.zip", {"MyPlugin.dll": b"dll", "MyPlugin.deps.json": b"{}"})
    target = tmp_path / "csgo" / "addons" / "counterstrikesharp" / "plugins" / "MyPlugin"
    result = install_local(src, target_dir=target, direct_target_dir=tmp_path / "csgo")
    assert (target / "MyPlugin.dll").is_file()
    assert result.version == "local"
    assert result.zip_case == ZipCase.FLAT_DLL


def test_install_local_folder_with_addons_layout_routes_to_csgo(tmp_path: Path) -> None:
    src = tmp_path / "AutoRestart"
    dll = src / "addons" / "counterstrikesharp" / "plugins" / "AutoRestart" / "AutoRestart.dll"
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b"dll")
    csgo = tmp_path / "csgo"
    target = csgo / "addons" / "counterstrikesharp" / "plugins" / "AutoRestart"

    result = install_local(src, target_dir=target, direct_target_dir=csgo)

    assert (target / "AutoRestart.dll").is_file()
    assert Path(result.target_dir) == csgo
    assert not (target / "addons").exists(), "DIRECT layout must not nest addons/ inside plugins/"


def test_install_local_folder_and_zip_produce_identical_trees(tmp_path: Path) -> None:
    entries = {"Wrapper/Foo.dll": b"a", "Wrapper/lang/en.json": b"{}"}
    zip_src = _write_zip(tmp_path / "Foo.zip", entries)
    folder_src = tmp_path / "FooFolder"
    for name, data in entries.items():
        (folder_src / name).parent.mkdir(parents=True, exist_ok=True)
        (folder_src / name).write_bytes(data)

    a, b = tmp_path / "a", tmp_path / "b"
    install_local(zip_src, target_dir=a)
    install_local(folder_src, target_dir=b)

    def _tree(root: Path) -> set[str]:
        return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}

    assert _tree(a) == _tree(b) == {"Foo.dll", "lang/en.json"}


def test_install_local_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(InvalidRepoReferenceError):
        install_local(tmp_path / "nope", target_dir=tmp_path / "t")


def test_install_local_rejects_non_zip_file(tmp_path: Path) -> None:
    f = tmp_path / "plugin.dll"
    f.write_bytes(b"MZ")
    with pytest.raises(InvalidRepoReferenceError):
        install_local(f, target_dir=tmp_path / "t")


# ---------------------------------------------------------------------------
# validate_plugin_input
# ---------------------------------------------------------------------------

def test_validate_input_existing_folder_is_local(tmp_path: Path) -> None:
    folder = tmp_path / "CoolPlugin"
    folder.mkdir()
    ref = validate_plugin_input(f'"{folder}"')  # drag & drop adds quotes
    assert ref is not None and ref.local_path == str(folder.resolve())
    assert ref.repo == "CoolPlugin"
    assert ref.full_ref == "local:CoolPlugin"


def test_validate_input_zip_is_local_with_stem_name(tmp_path: Path) -> None:
    z = _write_zip(tmp_path / "Thing-1.2.zip", {"Thing.dll": b""})
    ref = validate_plugin_input(str(z))
    assert ref is not None and ref.repo == "Thing-1.2" and ref.local_path


def test_validate_input_github_url_and_slug() -> None:
    ref = validate_plugin_input("https://github.com/ed0ard/CS2-Bot-Randomizer")
    assert ref is not None and ref.full_ref == "ed0ard/CS2-Bot-Randomizer" and not ref.local_path
    assert validate_plugin_input("XBribo/CS2-Bot-Hider").full_ref == "XBribo/CS2-Bot-Hider"


@pytest.mark.parametrize("raw", ["", "   ", "not a repo at all"])
def test_validate_input_rejects_junk(raw: str) -> None:
    assert validate_plugin_input(raw) is None


def test_validate_input_rejects_local_non_zip_file(tmp_path: Path) -> None:
    f = tmp_path / "readme.txt"
    f.write_text("x")
    assert validate_plugin_input(str(f)) is None


# ---------------------------------------------------------------------------
# align_plugin_folder — CSSharp loads plugins/<Name>/<Name>.dll only
# ---------------------------------------------------------------------------

def test_install_renames_folder_to_assembly_name(tmp_path: Path) -> None:
    """Repo 'CS2-Bot-Randomizer' ships BotRandomizer.dll — folder must follow the DLL."""
    src = _write_zip(tmp_path / "BotRandomizer.zip", {
        "BotRandomizer.dll": b"dll", "BotRandomizer.deps.json": b"{}", "catalog.json": b"{}",
    })
    plugins = tmp_path / "plugins"
    result = install_local(src, target_dir=plugins / "CS2-Bot-Randomizer")
    assert (plugins / "BotRandomizer" / "BotRandomizer.dll").is_file()
    assert (plugins / "BotRandomizer" / "catalog.json").is_file()
    assert not (plugins / "CS2-Bot-Randomizer").exists()
    assert Path(result.target_dir) == plugins / "BotRandomizer"


def test_align_merges_into_existing_folder_keeping_configs(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    existing = plugins / "Foo"
    existing.mkdir(parents=True)
    (existing / "Foo.dll").write_bytes(b"old")
    (existing / "user_config.json").write_text("{}")
    src = _write_zip(tmp_path / "Foo.zip", {"Foo.dll": b"new", "Foo.deps.json": b"{}"})

    install_local(src, target_dir=plugins / "foo-plugin")

    assert (existing / "Foo.dll").read_bytes() == b"new"
    assert (existing / "user_config.json").is_file()
    assert not (plugins / "foo-plugin").exists()


def test_align_leaves_ambiguous_layout_alone(tmp_path: Path) -> None:
    src = _write_zip(tmp_path / "multi.zip", {
        "A.dll": b"", "A.deps.json": b"{}", "B.dll": b"", "B.deps.json": b"{}",
    })
    plugins = tmp_path / "plugins"
    install_local(src, target_dir=plugins / "repo-name")
    assert (plugins / "repo-name" / "A.dll").is_file()


def _resolve_repo(repo: str, asset_names: list[str]) -> str:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "tag_name": "v1",
            "assets": [{"name": n, "browser_download_url": f"https://fake/{n}"} for n in asset_names],
        })

    async def _run() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            url, _, _ = await resolve_latest_asset_url(repo, None, client)
            return url

    return asyncio.run(_run())


def test_plugin_zip_beats_website_zip_listed_first() -> None:
    """Nereziel/cs2-WeaponPaints ships WeaponPaints-Website.zip before WeaponPaints.zip."""
    url = _resolve_repo("Nereziel/cs2-WeaponPaints", ["WeaponPaints-Website.zip", "WeaponPaints.zip"])
    assert url.endswith("/WeaponPaints.zip")


@pytest.mark.parametrize("side", ["Plugin-src.zip", "Plugin-source.zip", "Plugin-docs.zip", "Plugin-debug.zip"])
def test_side_archives_lose_to_plugin_build(side: str) -> None:
    assert _resolve_repo("owner/Plugin", [side, "Plugin.zip"]).endswith("/Plugin.zip")


def test_repo_name_match_breaks_ties() -> None:
    url = _resolve_repo("owner/CS2-SimpleAdmin", ["Extras.zip", "CS2-SimpleAdmin-1.9.0.zip"])
    assert url.endswith("CS2-SimpleAdmin-1.9.0.zip")
