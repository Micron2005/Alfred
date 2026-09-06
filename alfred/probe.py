"""Machine self-inventory.

A new node runs this on startup and announces the result. It is the only
thing a device has to be able to do before Alfred knows what to do with it —
no config file, no capability list, no name.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from alfred.contracts import NodeProfile

PROBE_BINARIES = [
    "git", "python3", "gcc", "make", "docker", "ffmpeg",
    "freecadcmd", "FreeCADCmd", "openscad", "kicad",
    "mosquitto_pub", "pandoc", "nvidia-smi", "ollama",
]
PROBE_PACKAGES = ["httpx", "pypdf", "paho", "psutil", "numpy", "scipy", "PIL"]

ID_FILE = Path("~/.alfred/node_id").expanduser()


def node_id() -> str:
    """Stable across reboots, so a machine that comes back is recognised as
    itself rather than enrolling all over again."""
    if ID_FILE.exists():
        return ID_FILE.read_text().strip()
    generated = f"{socket.gethostname().split('.')[0]}-{uuid.uuid4().hex[:6]}"
    ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    ID_FILE.write_text(generated)
    return generated


def _ram_gb() -> float:
    try:
        import psutil
        return round(psutil.virtual_memory().total / 1e9, 1)
    except ImportError:
        pass
    try:  # Linux without psutil
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1e6, 1)
    except OSError:
        pass
    return 2.0  # conservative: assume weak rather than assume capable


def _vram_gb() -> float:
    if not shutil.which("nvidia-smi"):
        return 0.0
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        ).stdout.strip().splitlines()
        return round(max(int(x) for x in out) / 1024, 1)
    except Exception:
        return 0.0


def _gpu_name() -> str:
    if not shutil.which("nvidia-smi"):
        return ""
    try:
        return subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8,
        ).stdout.strip().splitlines()[0]
    except Exception:
        return ""


def _reachable(url: str) -> bool:
    try:
        urllib.request.urlopen(url + "/api/tags", timeout=4)
        return True
    except (urllib.error.URLError, OSError):
        return False


def probe(cfg: dict | None = None) -> NodeProfile:
    cfg = cfg or {}
    core = cfg.get("core", {})
    local_ollama = _reachable(core.get("ollama_url", "http://127.0.0.1:11434"))
    remote = core.get("remote_model_url")
    if core.get("provider") == "openai":
        remote = bool(os.environ.get(core.get("api_key_env", "ALFRED_API_KEY")))

    return NodeProfile(
        node_id=node_id(),
        hostname=socket.gethostname(),
        os=f"{platform.system()} {platform.release()}",
        arch=platform.machine(),
        cpu_cores=os.cpu_count() or 1,
        ram_gb=_ram_gb(),
        vram_gb=_vram_gb(),
        gpu_name=_gpu_name(),
        binaries=[b for b in PROBE_BINARIES if shutil.which(b)],
        python_pkgs=[p for p in PROBE_PACKAGES if importlib.util.find_spec(p) is not None],
        has_local_model=local_ollama,
        can_reach_model=bool(remote) or local_ollama,
        python_version=platform.python_version(),
    )
