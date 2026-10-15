"""plugin_load_results against log lines captured from a real server boot."""
from __future__ import annotations

from core.health_check import plugin_load_results

# Excerpt from a real boot on the test server (WeaponPaints without a database).
REAL_LOG = r"""
[INFO] (cssharp:PluginContext) Loading plugin MenuManagerCore
[INFO] (cssharp:PluginContext) Finished loading plugin MenuManager [Core]
[INFO] (cssharp:PluginContext) Loading plugin WeaponPaints
[EROR] (cssharp:PluginManager) Failed to load plugin from C:\srv\game\csgo\addons\counterstrikesharp\plugins\WeaponPaints\WeaponPaints.dll
System.Reflection.TargetInvocationException: Exception has been thrown by the target of an invocation.
 ---> System.InvalidOperationException: You need to setup Database credentials in "configs/plugins/WeaponPaints/WeaponPaints.json"!
[INFO] (cssharp:PluginContext) Loading plugin CS2-SimpleAdmin
[INFO] (cssharp:PluginContext) Finished loading plugin CS2-SimpleAdmin (RELEASE)
"""


def test_display_names_map_back_to_folder_names() -> None:
    loaded, _ = plugin_load_results(REAL_LOG)
    assert loaded == {"menumanagercore", "cs2-simpleadmin"}


def test_failure_reason_skips_reflection_wrapper() -> None:
    _, failed = plugin_load_results(REAL_LOG)
    assert list(failed) == ["WeaponPaints"]
    assert failed["WeaponPaints"].startswith("You need to setup Database credentials")


def test_later_successful_boot_clears_earlier_failure() -> None:
    log = REAL_LOG + (
        "[INFO] (cssharp:PluginContext) Loading plugin WeaponPaints\n"
        "[INFO] (cssharp:PluginContext) Finished loading plugin WeaponPaints\n"
    )
    loaded, failed = plugin_load_results(log)
    assert "weaponpaints" in loaded and "WeaponPaints" not in failed
