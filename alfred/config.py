"""Config loading.

This module is the answer to "do I upload the same code and rename it?".
The code is identical on every machine. This file is what differs, and what
it declares is capabilities — never an identity.

A config that says `capabilities = ["code.write"]` can move from the Zenbook
to a new machine unchanged, and the work follows it. A config that said
`role = "atlas"` would pin the work to a box forever.
"""

from __future__ import annotations

import os
import shutil
import socket
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "worker": {
        "id": None,  # defaults to the hostname
        "capabilities": [],
        "concurrency": 1,
        "heartbeat_s": 5,
        "software": [],  # probed at startup if left empty
    },
    "bus": {"kind": "local", "url": "nats://127.0.0.1:4222"},
    "core": {
        "state_db": "~/.alfred/state.db",
        "artifact_dir": "~/.alfred/artifacts",
        "model": "qwen2.5:32b",
        "ollama_url": "http://127.0.0.1:11434",
        "fallback_after_s": 20,  # no qualified worker in this long -> do it here
        "supervisor_tick_s": 30,
    },
}

# Probed once at startup so `requires.software` can be enforced honestly
# rather than by hopeful assertion in a config file.
PROBE_BINARIES = [
    "git", "python3", "gcc", "make", "docker",
    "freecad", "openscad", "kicad", "mosquitto_pub", "pandoc",
]


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def probe_software() -> list[str]:
    return [b for b in PROBE_BINARIES if shutil.which(b)]


def load(path: str | os.PathLike | None = None) -> dict:
    cfg = DEFAULTS
    if path:
        with open(Path(path).expanduser(), "rb") as fh:
            cfg = _merge(cfg, tomllib.load(fh))

    cfg["worker"]["id"] = cfg["worker"].get("id") or socket.gethostname()
    if not cfg["worker"].get("software"):
        cfg["worker"]["software"] = probe_software()

    for key in ("state_db", "artifact_dir"):
        cfg["core"][key] = str(Path(cfg["core"][key]).expanduser())
    try:
        Path(cfg["core"]["artifact_dir"]).mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as exc:
        # Typically /mnt/alfred referenced before the NFS mount exists.
        # Fall back to a local dir and keep going — a missing shared mount
        # should degrade artifact sharing, not prevent Alfred from starting.
        fallback = Path("~/.alfred/artifacts").expanduser()
        print(f"warning: cannot use artifact_dir {cfg['core']['artifact_dir']} "
              f"({exc}); using {fallback} instead")
        cfg["core"]["artifact_dir"] = str(fallback)
        fallback.mkdir(parents=True, exist_ok=True)
    Path(cfg["core"]["state_db"]).parent.mkdir(parents=True, exist_ok=True)
    return cfg
