"""
main.py — Orchestrator.

Sequences the installation phases, wiring CLI progress callbacks to async core
functions. Contains no business logic of its own.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from cli.app import (
    InstallationProgress,
    collect_base_dir,
    collect_plugins,
    collect_runtime_verify,
    collect_server_config,
    console,
    show_banner,
    show_check_report,
    show_summary,
    validate_plugin_input,
)
from core import config_patcher, health_check, mod_manager, server_controller, snapshot, steamcmd_wrapper
from core.lock_manager import LockFileManager
from core.validator import (
    StateManager,
    is_cs2_installed,
    has_stale_metamod,
    is_cssharp_installed,
    is_gameinfo_patched,
    is_metamod_installed,
    is_plugin_installed,
    is_steamcmd_installed,
)
from models.schemas import PluginLockEntry, PluginRef, ServerConfig
from utils.http_client import build_async_client


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CS2 Automated Server Setup & Management")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--restore",
        action="store_true",
        help="Reinstall all plugins from cs2-plugins.lock at their pinned versions",
    )
    mode.add_argument(
        "--update",
        action="store_true",
        help=(
            "Update an existing CS2 server via SteamCMD and re-apply the "
            "gameinfo.gi Metamod patch that Steam updates overwrite"
        ),
    )
    mode.add_argument(
        "--add-plugin",
        action="append",
        metavar="SOURCE",
        help=(
            "Install a plugin into an existing server: owner/repo, a GitHub URL, "
            "or a local folder/.zip (repeatable)"
        ),
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Inspect an existing install without changing anything",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="With --check: boot the server, confirm Metamod + CounterStrikeSharp load, then shut down",
    )
    p.add_argument(
        "--dir",
        metavar="PATH",
        help="Installation directory (skips the directory prompt)",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Launch the server after each plugin install and scan logs for "
            "runtime errors (auto-rollback on confirmed failure)"
        ),
    )
    args = p.parse_args()
    if args.live and not args.check:
        p.error("--live requires --check")
    return args


def _resolve_base_dir(args: argparse.Namespace, create: bool = True) -> Path:
    if args.dir:
        path = Path(args.dir).expanduser().resolve()
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path
    return collect_base_dir()


async def _run_check(base_dir: Path, live: bool) -> bool:
    """Runs the read-only health check; returns True when nothing failed."""
    if not base_dir.is_dir():
        console.print(f"[red]Directory not found: {base_dir}[/red]")
        return False
    items = health_check.run_static_checks(base_dir)
    show_check_report(items, f"Health Check — {base_dir}")
    ok = not health_check.has_failures(items)
    if not live:
        return ok

    server_dir = base_dir / "cs2_server"
    config = health_check.parse_start_bat(server_dir / "game" / "start_server.bat")
    if not ok:
        console.print("[yellow]Skipping live boot: fix the failed checks first.[/yellow]")
        return False
    if config is None:
        console.print("[red]Live boot needs start_server.bat — run the installer first.[/red]")
        return False

    with console.status("[cyan]Booting CS2 server for live check (up to ~3 min)…[/cyan]"):
        live_items = await health_check.run_live_check(server_dir, config)
    show_check_report(live_items, "Live Check")
    return not health_check.has_failures(live_items)


async def _run_add_plugins(base_dir: Path, sources: list[str], verify: bool) -> bool:
    """Installs plugins into an existing server without the setup prompts."""
    server_dir = base_dir / "cs2_server"
    csgo_dir = server_dir / "game" / "csgo"
    if not is_cssharp_installed(csgo_dir):
        console.print(
            f"[red]CounterStrikeSharp not found under {csgo_dir} — "
            "run the installer first.[/red]"
        )
        return False

    refs = [validate_plugin_input(src) for src in sources]
    if any(ref is None for ref in refs):
        return False

    verify_config: Optional[ServerConfig] = None
    if verify:
        verify_config = health_check.parse_start_bat(server_dir / "game" / "start_server.bat")
        if verify_config is None:
            console.print("[yellow]No start_server.bat — skipping runtime verification.[/yellow]")

    state = StateManager(base_dir / "install_state.json")
    lock_mgr = LockFileManager(base_dir / "cs2-plugins.lock")
    results: list[tuple[str, bool, str]] = []
    async with build_async_client() as client:
        with InstallationProgress() as prog:
            for ref in refs:
                await _install_plugin(
                    ref,
                    server_dir=server_dir,
                    csgo_dir=csgo_dir,
                    plugins_dir=csgo_dir / "addons" / "counterstrikesharp" / "plugins",
                    snapshot_dir=base_dir / ".snapshots",
                    client=client,
                    prog=prog,
                    state=state,
                    lock_mgr=lock_mgr,
                    results=results,
                    verify_config=verify_config,
                )
    show_summary(results)
    return all(ok for _, ok, _ in results)


async def _ensure_metamod(
    csgo_dir: Path,
    client,
    prog: InstallationProgress,
    state: StateManager,
    results: list[tuple[str, bool, str]],
) -> bool:
    """
    Installs Metamod:Source 2.0, replacing a stale 1.x (Source 1) build if
    present. Returns True when a stale build was replaced — any existing
    CounterStrikeSharp was then built for a different SourceHook ABI and
    must be refreshed too.
    """
    prog.start_task("metamod", "Install Metamod")
    if is_metamod_installed(csgo_dir):
        prog.complete_task("metamod")
        results.append(("Metamod", True, "Already installed — skipped"))
        return False
    replaced = has_stale_metamod(csgo_dir)
    try:
        if replaced:
            prog.note_task("metamod", "Removing incompatible Metamod 1.x build")
            # Only the 1.x binaries go: addons/metamod/ also holds other
            # plugins' registrations (e.g. counterstrikesharp.vdf).
            addons = csgo_dir / "addons"
            shutil.rmtree(addons / "metamod" / "bin")
            for vdf in ("metamod.vdf", "metamod_x64.vdf"):
                (addons / vdf).unlink(missing_ok=True)
        result = await mod_manager.install_metamod_cs2(
            target_dir=csgo_dir,
            http_client=client,
            on_progress=lambda e: prog.update_task("metamod", e.percent),
        )
        prog.complete_task("metamod")
        state.mark_complete("metamod_installed", {"version": result.version})
        results.append(("Metamod", True, result.version))
    except Exception as exc:
        prog.fail_task("metamod", str(exc))
        results.append(("Metamod", False, str(exc)))
        return False
    return replaced


async def _ensure_cssharp(
    csgo_dir: Path,
    client,
    prog: InstallationProgress,
    state: StateManager,
    results: list[tuple[str, bool, str]],
    force: bool = False,
) -> None:
    """Installs the latest CounterStrikeSharp (with .NET runtime); force refreshes it in place."""
    prog.start_task("cssharp", "Install CounterStrikeSharp")
    if is_cssharp_installed(csgo_dir) and not force:
        prog.complete_task("cssharp")
        results.append(("CounterStrikeSharp", True, "Already installed — skipped"))
        return
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
        detail = f"{result.version} (refreshed for Metamod 2.0)" if force else result.version
        results.append(("CounterStrikeSharp", True, detail))
    except Exception as exc:
        prog.fail_task("cssharp", str(exc))
        results.append(("CounterStrikeSharp", False, str(exc)))


async def _install_plugin(
    plugin: PluginRef,
    *,
    server_dir: Path,
    csgo_dir: Path,
    plugins_dir: Path,
    snapshot_dir: Path,
    client,
    prog: InstallationProgress,
    state: StateManager,
    lock_mgr: LockFileManager,
    results: list[tuple[str, bool, str]],
    verify_config: Optional[ServerConfig] = None,
) -> None:
    """
    Snapshot → install (GitHub release or local folder/zip) → optional
    runtime verification with rollback → lock entry. Appends one result row.
    """
    key = f"plugin_{plugin.repo}"
    prog.start_task(key, f"Plugin: {plugin.display_name}")

    # Lock entry covers DIRECT-routed plugins whose files live
    # outside plugins/<repo>; the directory probe covers the rest.
    locked = lock_mgr.get(plugin.full_ref) is not None
    present = is_plugin_installed(plugins_dir, plugin.repo)
    if locked or present:
        prog.complete_task(key)
        detail = "Already installed — skipped"
        if locked and not present:
            # Lock says installed but no files under plugins/ —
            # either DIRECT-routed content or a manual delete.
            detail = (
                "In lock file — skipped (no files under plugins/; "
                "run --restore to reinstall)"
            )
        results.append((f"Plugin: {plugin.display_name}", True, detail))
        return

    snap: Optional[snapshot.SnapshotMeta] = None
    try:
        snap = snapshot.take_snapshot(
            csgo_dir, snapshot_dir, label=f"before_{plugin.repo}"
        )
        snapshot.cleanup_old_snapshots(snapshot_dir)
    except snapshot.SnapshotError:
        pass

    try:
        if plugin.local_path:
            result = mod_manager.install_local(
                Path(plugin.local_path),
                target_dir=plugins_dir / plugin.repo,
                direct_target_dir=csgo_dir,
            )
        else:
            result = await mod_manager.install_mod(
                repo=plugin.full_ref,
                target_dir=plugins_dir / plugin.repo,
                http_client=client,
                on_progress=lambda e, k=key: prog.update_task(k, e.percent),
                direct_target_dir=csgo_dir,
            )

        if verify_config is not None and is_cs2_installed(server_dir):
            if not verify_config.rcon_password:
                console.print(
                    "[yellow]  RCON password empty — skipping runtime verification.[/yellow]"
                )
            else:
                console.print(
                    f"[cyan]  Verifying {plugin.display_name} at runtime...[/cyan]"
                )
                result_v = await server_controller.verify_plugin_runtime(
                    server_dir, csgo_dir, verify_config, plugin.repo,
                )
                outcome = server_controller.classify_verification_outcome(result_v)
                if outcome == "rolled_back":
                    if snap:
                        try:
                            await server_controller.rollback_with_retry(snap, csgo_dir)
                            console.print(
                                f"[yellow]  Rolled back to snapshot {snap.snapshot_id}[/yellow]"
                            )
                        except snapshot.SnapshotError as rb_exc:
                            console.print(f"[red]  Rollback failed: {rb_exc}[/red]")
                    state.mark_failed(
                        f"plugin:{plugin.full_ref}",
                        "Runtime verification detected errors: "
                        + "; ".join(result_v.errors_detected[:3]),
                    )
                    prog.fail_task(
                        key,
                        f"Runtime verification failed for {plugin.display_name}. "
                        "Auto-rollback executed.",
                    )
                    results.append(
                        (
                            f"Plugin: {plugin.display_name}",
                            False,
                            "Runtime verification failed — auto-rollback executed",
                        )
                    )
                    return
                if outcome == "inconclusive":
                    console.print(
                        f"[yellow]  Runtime verification inconclusive for "
                        f"{plugin.display_name} — keeping install; check manually.[/yellow]"
                    )
                else:
                    console.print(
                        f"[green]  Runtime verification passed for {plugin.display_name}.[/green]"
                    )

        prog.complete_task(key)
        state.mark_complete(
            f"plugin:{plugin.full_ref}", {"version": result.version}
        )
        if plugin.local_path:
            # A local archive has no URL to pin — it can't be --restore'd.
            results.append((f"Plugin: {plugin.display_name}", True, "Local — not in lock file"))
            return
        lock_mgr.record(
            PluginLockEntry(
                owner=plugin.owner,
                repo=plugin.repo,
                version=result.version,
                commit_ref=result.commit_ref,
                download_url=result.download_url,
                asset_keyword=None,
                installed_at=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
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


async def _restore_from_lock(
    plugins_dir: Path,
    csgo_dir: Path,
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

                # Same routing rule as install_mod: addons/-rooted content
                # belongs in csgo_dir, never nested under plugins/<repo>/.
                target_dir = plugins_dir / entry.repo
                if mod_manager._routes_to_addons(namelist, case, prefix):
                    target_dir = csgo_dir
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

    if args.add_plugin:
        try:
            base_dir = _resolve_base_dir(args, create=False)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Cancelled by user.[/yellow]")
            return
        if not await _run_add_plugins(base_dir, args.add_plugin, args.verify):
            sys.exit(1)
        return

    if args.check:
        try:
            base_dir = _resolve_base_dir(args, create=False)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Check cancelled by user.[/yellow]")
            return
        if not await _run_check(base_dir, args.live):
            sys.exit(1)
        return

    # ----------------------------------------------------------------------
    # --update mode: refresh CS2 via SteamCMD, then re-apply the Metamod
    # hook that every Steam update silently wipes from gameinfo.gi.
    # ----------------------------------------------------------------------
    if args.update:
        try:
            base_dir = _resolve_base_dir(args)
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Update cancelled by user.[/yellow]")
            return

        steamcmd_dir = base_dir / "steamcmd"
        server_dir = base_dir / "cs2_server"
        csgo_dir = server_dir / "game" / "csgo"
        state = StateManager(base_dir / "install_state.json")
        results: list[tuple[str, bool, str]] = []

        if not is_cs2_installed(server_dir):
            console.print(
                f"[red]No CS2 install found under {server_dir} — "
                "run the installer without --update first.[/red]"
            )
            return

        with InstallationProgress() as prog:
            prog.start_task("update", "Update CS2 Server", total=100.0)
            try:
                steamcmd_wrapper.ensure_disk_space(server_dir)
                if not is_steamcmd_installed(steamcmd_dir):
                    await steamcmd_wrapper.download_steamcmd(steamcmd_dir)
                async for event in steamcmd_wrapper.install_cs2(
                    steamcmd_dir / "steamcmd.exe", server_dir
                ):
                    if event.percent >= 0:
                        prog.update_task("update", event.percent)
                    else:
                        prog.note_task("update", event.raw_line)
                prog.complete_task("update")
                state.mark_complete("cs2_updated")
                results.append(("CS2 Update", True, "Updated & validated"))
            except Exception as exc:
                prog.fail_task("update", str(exc))
                results.append(("CS2 Update", False, str(exc)))
                show_summary(results)
                return

            async with build_async_client() as client:
                if await _ensure_metamod(csgo_dir, client, prog, state, results):
                    await _ensure_cssharp(csgo_dir, client, prog, state, results, force=True)

            prog.start_task("gameinfo", "Re-patch gameinfo.gi")
            if is_gameinfo_patched(csgo_dir):
                prog.complete_task("gameinfo")
                results.append(("gameinfo.gi Patch", True, "Survived update — intact"))
            else:
                try:
                    config_patcher.patch_gameinfo(csgo_dir)
                    prog.complete_task("gameinfo")
                    state.mark_complete("gameinfo_patched")
                    results.append(("gameinfo.gi Patch", True, "Re-applied after update"))
                except Exception as exc:
                    prog.fail_task("gameinfo", str(exc))
                    results.append(("gameinfo.gi Patch", False, str(exc)))

        show_summary(results)
        return

    # Phase 0 — collect all user input before any async work begins
    try:
        base_dir = _resolve_base_dir(args)
        server_config = collect_server_config()
        plugins = collect_plugins()
        effective_verify = True if args.verify else collect_runtime_verify()
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
                results = await _restore_from_lock(
                    plugins_dir, csgo_dir, lock_mgr, client, prog
                )
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
                    steamcmd_wrapper.ensure_disk_space(server_dir)
                    steamcmd_exe = steamcmd_dir / "steamcmd.exe"
                    async for event in steamcmd_wrapper.install_cs2(steamcmd_exe, server_dir):
                        if event.percent >= 0:
                            prog.update_task("cs2", event.percent)
                        else:
                            prog.note_task("cs2", event.raw_line)
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
            metamod_replaced = await _ensure_metamod(csgo_dir, client, prog, state, results)

            # ------------------------------------------------------------------
            # Phase 5: CounterStrikeSharp
            # ------------------------------------------------------------------
            await _ensure_cssharp(
                csgo_dir, client, prog, state, results, force=metamod_replaced
            )

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
                await _install_plugin(
                    plugin,
                    server_dir=server_dir,
                    csgo_dir=csgo_dir,
                    plugins_dir=plugins_dir,
                    snapshot_dir=snapshot_dir,
                    client=client,
                    prog=prog,
                    state=state,
                    lock_mgr=lock_mgr,
                    results=results,
                    verify_config=server_config if effective_verify else None,
                )

    # Phase 9 — summary
    show_summary(results)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user. Exiting.[/yellow]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"\n[red]Fatal error: {exc}[/red]")
        sys.exit(1)
