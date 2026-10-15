"""
render.py — regenerates the README media from a REAL server install.

    python docs/media/render.py --dir C:\\path\\to\\server [--live]

Runs the actual health check (and, with --live, boots the server) and the
real plugin data from cs2-plugins.lock, then writes:

    docs/media/check.svg    static + live health check tables
    docs/media/plugins.svg  plugin manager + install summary
    docs/media/demo.gif     animated replay of `--check --live`

Local paths are anonymized to C:\\cs2\\server; no tokens or passwords are
rendered (the check tables never contain them).
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from rich import box  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.rule import Rule  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.terminal_theme import MONOKAI  # noqa: E402
from rich.text import Text  # noqa: E402

from cli import app as cli_app  # noqa: E402
from core import health_check  # noqa: E402
from models.schemas import PluginRef  # noqa: E402

OUT = Path(__file__).resolve().parent
WIDTH = 84
ANON = r"C:\cs2\server"
FONTS = Path(r"C:\Windows\Fonts")

_STATUS = {
    "ok": "[green]✓  OK[/green]",
    "warn": "[yellow]!  Warning[/yellow]",
    "fail": "[red]✗  Failed[/red]",
}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _anon(text: str, base: Path) -> str:
    for variant in {str(base), str(base).replace("\\", "/")}:
        text = text.replace(variant, ANON)
    return text


def collect(base: Path, live: bool) -> tuple[list, list]:
    static = health_check.run_static_checks(base)
    live_items: list = []
    if live:
        cfg = health_check.parse_start_bat(base / "cs2_server" / "game" / "start_server.bat")
        if cfg is None:
            raise SystemExit("start_server.bat missing — cannot run live check")
        live_items = asyncio.run(health_check.run_live_check(base / "cs2_server", cfg))
    for item in (*static, *live_items):
        item.detail = _anon(item.detail, base)
    return static, live_items


def _check_table(items: list) -> Table:
    table = Table(box=box.ROUNDED, border_style="cyan")
    table.add_column("Check", style="bold white")
    table.add_column("Status")
    table.add_column("Details", style="dim")
    for item in items:
        table.add_row(item.name, _STATUS[item.status], item.detail)
    return table


def _prompt(cmd: str) -> Text:
    t = Text()
    t.append("PS C:\\cs2> ", style="bold green")
    t.append(cmd, style="bold white")
    return t


CHECK_CMD = r"python main.py --check --live --dir C:\cs2\server"


def check_scene(static: list, live_items: list) -> list:
    parts = [
        _prompt(CHECK_CMD),
        Text(""),
        Rule(f"[bold cyan]Health Check — {ANON}[/bold cyan]", style="cyan"),
        _check_table(static),
    ]
    if live_items:
        parts += [Rule("[bold cyan]Live Check[/bold cyan]", style="cyan"), _check_table(live_items)]
    return parts


def plugin_scene(base: Path) -> list:
    lock = json.loads((base / "cs2-plugins.lock").read_text(encoding="utf-8"))["entries"]
    refs = [
        PluginRef(owner="ed0ard", repo="CS2-Bot-Randomizer"),
        PluginRef(owner="XBribo", repo="CS2-Bot-Hider"),
        PluginRef(owner="local", repo="AutoRestart", local_path=r"D:\Downloads\AutoRestart"),
    ]
    # Rich table exactly as the CLI draws it, but captured instead of printed.
    rec = Console(file=io.StringIO(), width=WIDTH, force_terminal=True, color_system="truecolor")
    saved = cli_app.console
    cli_app.console = rec
    try:
        cli_app._render_plugin_table(refs)
    finally:
        cli_app.console = saved
    manager_lines = Text.from_ansi(rec.file.getvalue())

    summary = Table(box=box.ROUNDED, border_style="cyan")
    summary.add_column("Step", style="bold white")
    summary.add_column("Status")
    summary.add_column("Details", style="dim")
    for ref in refs[:2]:
        version = lock.get(ref.full_ref, {}).get("version", "?")
        summary.add_row(f"Plugin: {ref.display_name}", "[green]✓  Done[/green]", version)
    summary.add_row("Plugin: AutoRestart", "[green]✓  Done[/green]", "Local — not in lock file")

    return [
        Rule("[bold cyan]Plugin Manager[/bold cyan]", style="cyan"),
        Text("GitHub:  owner/repo  or  https://github.com/owner/repo", style="dim"),
        Text("Local:   a plugin folder or .zip path (drag & drop works), or [L] to browse", style="dim"),
        Text(""),
        manager_lines,
        Text("[A] Add  [L] Local (browse)  [R] Remove  [C] Continue", style="white"),
        Text(""),
        Rule("[bold cyan]Installation Summary[/bold cyan]", style="cyan"),
        summary,
    ]


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

def save_svg(parts: list, path: Path, title: str) -> None:
    rec = Console(record=True, file=io.StringIO(), width=WIDTH, force_terminal=True,
                  color_system="truecolor")
    for part in parts:
        rec.print(part)
    rec.save_svg(str(path), title=title, theme=MONOKAI)


# ---------------------------------------------------------------------------
# GIF
# ---------------------------------------------------------------------------

BG = (13, 17, 23)
FG = (201, 209, 217)
FONT_SIZE = 17
PAD = 22
BAR = 34


def _lines(parts: list) -> list[list[tuple[str, tuple, bool, bool]]]:
    con = Console(file=io.StringIO(), width=WIDTH, force_terminal=True, color_system="truecolor")
    out = []
    for part in parts:
        for line in con.render_lines(part, con.options, pad=False):
            segs = []
            for seg in line:
                style = seg.style
                color = FG
                if style and style.color:
                    color = tuple(style.color.get_truecolor(MONOKAI))
                bold = bool(style and style.bold)
                dim = bool(style and style.dim)
                segs.append((seg.text, color, bold, dim))
            out.append(segs)
    return out


class Painter:
    def __init__(self, rows: int) -> None:
        self.mono = ImageFont.truetype(str(FONTS / "consola.ttf"), FONT_SIZE)
        self.bold = ImageFont.truetype(str(FONTS / "consolab.ttf"), FONT_SIZE)
        self.sym = ImageFont.truetype(str(FONTS / "seguisym.ttf"), FONT_SIZE - 1)
        left, top, right, bottom = self.mono.getbbox("M")
        self.cw = self.mono.getlength("M")
        self.lh = int(FONT_SIZE * 1.32)
        self.w = int(PAD * 2 + self.cw * WIDTH)
        self.h = BAR + PAD * 2 + self.lh * rows

    # Box-drawing chars are painted as strokes spanning the full cell so
    # borders join seamlessly across rows: (left, right, up, down).
    _BOX = {
        "─": (1, 1, 0, 0), "│": (0, 0, 1, 1),
        "┌": (0, 1, 0, 1), "╭": (0, 1, 0, 1), "┐": (1, 0, 0, 1), "╮": (1, 0, 0, 1),
        "└": (0, 1, 1, 0), "╰": (0, 1, 1, 0), "┘": (1, 0, 1, 0), "╯": (1, 0, 1, 0),
        "├": (0, 1, 1, 1), "┤": (1, 0, 1, 1), "┬": (1, 1, 0, 1), "┴": (1, 1, 1, 0),
        "┼": (1, 1, 1, 1),
    }

    def _stroke(self, d: ImageDraw.ImageDraw, x: float, y: int, ch: str, color: tuple) -> None:
        left, right, up, down = self._BOX[ch]
        cx, cy = int(x + self.cw / 2), y + self.lh // 2
        x0, x1, y0, y1 = int(x), int(x + self.cw), y, y + self.lh
        if left:
            d.rectangle((x0, cy, cx, cy + 1), fill=color)
        if right:
            d.rectangle((cx, cy, x1, cy + 1), fill=color)
        if up:
            d.rectangle((cx, y0, cx + 1, cy), fill=color)
        if down:
            d.rectangle((cx, cy, cx + 1, y1), fill=color)

    @staticmethod
    def _in_mono(ch: str) -> bool:
        o = ord(ch)
        return o < 0x0250 or 0x2000 <= o <= 0x206F or 0x2500 <= o <= 0x259F

    def frame(self, lines: list) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), BG)
        d = ImageDraw.Draw(img)
        # window chrome
        d.rounded_rectangle((0, 0, self.w - 1, self.h - 1), radius=10, outline=(48, 54, 61))
        d.rectangle((1, 1, self.w - 2, BAR), fill=(22, 27, 34))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            cx = 20 + i * 20
            d.ellipse((cx - 6, BAR // 2 - 6, cx + 6, BAR // 2 + 6), fill=c)
        d.text((self.w // 2, BAR // 2), "cs2-server-tool — PowerShell", fill=(139, 148, 158),
               font=self.mono, anchor="mm")
        y = BAR + PAD
        for segs in lines:
            x = PAD
            for text, color, bold, dim in segs:
                if dim:
                    color = tuple(int(c * 0.62 + b * 0.38) for c, b in zip(color, BG))
                for ch in text:
                    if ch in self._BOX:
                        self._stroke(d, x, y, ch, color)
                    elif ch != " ":
                        ch = {"✓": "✔", "✗": "✘"}.get(ch, ch)
                        font = (self.bold if bold else self.mono) if self._in_mono(ch) else self.sym
                        d.text((x, y + 2), ch, fill=color, font=font)
                    x += self.cw
            y += self.lh
        return img


def save_gif(static: list, live_items: list, path: Path) -> None:
    head = _lines([_prompt(CHECK_CMD)])
    static_block = _lines([Text(""), Rule(f"[bold cyan]Health Check — {ANON}[/bold cyan]", style="cyan"),
                           _check_table(static)])
    live_block = _lines([Rule("[bold cyan]Live Check[/bold cyan]", style="cyan"), _check_table(live_items)])
    total_rows = len(head) + len(static_block) + len(live_block) + 1
    p = Painter(total_rows)

    frames: list[Image.Image] = []
    durations: list[int] = []

    def add(lines: list, ms: int) -> None:
        frames.append(p.frame(lines))
        durations.append(ms)

    prompt_prefix = head[0][0]
    cmd_text = head[0][1][0]
    cursor = ("█", FG, False, False)
    add([[prompt_prefix, cursor]], 600)
    for i in range(1, len(cmd_text) + 1, 2):
        add([[prompt_prefix, (cmd_text[:i], FG, True, False), cursor]], 45)
    typed = [[prompt_prefix, (cmd_text, FG, True, False)]]
    add(typed, 500)

    shown = list(typed)
    for line in static_block:
        shown.append(line)
        add(list(shown), 55)
    add(list(shown), 900)

    spin = "◐◓◑◒"
    for i in range(16):
        status = [(f"{spin[i % 4]} ", (102, 217, 239), True, False),
                  ("Booting CS2 server for live check…", (102, 217, 239), False, False)]
        add(shown + [status], 120)

    for line in live_block:
        shown.append(line)
        add(list(shown), 70)
    add(list(shown), 4500)

    frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, optimize=True, disposal=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, type=Path)
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    base = args.dir.resolve()

    static, live_items = collect(base, args.live)
    save_svg(check_scene(static, live_items), OUT / "check.svg", "python main.py --check --live")
    save_svg(plugin_scene(base), OUT / "plugins.svg", "Plugin Manager")
    if live_items:
        save_gif(static, live_items, OUT / "demo.gif")
    for f in ("check.svg", "plugins.svg", "demo.gif"):
        fp = OUT / f
        if fp.exists():
            print(f"{f}: {fp.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
