"""Alfred's screen sight: capture what is on a display, then look at it.

This is the missing organ between "he can look at an image file" (media.py)
and "hey, can you see my screen?" — it grabs the live screen, hands the frame
to the vision model, and answers. This handler is the on-demand form, used
for *other* machines' screens on request; the owner's own screen is watched
continuously by alfred.core.sight, which reuses `capture()` below.

Capture is best-effort across environments and degrades honestly:
  - WSL           : PowerShell photographs the real Windows desktop
  - Linux/Wayland : grim
  - Linux/X11     : scrot, then imagemagick import, then ffmpeg x11grab
If no grabber is present, it says so and names the one to install, rather
than pretending to see.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import tempfile
from pathlib import Path

from alfred import llm, wsl
from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok

VISION_SYSTEM = (
    llm.WORKER_SYSTEM
    + " You are looking at a capture of the owner's computer screen. Describe "
    "what is actually visible — the application in focus, key text, anything "
    "that looks like an error or a setting worth noting. If asked a specific "
    "question, answer only from what the screen shows."
)


def _vision_model(cfg: dict) -> str:
    return llm.vision_model(cfg)


async def _run(cmd: list[str], timeout: int = 20) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out
    except asyncio.TimeoutError:
        proc.kill()
        return 1, b""


# Photograph the whole virtual desktop (all monitors) from the Windows side.
# SetProcessDPIAware first, or a 150 %-scaled display yields a cropped frame.
_WIN_GRAB = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
    "Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public class Dpi "
    "{ [DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware(); }'; "
    "[Dpi]::SetProcessDPIAware() | Out-Null; "
    "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
    "$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height); "
    "$g=[System.Drawing.Graphics]::FromImage($bmp); "
    "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); "
    "$p=Join-Path ([System.IO.Path]::GetTempPath()) ('alfred-'+[guid]::NewGuid()+'.png'); "
    "$bmp.Save($p); $g.Dispose(); $bmp.Dispose(); [Console]::Out.Write($p)"
)


async def _capture_windows(dest: Path) -> tuple[bool, str]:
    code, out = await wsl.powershell(_WIN_GRAB)
    if code != 0 or not out:
        return False, f"Windows screen capture failed: {out[-200:] or 'no output'}"
    winpath = out.splitlines()[-1].strip()
    code, out = await _run(["wslpath", "-u", winpath])
    lin = out.decode().strip()
    if code != 0 or not lin or not Path(lin).is_file():
        return False, f"Windows wrote {winpath} but WSL cannot read it"
    shutil.copy(lin, dest)
    try:
        Path(lin).unlink()
    except OSError:
        pass
    return True, ""


async def capture(dest: Path) -> tuple[bool, str]:
    """Grab the screen to dest as PNG. Returns (ok, note-on-failure)."""
    if wsl.is_wsl():
        return await _capture_windows(dest)

    # Wayland
    if shutil.which("grim"):
        code, _ = await _run(["grim", str(dest)])
        if code == 0 and dest.is_file():
            return True, ""
    # X11: scrot
    if shutil.which("scrot"):
        code, _ = await _run(["scrot", "-o", str(dest)])
        if code == 0 and dest.is_file():
            return True, ""
    # X11: imagemagick import
    if shutil.which("import"):
        code, _ = await _run(["import", "-window", "root", str(dest)])
        if code == 0 and dest.is_file():
            return True, ""
    # X11: ffmpeg x11grab, one frame
    if shutil.which("ffmpeg") and os.environ.get("DISPLAY"):
        code, _ = await _run([
            "ffmpeg", "-y", "-f", "x11grab", "-frames:v", "1",
            "-i", os.environ["DISPLAY"], str(dest),
        ])
        if code == 0 and dest.is_file():
            return True, ""
    return False, ("no screen grabber found — install one: "
                   "grim (Wayland) or scrot (X11)")


@handler("screen.view")
async def screen_view(task: Task, cfg: dict) -> TaskResult:
    """Capture this machine's screen and answer about it with the vision model."""
    question = task.inputs.get("question") or task.prompt or \
        "What is on this screen right now? Note the focused app and anything important."
    with tempfile.TemporaryDirectory() as td:
        shot = Path(td) / "screen.png"
        grabbed, note = await capture(shot)
        if not grabbed:
            return fail(task, note)
        b64 = base64.b64encode(shot.read_bytes()).decode()
        try:
            answer = await llm.complete(
                f"{question}\n\nBe specific and concrete. Under 200 words.",
                cfg, system=VISION_SYSTEM, images=[b64],
                model=_vision_model(cfg), timeout=180,
            )
        except Exception as exc:
            return fail(
                task,
                f"vision model failed: {exc} — is it pulled? "
                f"ollama pull {_vision_model(cfg)}",
            )
    return ok(task, summary=answer, data={"model": _vision_model(cfg), "source": "screen"})
