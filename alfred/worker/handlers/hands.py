"""Alfred's hands: the mouse, the keyboard, apps and windows on the desktop.

Two capabilities:

    ui.windows   read-only, runs at once: screen size, the window in front,
                 every window with a title. How Alfred learns where things
                 are before he touches anything.
    ui.act       ONE action from the closed catalog below. Never dispatched
                 without the owner's approval (the panel card) or a standing
                 "let Alfred drive" grant with minutes left on it; the core
                 marks the task `_approved` either way and this handler
                 refuses anything unmarked, so a task that slips past the gate
                 still does nothing.

Two backends behind one interface: Windows through PowerShell (the owner
runs Alfred inside WSL, so `powershell.exe` reaches the real desktop), and
Linux through xdotool. Neither is a shell. There is deliberately no action
that runs a command, and opening or focusing a terminal is refused even with
approval: a terminal plus `type` would be free-form shell by the back door,
and the catalog is only worth having if it stays closed.

Eyes and hands are separate on purpose. Nothing here decides anything from
the screen, and nothing in sight.py can call this. The one crossing is the
receipt: after every action the screen is captured to a file, so the owner
can see what the hands did, not just read about it.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from alfred import wsl
from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import handler
from alfred.worker.handlers.screen import capture

MAX_TEXT = 2000
PAUSE_AFTER_S = 0.4   # let the desktop react before reporting what is in front

# Windows Alfred must never type into, focus or open: each one is a shell.
TERMINALS = re.compile(
    r"powershell|command prompt|cmd\.exe|windows terminal|\bwsl\b|ubuntu|"
    r"\bbash\b|\bzsh\b|\bterminal\b|konsole|xterm|gnome-terminal|alacritty|kitty|^run$",
    re.IGNORECASE,
)
# Places where typing a program's name runs it: the Start menu, its search box.
LAUNCHERS = re.compile(r"^(start|search|windows search|start menu|search box)$", re.IGNORECASE)
# The owner's own controls: closing them is how you lose the STOP button.
OWN_WINDOWS = re.compile(r"alfred|micron os", re.IGNORECASE)
# Programs whose whole purpose is arbitrary commands or system-wide damage.
DENIED_APPS = {
    "cmd", "powershell", "pwsh", "powershell_ise", "wt", "wsl", "bash", "sh", "zsh",
    "regedit", "shutdown", "logoff", "diskpart", "bcdedit", "gnome-terminal", "xterm",
    "konsole", "alacritty", "kitty", "sudo", "su", "mshta", "cscript", "wscript",
}
# Files that are programs in disguise; opening one is running a script.
SCRIPT_SUFFIXES = (".bat", ".cmd", ".ps1", ".vbs", ".js", ".wsf", ".msi", ".reg", ".sh", ".py")

BUTTONS = {"left": 1, "middle": 2, "right": 3}

# Key names the owner (and the planner) may use, mapped per backend.
_VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "super": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "printscreen": 0x2C, "capslock": 0x14,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)},   # a-z -> 0x41..
    **{str(d): 0x30 + d for d in range(10)},
    ",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, "-": 0xBD, "=": 0xBB, "`": 0xC0,
}
_XDO = {
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "win": "super", "super": "super",
    "enter": "Return", "return": "Return", "tab": "Tab", "esc": "Escape", "escape": "Escape",
    "space": "space", "backspace": "BackSpace", "delete": "Delete", "del": "Delete", "insert": "Insert",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
    "printscreen": "Print", "capslock": "Caps_Lock",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
    **{chr(c): chr(c) for c in range(ord("a"), ord("z") + 1)},
    **{str(d): str(d) for d in range(10)},
    ",": "comma", ".": "period", "/": "slash", ";": "semicolon", "'": "apostrophe",
    "[": "bracketleft", "]": "bracketright", "\\": "backslash", "-": "minus", "=": "equal", "`": "grave",
}


def parse_combo(combo: str) -> list[str]:
    """'Ctrl + Shift + S' -> ['ctrl', 'shift', 's']; raises ValueError on
    anything not in the key table."""
    keys = [k.strip().lower() for k in str(combo).split("+") if k.strip()]
    if not keys:
        raise ValueError("no keys given")
    bad = [k for k in keys if k not in _VK]
    if bad:
        raise ValueError(f"unknown key(s) {bad}; use names like ctrl, alt, shift, win, enter, "
                         "tab, esc, f5, a-z, 0-9")
    return keys


# ---- backends ------------------------------------------------------------

class Backend:
    name = "none"

    async def screen(self) -> tuple[int, int]:
        raise NotImplementedError

    async def foreground(self) -> str:
        raise NotImplementedError

    async def windows(self) -> list[dict]:
        raise NotImplementedError

    async def move(self, x: int, y: int) -> None:
        raise NotImplementedError

    async def click(self, x: int | None, y: int | None, button: str, double: bool) -> None:
        raise NotImplementedError

    async def scroll(self, amount: int, x: int | None, y: int | None) -> None:
        raise NotImplementedError

    async def type_text(self, text: str) -> None:
        raise NotImplementedError

    async def key(self, keys: list[str]) -> None:
        raise NotImplementedError

    async def open_app(self, app: str) -> None:
        raise NotImplementedError

    async def focus(self, title: str) -> str:
        raise NotImplementedError

    async def close(self, title: str) -> str:
        raise NotImplementedError


class HandsError(RuntimeError):
    """A backend command failed; the message is for the owner."""


def _ps_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _sendkeys_escape(text: str) -> str:
    # SendKeys grammar: these have meaning unless braced; newline is Enter.
    out = []
    for ch in text:
        if ch in "+^%~(){}[]":
            out.append("{" + ch + "}")
        elif ch == "\n":
            out.append("{ENTER}")
        elif ch == "\t":
            out.append("{TAB}")
        elif ch == "\r":
            continue
        else:
            out.append(ch)
    return "".join(out)


_PS_PRELUDE = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
    "Add-Type -TypeDefinition @'\n"
    "using System; using System.Runtime.InteropServices; using System.Text;\n"
    "public class Hands {\n"
    " [DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();\n"
    " [DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int x, int y);\n"
    " [DllImport(\"user32.dll\")] public static extern void mouse_event(uint f, uint dx, uint dy, int data, UIntPtr extra);\n"
    " [DllImport(\"user32.dll\")] public static extern void keybd_event(byte vk, byte scan, uint f, UIntPtr extra);\n"
    " [DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow();\n"
    " [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h);\n"
    " [DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h, int cmd);\n"
    " [DllImport(\"user32.dll\")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);\n"
    " public static string Title(IntPtr h) { var sb = new StringBuilder(512); GetWindowText(h, sb, 512); return sb.ToString(); }\n"
    " public static string Front() { return Title(GetForegroundWindow()); }\n"
    "}\n'@; [Hands]::SetProcessDPIAware() | Out-Null; "
)
_PS_FIND = (
    "$p = Get-Process | Where-Object { $_.MainWindowTitle -and "
    "$_.MainWindowTitle.ToLower().Contains(TITLE.ToLower()) } | Select-Object -First 1; "
    "if (-not $p) { [Console]::Out.Write('NOWINDOW'); exit 0 }; "
)


def _ps_find(title: str) -> str:
    return _PS_FIND.replace("TITLE", _ps_str(title))


class WindowsHands(Backend):
    """Windows, driven from WSL (or native Windows) through powershell.exe."""
    name = "windows"

    async def _ps(self, body: str) -> str:
        code, out = await wsl.powershell(_PS_PRELUDE + body, timeout=30)
        if code != 0:
            raise HandsError(f"Windows refused (exit {code}): {out[-300:] or 'no output'}")
        return out

    async def screen(self) -> tuple[int, int]:
        out = await self._ps("$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
                             "[Console]::Out.Write(\"$($b.Width) $($b.Height)\")")
        w, h = out.split()[-2:]
        return int(w), int(h)

    async def foreground(self) -> str:
        return (await self._ps("[Console]::Out.Write([Hands]::Front())")).strip()

    async def windows(self) -> list[dict]:
        out = await self._ps(
            "Get-Process | Where-Object { $_.MainWindowTitle } | ForEach-Object { "
            "[Console]::Out.WriteLine(\"$($_.Id)`t$($_.ProcessName)`t$($_.MainWindowTitle)\") }")
        rows = []
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                rows.append({"pid": int(parts[0]), "app": parts[1], "title": parts[2]})
        return rows

    async def move(self, x: int, y: int) -> None:
        await self._ps(f"[Hands]::SetCursorPos({x},{y}) | Out-Null")

    async def click(self, x: int | None, y: int | None, button: str, double: bool) -> None:
        down, up = {"left": (0x02, 0x04), "right": (0x08, 0x10), "middle": (0x20, 0x40)}[button]
        pos = f"[Hands]::SetCursorPos({x},{y}) | Out-Null; Start-Sleep -Milliseconds 60; " if x is not None else ""
        one = f"[Hands]::mouse_event({down},0,0,0,[UIntPtr]::Zero); [Hands]::mouse_event({up},0,0,0,[UIntPtr]::Zero); "
        await self._ps(pos + one + (("Start-Sleep -Milliseconds 90; " + one) if double else ""))

    async def scroll(self, amount: int, x: int | None, y: int | None) -> None:
        pos = f"[Hands]::SetCursorPos({x},{y}) | Out-Null; " if x is not None else ""
        await self._ps(pos + f"[Hands]::mouse_event(0x0800,0,0,{-120 * amount},[UIntPtr]::Zero)")

    async def type_text(self, text: str) -> None:
        await self._ps(f"[System.Windows.Forms.SendKeys]::SendWait({_ps_str(_sendkeys_escape(text))})")

    async def key(self, keys: list[str]) -> None:
        codes = [_VK[k] for k in keys]
        press = "; ".join(f"[Hands]::keybd_event({c},0,0,[UIntPtr]::Zero)" for c in codes)
        release = "; ".join(f"[Hands]::keybd_event({c},0,2,[UIntPtr]::Zero)" for c in reversed(codes))
        await self._ps(f"{press}; Start-Sleep -Milliseconds 40; {release}")

    async def open_app(self, app: str) -> None:
        await self._ps(f"Start-Process -FilePath {_ps_str(app)}")

    async def focus(self, title: str) -> str:
        out = await self._ps(
            _ps_find(title)
            + "[Hands]::ShowWindow($p.MainWindowHandle, 9) | Out-Null; "
            "[Hands]::SetForegroundWindow($p.MainWindowHandle) | Out-Null; "
            "[Console]::Out.Write($p.MainWindowTitle)")
        if out.strip() == "NOWINDOW":
            raise HandsError(f"no window with '{title}' in its title")
        return out.strip()

    async def close(self, title: str) -> str:
        out = await self._ps(
            _ps_find(title)
            + "$t = $p.MainWindowTitle; $p.CloseMainWindow() | Out-Null; [Console]::Out.Write($t)")
        if out.strip() == "NOWINDOW":
            raise HandsError(f"no window with '{title}' in its title")
        return out.strip()


class XdoHands(Backend):
    """Linux desktops (X11) through xdotool. Also what the tests here run."""
    name = "xdotool"

    async def _xdo(self, *args: str, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            "xdotool", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
        text = out.decode(errors="replace").strip()
        if check and proc.returncode:
            raise HandsError(f"xdotool {args[0]} failed: {text[-200:] or 'no output'}")
        return text

    async def screen(self) -> tuple[int, int]:
        w, h = (await self._xdo("getdisplaygeometry")).split()[:2]
        return int(w), int(h)

    async def foreground(self) -> str:
        return await self._xdo("getactivewindow", "getwindowname", check=False)

    async def windows(self) -> list[dict]:
        ids = (await self._xdo("search", "--onlyvisible", "--name", ".", check=False)).split()
        rows = []
        for wid in ids[:60]:
            title = await self._xdo("getwindowname", wid, check=False)
            if title:
                rows.append({"pid": int(wid), "app": "", "title": title})
        return rows

    async def move(self, x: int, y: int) -> None:
        await self._xdo("mousemove", str(x), str(y))

    async def click(self, x: int | None, y: int | None, button: str, double: bool) -> None:
        if x is not None:
            await self._xdo("mousemove", "--sync", str(x), str(y))
        await self._xdo("click", *(["--repeat", "2", "--delay", "90"] if double else []), str(BUTTONS[button]))

    async def scroll(self, amount: int, x: int | None, y: int | None) -> None:
        if x is not None:
            await self._xdo("mousemove", "--sync", str(x), str(y))
        await self._xdo("click", "--repeat", str(abs(amount)), "--delay", "30", "5" if amount > 0 else "4")

    async def type_text(self, text: str) -> None:
        await self._xdo("type", "--delay", "15", "--", text)

    async def key(self, keys: list[str]) -> None:
        await self._xdo("key", "--clearmodifiers", "+".join(_XDO[k] for k in keys))

    async def open_app(self, app: str) -> None:
        if not shutil.which(app):
            raise HandsError(f"no program called '{app}' on this machine")
        subprocess.Popen([app], start_new_session=True, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=os.environ.copy())

    async def _find(self, title: str) -> str:
        ids = (await self._xdo("search", "--onlyvisible", "--name", re.escape(title), check=False)).split()
        if not ids:
            raise HandsError(f"no window with '{title}' in its title")
        return ids[0]

    async def focus(self, title: str) -> str:
        wid = await self._find(title)
        await self._xdo("windowactivate", "--sync", wid)
        return await self._xdo("getwindowname", wid, check=False)

    async def close(self, title: str) -> str:
        wid = await self._find(title)
        name = await self._xdo("getwindowname", wid, check=False)
        if shutil.which("wmctrl"):   # asks the app to close, so it can prompt to save
            proc = await asyncio.create_subprocess_exec(
                "wmctrl", "-ic", wid, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 20)
            if proc.returncode == 0:
                return name
        await self._xdo("windowclose", wid)
        return name


def backend() -> Backend | None:
    if wsl.is_wsl() or platform.system() == "Windows":
        return WindowsHands() if shutil.which("powershell.exe") else None
    if shutil.which("xdotool") and os.environ.get("DISPLAY"):
        return XdoHands()
    return None


# ---- the catalog ----------------------------------------------------------

def _xy(args: dict, required: bool) -> tuple[int | None, int | None]:
    if "x" not in args and "y" not in args:
        if required:
            raise KeyError("'x' and 'y'")
        return None, None
    try:
        return int(args["x"]), int(args["y"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("x and y must both be whole numbers") from None


def describe_action(action: str, args: dict) -> str:
    """One plain sentence for the approval card: exactly what the hands will
    do, before they do it."""
    try:
        if action == "move":
            return f"Move the mouse to ({int(args['x'])}, {int(args['y'])})"
        if action == "click":
            what = f"{'Double-click' if args.get('double') else 'Click'} {args.get('button', 'left')}"
            x, y = _xy(args, required=False)
            return f"{what} at ({x}, {y})" if x is not None else f"{what} where the mouse is"
        if action == "scroll":
            n = int(args.get("amount", 3))
            return f"Scroll {'down' if n > 0 else 'up'} {abs(n)} notch{'es' if abs(n) != 1 else ''}"
        if action == "type":
            text = str(args.get("text", ""))
            shown = text if len(text) <= 60 else text[:57] + "..."
            return f"Type {shown!r} into the window in front"
        if action == "key":
            return "Press " + "+".join(k.capitalize() for k in parse_combo(str(args.get("combo", ""))))
        if action == "open_app":
            return f"Open {args.get('app', '?')}"
        if action == "focus_window":
            return f"Bring the window '{args.get('title', '?')}' to the front"
        if action == "close_window":
            return f"Close the window '{args.get('title', '?')}'"
    except (KeyError, TypeError, ValueError):
        pass
    return f"{action} {args}"


def deny_reason(action: str, args: dict, front: str) -> str | None:
    """Refusals that no approval overrides. `front` is the window in front
    right now, because typing goes wherever the focus is."""
    if action in ("type", "key") and TERMINALS.search(front or ""):
        return f"the window in front is a terminal ('{front}'); Alfred does not type into shells"
    if action == "focus_window" and TERMINALS.search(str(args.get("title", ""))):
        return "that is a terminal; Alfred does not take the keyboard to a shell"
    if action == "open_app":
        app = str(args.get("app", "")).lower().strip("'\" ")
        base = re.split(r"[\\/]", app)[-1].removesuffix(".exe")
        if base in DENIED_APPS:
            return f"'{base}' is a shell or a system tool, not in the catalog"
        if app.endswith(SCRIPT_SUFFIXES):
            return "that is a script, and opening it runs it; Alfred's hands do not run commands"
    if action == "type" and LAUNCHERS.match((front or "").strip()):
        words = set(re.findall(r"[a-z_]+", str(args.get("text", "")).lower()))
        if words & DENIED_APPS:
            return "typing a shell's name into the Start menu would launch it; use open_app for programs"
    if action == "close_window" and OWN_WINDOWS.search(str(args.get("title", ""))):
        return "that window is the owner's Alfred panel; closing it would take away the STOP button"
    if action == "type" and len(str(args.get("text", ""))) > MAX_TEXT:
        return f"text longer than {MAX_TEXT} characters; split it"
    return None


async def perform(be: Backend, action: str, args: dict) -> str:
    """Do one catalog action. Returns a short past-tense phrase."""
    if action == "move":
        x, y = _xy(args, required=True)
        await be.move(x, y)
        return f"moved the mouse to ({x}, {y})"
    if action == "click":
        button = str(args.get("button", "left")).lower()
        if button not in BUTTONS:
            raise ValueError("button must be left, right or middle")
        x, y = _xy(args, required=False)
        double = bool(args.get("double"))
        await be.click(x, y, button, double)
        where = f"at ({x}, {y})" if x is not None else "where the mouse was"
        return f"{'double-clicked' if double else 'clicked'} {button} {where}"
    if action == "scroll":
        n = int(args.get("amount", 3))
        if n == 0 or abs(n) > 50:
            raise ValueError("amount is notches, -50..50, positive is down")
        x, y = _xy(args, required=False)
        await be.scroll(n, x, y)
        return f"scrolled {'down' if n > 0 else 'up'} {abs(n)}"
    if action == "type":
        text = str(args.get("text", ""))
        if not text:
            raise ValueError("nothing to type")
        await be.type_text(text)
        return f"typed {len(text)} characters"
    if action == "key":
        keys = parse_combo(str(args.get("combo", "")))
        await be.key(keys)
        return "pressed " + "+".join(keys)
    if action == "open_app":
        app = str(args.get("app", "")).strip()
        if not app or any(c in app for c in "&|;<>`$\n"):
            raise ValueError("app must be a program name or path, nothing else")
        await be.open_app(app)
        return f"opened {app}"
    if action == "focus_window":
        title = str(args.get("title", "")).strip()
        if not title:
            raise ValueError("title is required")
        return f"brought '{await be.focus(title)}' to the front"
    if action == "close_window":
        title = str(args.get("title", "")).strip()
        if not title:
            raise ValueError("title is required")
        return f"asked '{await be.close(title)}' to close"
    raise ValueError(f"unknown action {action}")


ACTIONS = {
    "move": "move the mouse to (x, y)",
    "click": "click left/right/middle at (x, y) or where the mouse is; double: true for a double-click",
    "scroll": "scroll the wheel `amount` notches (positive down) at (x, y) or where the mouse is",
    "type": "type `text` into the window in front (newline = Enter)",
    "key": "press a key combination `combo` like ctrl+s, alt+tab, win+d, enter",
    "open_app": "start a program by name, e.g. notepad, calc, msedge",
    "focus_window": "bring the window whose title contains `title` to the front",
    "close_window": "ask the window whose title contains `title` to close (it may prompt to save)",
}


async def _receipt(cfg: dict, task: Task) -> tuple[str | None, str]:
    """Screenshot after the action, as an artifact. Failure is a note, not
    an error: the action already happened and must be reported either way."""
    try:
        directory = Path(cfg["core"]["artifact_dir"]) / task.id
        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / "after.png"
        ok, note = await capture(dest)
    except (KeyError, OSError, asyncio.TimeoutError) as exc:
        return None, f"no screenshot: {exc}"
    return (dest.as_uri(), "") if ok else (None, f"no screenshot: {note}")


def _no_hands(task: Task) -> TaskResult:
    return TaskResult(
        task_id=task.id, worker_id="", status="rejected",
        error="no hands on this machine: needs powershell.exe (WSL/Windows) or xdotool with "
              "a DISPLAY (Linux)",
    )


@handler("ui.windows")
async def ui_windows(task: Task, cfg: dict) -> TaskResult:
    be = backend()
    if be is None:
        return _no_hands(task)
    try:
        w, h = await be.screen()
        front = await be.foreground()
        wins = await be.windows()
    except (HandsError, ValueError, asyncio.TimeoutError) as exc:
        return TaskResult(task_id=task.id, worker_id="", status="error", error=str(exc))
    listing = "\n".join(f"  - {r['title']}" + (f" ({r['app']})" if r["app"] else "") for r in wins[:30])
    return TaskResult(
        task_id=task.id, worker_id="", status="ok",
        summary=f"Screen {w}x{h}. In front: '{front or 'nothing'}'. "
                f"{len(wins)} window(s) with a title:\n{listing or '  (none)'}",
        data={"screen": [w, h], "foreground": front, "windows": wins, "backend": be.name},
    )


@handler("ui.act")
async def ui_act(task: Task, cfg: dict) -> TaskResult:
    action = str(task.inputs.get("action", ""))
    args = task.inputs.get("args") or {}
    if not isinstance(args, dict):
        args = {}

    if action not in ACTIONS:
        return TaskResult(task_id=task.id, worker_id="", status="rejected",
                          error=f"'{action}' is not in the catalog: {', '.join(ACTIONS)}")
    if not task.inputs.get("_approved"):
        return TaskResult(
            task_id=task.id, worker_id="", status="rejected",
            error="ui.act without the owner's approval or a driving grant; this task should "
                  "have been parked as a pending action, not dispatched",
        )
    if not task.idempotency_key:
        return TaskResult(task_id=task.id, worker_id="", status="error",
                          error="refusing to touch the desktop without an idempotency_key")

    be = backend()
    if be is None:
        return _no_hands(task)

    description = describe_action(action, args)
    try:
        front_before = await be.foreground()
        reason = deny_reason(action, args, front_before)
        if reason is not None:
            return TaskResult(task_id=task.id, worker_id="", status="rejected",
                              error=f"denied: {reason}")
        did = await perform(be, action, args)
        await asyncio.sleep(PAUSE_AFTER_S)
        front_after = await be.foreground()
    except KeyError as exc:
        return TaskResult(task_id=task.id, worker_id="", status="error",
                          error=f"{action} is missing argument {exc}")
    except (HandsError, ValueError) as exc:
        return TaskResult(task_id=task.id, worker_id="", status="error",
                          error=f"{description} failed: {exc}")
    except asyncio.TimeoutError:
        return TaskResult(task_id=task.id, worker_id="", status="error",
                          error=f"{description} failed: the desktop did not answer in time")

    shot, shot_note = await _receipt(cfg, task)
    return TaskResult(
        task_id=task.id, worker_id="", status="ok",
        summary=f"{description} — {did}. In front now: '{front_after or 'nothing'}'."
                + (f" ({shot_note})" if shot_note else ""),
        artifacts=[shot] if shot else [],
        data={"action": action, "did": did, "foreground_before": front_before,
              "foreground_after": front_after, "backend": be.name},
    )
