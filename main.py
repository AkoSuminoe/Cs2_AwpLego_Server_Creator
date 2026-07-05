"""
main.py — Orchestrator.

Sequences the installation phases, wiring CLI progress callbacks to async core
functions. Contains no business logic of its own.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from cli.app import (
    InstallationProgress,
    collect_base_dir,
    collect_plugins,
    collect_server_config,
    console,
    show_banner,
    show_summary,
)
from core import config_patcher, mod_manager, snapshot, steamcmd_wrapper
from core.lock_manager import LockFileManager
from core.validator import (
    StateManager,
    is_cs2_installed,
    is_cssharp_installed,
    is_gameinfo_patched,
    is_metamod_installed,
    is_plugin_installed,
    is_steamcmd_installed,
)
from models.schemas import PluginLockEntry
from utils.http_client import build_async_client


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CS2 Automated Server Setup & Management")
    p.add_argument(
        "--restore",
        action="store_true",
        help="Reinstall all plugins from cs2-plugins.lock at their pinned versions",
    )
    return p.parse_args()


async def _restore_from_lock(
    plugins_dir: Path,
    lock_mgr: LockFileManager,
    client,
    prog: InstallationProgress,
) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    entries = lock_mgr.all_entries()

    if not entries:
        console.print("[yellow]cs2-plugins.lock is empty — nothing to restore.[/yellow]")
        return results

    for entry in entries:
        key = f"restore_{entry.repo}"
        label = f"Restore: {entry.repo} @ {entry.version}"
        prog.start_task(key, label)

        try:
            with tempfile.TemporaryDirectory() as tmp:
                zip_path = Path(tmp) / "asset.zip"

                def _on_chunk(dl: int, total: int, k: str = key) -> None:
                    if total > 0:
                        prog.update_task(k, (dl / total) * 100.0)

                await mod_manager.download_asset(
                    entry.download_url, zip_path, client, _on_chunk
                )

                with zipfile.ZipFile(zip_path) as zf:
                    namelist = zf.namelist()

                case, prefix = mod_manager._classify_zip(namelist)
                if case == mod_manager.ZipCase.AMBIGUOUS:
                    raise mod_manager.UnrecognizedZipStructureError(
                        f"Cannot determine ZIP layout for '{entry.owner}/{entry.repo}'."
                    )

                target_dir = plugins_dir / entry.repo
                mod_manager._extract_zip(zip_path, target_dir, case, prefix)

            prog.complete_task(key)
            results.append((label, True, entry.version))
        except Exception as exc:
            prog.fail_task(key, str(exc))
            results.append((label, False, str(exc)))

    return results


async def main() -> None:
    try:
        args = _parse_args()
    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"[red]Failed to parse arguments: {exc}[/red]")
        return

    show_banner()

    # Phase 0 — collect all user input before any async work begins
    try:
        base_dir = collect_base_dir()
        server_config = collect_server_config()
        plugins = collect_plugins()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Setup cancelled by user.[/yellow]")
        return

    # Phase 1 — resolve canonical paths
    steamcmd_dir = base_dir / "steamcmd"
    server_dir = base_dir / "cs2_server"
    csgo_dir = server_dir / "game" / "csgo"
    plugins_dir = csgo_dir / "addons" / "counterstrikesharp" / "plugins"
    snapshot_dir = base_dir / ".snapshots"

    state = StateManager(base_dir / "install_state.json")
    lock_mgr = LockFileManager(base_dir / "cs2-plugins.lock")
    results: list[tuple[str, bool, str]] = []

    async with build_async_client() as client:
        with InstallationProgress() as prog:

            # ------------------------------------------------------------------
            # --restore mode: skip installer, reinstall pinned plugins and exit
            # ------------------------------------------------------------------
            if args.restore:
                results = await _restore_from_lock(plugins_dir, lock_mgr, client, prog)
                show_summary(results)
                return

            # ------------------------------------------------------------------
            # Phase 2: SteamCMD
            # ------------------------------------------------------------------
            prog.start_task("steamcmd", "Download SteamCMD")
            if is_steamcmd_installed(steamcmd_dir):
                prog.complete_task("steamcmd")
                results.append(("SteamCMD", True, "Already installed — skipped"))
            else:
                try:
                    await steamcmd_wrapper.download_steamcmd(steamcmd_dir)
                    prog.complete_task("steamcmd")
                    state.mark_complete("steamcmd_downloaded")
                    results.append(("SteamCMD", True, "Downloaded"))
                except Exception as exc:
                    prog.fail_task("steamcmd", str(exc))
                    results.append(("SteamCMD", False, str(exc)))
                    show_summary(results)
                    return

            # ------------------------------------------------------------------
            # Phase 3: CS2 server install (async generator → live progress bar)
            # ------------------------------------------------------------------
            prog.start_task("cs2", "Install CS2 Server", total=100.0)
            if is_cs2_installed(server_dir):
                prog.complete_task("cs2")
                results.append(("CS2 Server", True, "Already installed — skipped"))
            else:
                try:
                    steamcmd_exe = steamcmd_dir / "steamcmd.exe"
                    async for event in steamcmd_wrapper.install_cs2(steamcmd_exe, server_dir):
                        prog.update_task("cs2", event.percent)
                    prog.complete_task("cs2")
                    state.mark_complete("cs2_installed")
                    results.append(("CS2 Server", True, "Installed"))
                except steamcmd_wrapper.SteamCMDInstallError as exc:
                    prog.fail_task("cs2", str(exc))
                    results.append(("CS2 Server", False, str(exc)))
                    show_summary(results)
                    return

            # ------------------------------------------------------------------
            # Phase 4: Metamod
            # ------------------------------------------------------------------
            prog.start_task("metamod", "Install Metamod")
            if is_metamod_installed(csgo_dir):
                prog.complete_task("metamod")
                results.append(("Metamod", True, "Already installed — skipped"))
            else:
                try:
                    result = await mod_manager.install_mod(
                        repo="alliedmodders/metamod-source",
                        target_dir=csgo_dir,
                        asset_keyword="windows",
                        http_client=client,
                        on_progress=lambda e: prog.update_task("metamod", e.percent),
                    )
                    prog.complete_task("metamod")
                    state.mark_complete("metamod_installed", {"version": result.version})
                    results.append(("Metamod", True, result.version))
                except Exception as exc:
                    prog.fail_task("metamod", str(exc))
                    results.append(("Metamod", False, str(exc)))

            # ------------------------------------------------------------------
            # Phase 5: CounterStrikeSharp
            # ------------------------------------------------------------------
            prog.start_task("cssharp", "Install CounterStrikeSharp")
            if is_cssharp_installed(csgo_dir):
                prog.complete_task("cssharp")
                results.append(("CounterStrikeSharp", True, "Already installed — skipped"))
            else:
                try:
                    result = await mod_manager.install_mod(
                        repo="roflmuffin/CounterStrikeSharp",
                        target_dir=csgo_dir,
                        asset_keyword="with-runtime-windows",
                        http_client=client,
                        on_progress=lambda e: prog.update_task("cssharp", e.percent),
                    )
                    prog.complete_task("cssharp")
                    state.mark_complete("cssharp_installed", {"version": result.version})
                    results.append(("CounterStrikeSharp", True, result.version))
                except Exception as exc:
                    prog.fail_task("cssharp", str(exc))
                    results.append(("CounterStrikeSharp", False, str(exc)))

            # ------------------------------------------------------------------
            # Phase 6: gameinfo.gi patch
            # ------------------------------------------------------------------
            prog.start_task("gameinfo", "Patch gameinfo.gi")
            if is_gameinfo_patched(csgo_dir):
                prog.complete_task("gameinfo")
                results.append(("gameinfo.gi Patch", True, "Already patched — skipped"))
            else:
                try:
                    config_patcher.patch_gameinfo(csgo_dir)
                    prog.complete_task("gameinfo")
                    state.mark_complete("gameinfo_patched")
                    results.append(("gameinfo.gi Patch", True, "Patched"))
                except Exception as exc:
                    prog.fail_task("gameinfo", str(exc))
                    results.append(("gameinfo.gi Patch", False, str(exc)))

            # ------------------------------------------------------------------
            # Phase 7: start_server.bat + server.cfg (always overwrite — idempotent)
            # ------------------------------------------------------------------
            prog.start_task("configs", "Write server configs")
            try:
                config_patcher.write_server_configs(
                    base_dir=base_dir,
                    server_dir=server_dir,
                    config=server_config,
                    cfg_template_path=Path(__file__).parent / "server.cfg",
                )
                prog.complete_task("configs")
                results.append(("Server Configs", True, "start_server.bat + server.cfg written"))
            except Exception as exc:
                prog.fail_task("configs", str(exc))
                results.append(("Server Configs", False, str(exc)))

            # ------------------------------------------------------------------
            # Phase 7b: CSSharp databases.json (skipped when DB not configured)
            # ------------------------------------------------------------------
            if server_config.db_config and server_config.db_config.enabled:
                try:
                    config_patcher.write_databases_json(csgo_dir, server_config.db_config)
                    console.print(
                        "[green]  [+] CSSharp databases.json configured successfully "
                        "for plugin SQL binding![/green]"
                    )
                    results.append(("CSSharp Database Config", True, "databases.json written"))
                except Exception as exc:
                    console.print(f"[red]  [-] Failed to write databases.json: {exc}[/red]")
                    results.append(("CSSharp Database Config", False, str(exc)))

            # ------------------------------------------------------------------
            # Phase 8: user-defined plugins
            # ------------------------------------------------------------------
            for plugin in plugins:
                key = f"plugin_{plugin.repo}"
                prog.start_task(key, f"Plugin: {plugin.display_name}")

                if is_plugin_installed(plugins_dir, plugin.repo):
                    prog.complete_task(key)
                    results.append(
                        (f"Plugin: {plugin.display_name}", True, "Already installed — skipped")
                    )
                    continue

                snap: Optional[snapshot.SnapshotMeta] = None
                try:
                    snap = snapshot.take_snapshot(
                        csgo_dir, snapshot_dir, label=f"before_{plugin.repo}"
                    )
                    snapshot.cleanup_old_snapshots(snapshot_dir)
                except snapshot.SnapshotError:
                    pass

                try:
                    result = await mod_manager.install_mod(
                        repo=plugin.full_ref,
                        target_dir=plugins_dir / plugin.repo,
                        http_client=client,
                        on_progress=lambda e, k=key: prog.update_task(k, e.percent),
                    )
                    prog.complete_task(key)
                    state.mark_complete(
                        f"plugin:{plugin.full_ref}", {"version": result.version}
                    )
                    lock_mgr.record(
                        PluginLockEntry(
                            owner=plugin.owner,
                            repo=plugin.repo,
                            version=result.version,
                            commit_ref=result.commit_ref,
                            download_url=result.download_url,
                            asset_keyword=None,
                            installed_at=datetime.datetime.utcnow().isoformat() + "Z",
                        )
                    )
                    results.append(
                        (f"Plugin: {plugin.display_name}", True, result.version)
                    )
                except Exception as exc:
                    if snap:
                        try:
                            snapshot.rollback(snap, csgo_dir)
                            console.print(
                                f"[yellow]  Rolled back to snapshot {snap.snapshot_id}[/yellow]"
                            )
                        except snapshot.SnapshotError as rb_exc:
                            console.print(f"[red]  Rollback failed: {rb_exc}[/red]")
                    prog.fail_task(key, str(exc))
                    results.append((f"Plugin: {plugin.display_name}", False, str(exc)))

    # Phase 9 — summary
    show_summary(results)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Exiting.[/yellow]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"\n[red]Fatal error: {exc}[/red]")
        sys.exit(1)
