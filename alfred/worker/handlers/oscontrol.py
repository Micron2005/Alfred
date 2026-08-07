"""OS control. The capability that makes Micron OS an OS and not a chat window.

Runs on every enrolled machine, so "update the Zenbook" routes exactly like
any other work. Three tiers, strictly enforced:

    os.observe  read-only, executes immediately. Disk, memory, services,
                packages, logs. Observation is free.

    os.apply    mutating, and NEVER executes on Alfred's own judgment. The
                core parks these as pending actions; only the owner's
                explicit approval (shell button or /approve) dispatches
                them. The handler additionally refuses any task that does
                not carry the approval mark, so even a bug in the core
                cannot skip the gate.

    denied      a short list of operations no approval can unlock, because
                the owner must never be locked out of his own house: the
                alfred services themselves, ssh, systemd, sudo, the .ssh
                directory, Alfred's own state database.

Actions are a closed catalog, not free-form shell. A language model given
arbitrary sudo is a machine that will eventually delete something loved;
a language model choosing from a catalog can propose only what the catalog
can do.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok

# ---------------------------------------------------------------------------
# The lines no approval crosses
# ---------------------------------------------------------------------------

PROTECTED_SERVICES = {
    # Alfred must not be able to kill or disable himself or his hands;
    # a butler who can fire himself mid-sentence is a support ticket.
    "alfred-core", "alfred-worker",
    # The remote door and the ground the house stands on.
    "ssh", "sshd", "systemd-logind", "NetworkManager", "systemd-networkd",
}
PROTECTED_PACKAGES = {
    "systemd", "sudo", "openssh-server", "network-manager",
    "python3", "linux-image-generic", "grub-efi-amd64", "grub-pc",
}
PROTECTED_PATHS = (".ssh", ".alfred/state.db", ".gnupg")


def _deny_reason(action: str, args: dict) -> str | None:
    """Return why this is forbidden, or None if it may proceed to approval."""
    if action in {"service_ctl"}:
        name = str(args.get("name", "")).removesuffix(".service")
        base = name.split("@")[0]
        if base in PROTECTED_SERVICES and args.get("verb") in {"stop", "disable", "mask"}:
            return f"{name} is protected; the owner must never be locked out"
    if action == "pkg_remove":
        if str(args.get("package", "")) in PROTECTED_PACKAGES:
            return f"removing {args.get('package')} could brick the machine"
    if action == "file_write":
        target = Path(str(args.get("path", ""))).expanduser()
        home = Path.home()
        try:
            rel = target.resolve().relative_to(home)
        except ValueError:
            return "file writes are limited to the home directory"
        if any(str(rel).startswith(p) for p in PROTECTED_PATHS):
            return f"~/{rel} is protected"
    return None


# ---------------------------------------------------------------------------
# os.observe — read-only, immediate
# ---------------------------------------------------------------------------

OBSERVATIONS: dict[str, list[str]] = {
    "disk":     ["df", "-h", "--output=target,size,used,avail,pcent"],
    "memory":   ["free", "-h"],
    "cpu":      ["uptime"],
    "services": ["systemctl", "list-units", "--type=service", "--state=running",
                 "--no-pager", "--no-legend"],
    "failed":   ["systemctl", "--failed", "--no-pager", "--no-legend"],
    "packages": ["apt", "list", "--upgradable"],
    "network":  ["ip", "-brief", "addr"],
    "kernel":   ["uname", "-a"],
    "logs":     ["journalctl", "-p", "warning", "-n", "30", "--no-pager"],
}


@handler("os.observe")
async def os_observe(task: Task, cfg: dict) -> TaskResult:
    what = str(task.inputs.get("what", "")).lower()
    if what not in OBSERVATIONS:
        return fail(task, f"unknown observation {what!r}; options: {', '.join(OBSERVATIONS)}")
    argv = OBSERVATIONS[what]
    if shutil.which(argv[0]) is None:
        return fail(task, f"{argv[0]} is not available on this machine")

    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    raw, _ = await proc.communicate()
    output = raw.decode(errors="replace").strip()
    lines = output.splitlines()
    return ok(
        task,
        summary="\n".join(lines[:25]) or "(no output)",
        data={"what": what, "lines": len(lines), "truncated": len(lines) > 25},
    )


# ---------------------------------------------------------------------------
# os.apply — mutating, approval-gated, catalog-only
# ---------------------------------------------------------------------------

async def _run(argv: list[str], sudo: bool = False, timeout: int = 600) -> tuple[int, str]:
    if sudo:
        argv = ["sudo", "-n", *argv]  # -n: fail rather than hang on a password prompt
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, raw.decode(errors="replace").strip()


async def _act_pkg_install(args: dict) -> tuple[int, str]:
    return await _run(["apt-get", "install", "-y", str(args["package"])], sudo=True)

async def _act_pkg_remove(args: dict) -> tuple[int, str]:
    return await _run(["apt-get", "remove", "-y", str(args["package"])], sudo=True)

async def _act_pkg_upgrade(args: dict) -> tuple[int, str]:
    code, out = await _run(["apt-get", "update"], sudo=True)
    if code != 0:
        return code, out
    return await _run(["apt-get", "upgrade", "-y"], sudo=True, timeout=1800)

async def _act_service_ctl(args: dict) -> tuple[int, str]:
    verb = str(args["verb"])
    if verb not in {"start", "stop", "restart", "enable", "disable"}:
        return 1, f"verb {verb!r} not in catalog"
    return await _run(["systemctl", verb, str(args["name"])], sudo=True)

async def _act_setting_set(args: dict) -> tuple[int, str]:
    schema = str(args["schema"])
    if not schema.startswith("org.gnome."):
        return 1, "only org.gnome.* settings are in the catalog"
    return await _run(["gsettings", "set", schema, str(args["key"]), str(args["value"])])

async def _act_self_update(args: dict) -> tuple[int, str]:
    """Update Micron OS: pull what the owner pushed to the repo, then restart
    the core a few seconds later — delayed and detached, so this task's
    result is recorded before the process replaces itself.

    This is NOT self-editing (deliberately declined): Alfred authors nothing
    here. He fetches commits the owner already put on GitHub, fast-forward
    only — if the local clone has drifted, the pull refuses rather than
    merging surprises into a running butler."""
    import os
    repo = Path(str(args.get("repo") or Path(__file__).resolve().parents[3]))
    if not (repo / ".git").exists():
        return 1, f"{repo} is not a git repository"
    code, out = await _run(["git", "-C", str(repo), "pull", "--ff-only"])
    if code != 0:
        return code, out
    if "Already up to date" in out:
        return 0, "Already up to date — nothing to apply."
    if args.get("restart", True):
        unit = str(args.get("unit") or f"alfred-core@{os.environ.get('USER', 'root')}")
        # detached: survives this process; scoped sudo covers systemctl
        subprocess.Popen(
            ["sh", "-c", f"sleep 3; sudo -n systemctl restart {unit}"],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        out += "\n(restarting the core in 3 seconds to apply)"
    return 0, out


async def _act_file_write(args: dict) -> tuple[int, str]:
    path = Path(str(args["path"])).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(args.get("content", "")))
    return 0, f"wrote {len(str(args.get('content', '')))} chars to {path}"

ACTIONS = {
    "pkg_install": (_act_pkg_install, "install a package (apt)"),
    "pkg_remove":  (_act_pkg_remove, "remove a package (apt)"),
    "pkg_upgrade": (_act_pkg_upgrade, "apt update && upgrade"),
    "service_ctl": (_act_service_ctl, "start/stop/restart/enable/disable a service"),
    "setting_set": (_act_setting_set, "change a GNOME desktop setting"),
    "file_write":  (_act_file_write, "write a file under the home directory"),
    "self_update": (_act_self_update, "pull the owner's pushed updates and restart"),
}


def describe_action(action: str, args: dict) -> str:
    """One honest line the owner reads before approving."""
    if action == "pkg_install":
        return f"Install package '{args.get('package')}'"
    if action == "pkg_remove":
        return f"REMOVE package '{args.get('package')}'"
    if action == "pkg_upgrade":
        return "Update package lists and upgrade all packages"
    if action == "service_ctl":
        return f"{str(args.get('verb', '')).capitalize()} service '{args.get('name')}'"
    if action == "setting_set":
        return f"Set {args.get('schema')} {args.get('key')} = {args.get('value')}"
    if action == "file_write":
        return f"Write file {args.get('path')}"
    if action == "self_update":
        return "Update Micron OS from the repository and restart the core"
    return f"{action} {args}"


@handler("os.apply")
async def os_apply(task: Task, cfg: dict) -> TaskResult:
    action = str(task.inputs.get("action", ""))
    args = task.inputs.get("args") or {}

    if action not in ACTIONS:
        return fail(task, f"'{action}' is not in the catalog: {', '.join(ACTIONS)}")

    # The gate, enforced at the last possible moment as well as in the core.
    # Approval is granted by the owner, marked by the core at dispatch time.
    if not task.inputs.get("_approved"):
        return TaskResult(
            task_id=task.id, worker_id="", status="rejected",
            error="os.apply without owner approval; this task should have been "
                  "parked as a pending action, not dispatched",
        )
    if not task.idempotency_key:
        return fail(task, "refusing an OS change without an idempotency_key")

    reason = _deny_reason(action, args)
    if reason is not None:
        return fail(task, f"denied: {reason}")

    fn, _ = ACTIONS[action]
    try:
        code, output = await fn(args)
    except KeyError as exc:
        return fail(task, f"{action} is missing argument {exc}")
    except Exception as exc:
        return fail(task, f"{action} raised {type(exc).__name__}: {exc}")

    tail = "\n".join(output.splitlines()[-6:])
    if code != 0:
        hint = ""
        if "sudo" in output and ("password" in output.lower() or "sudo:" in output):
            hint = (" — the worker lacks scoped sudo; run "
                    "./deploy/install-alfredos.sh sudo on this machine")
        return fail(task, f"{describe_action(action, args)} failed (exit {code}): {tail}{hint}")

    return ok(
        task,
        summary=f"{describe_action(action, args)} — done. {tail}",
        data={"action": action, "exit_code": code},
    )
