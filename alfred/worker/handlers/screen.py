"""Alfred's screen sight: capture what is on a display, then look at it.

This is the missing organ between "he can look at an image file" (media.py)
and "hey, can you see my screen?" — it grabs the live screen, hands the frame
to the vision model, and answers. On-demand today; the always-on glance loop
builds on exactly this.

Capture is best-effort across environments and degrades honestly:
  - Linux/Wayland : grim
  - Linux/X11     : scrot, then imagemagick import, then ffmpeg x11grab
  - WSL (Zenbook) : PowerShell screen grab of the Windows desktop
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

from alfred import llm
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


def _is_wsl() -> bool:
    return "microsoft" in os.uname().release.lower()


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


async def _capture(dest: Path) -> tuple[bool, str]:
    """Grab the screen to dest as PNG. Returns (ok, note-on-failure)."""
    # WSL: reach out to Windows and photograph the real desktop
    if _is_wsl():
        win_tmp = None
        # write to a Windows-visible temp, then read it back
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height); "
            "$g=[System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); "
            "$p=[System.IO.Path]::GetTempFileName()+'.png'; "
            "$bmp.Save($p); [Console]::Out.Write($p)"
        )
        code, out = await _run(["powershell.exe", "-NoProfile", "-Command", ps])
        winpath = out.decode(errors="ignore").strip()
        if code == 0 and winpath:
            # translate C:\...\file.png -> /mnt/c/.../file.png
            code2, out2 = await _run(["wslpath", "-u", winpath])
            lin = out2.decode().strip()
            if code2 == 0 and lin and Path(lin).is_file():
                shutil.copy(lin, dest)
                try:
                    Path(lin).unlink()
                except OSError:
                    pass
                return True, ""
        return False, "WSL screen capture failed (PowerShell screenshot)"

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
                   "grim (Wayland), scrot (X11), or run on WSL")


@handler("screen.view")
async def screen_view(task: Task, cfg: dict) -> TaskResult:
    """Capture this machine's screen and answer about it with the vision model."""
    question = task.inputs.get("question") or task.prompt or \
        "What is on this screen right now? Note the focused app and anything important."
    with tempfile.TemporaryDirectory() as td:
        shot = Path(td) / "screen.png"
        grabbed, note = await _capture(shot)
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
