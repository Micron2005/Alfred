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

Two houses behind one catalog. On Linux the catalog runs apt, systemctl and
friends. On Windows -- native, or the owner's desktop where Alfred lives in
WSL -- it runs PowerShell and winget on the Windows side, because "is my PC
low on space?" means the C: drive, not the Linux VM. Under WSL the task may
say `"on": "linux"` to look at or change the VM instead.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import uuid
from pathlib import Path

from alfred import wsl
from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok, refuse

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


_OBSERVE_WORDS = {
    "disk": ("disk", "storage", "space", "df"),
    "memory": ("memory", "ram", "swap"),
    "cpu": ("cpu", "load", "uptime"),
    "services": ("services", "running", "daemon"),
    "failed": ("failed", "broken", "crash"),
    "packages": ("package", "upgradable", "updates", "apt"),
    "network": ("network", "ip ", "interface", "wifi", "ethernet"),
    "kernel": ("kernel", "uname", "os version"),
    "logs": ("log", "journal", "warning"),
}


def _infer_observation(prompt: str) -> str:
    """A planner that names the observation in prose but leaves `what` empty
    should not cost the owner a failed task."""
    low = prompt.lower()
    for what, words in _OBSERVE_WORDS.items():
        if any(w in low for w in words):
            return what
    return ""


@handler("os.observe")
async def os_observe(task: Task, cfg: dict) -> TaskResult:
    what = str(task.inputs.get("what", "")).lower() or _infer_observation(task.prompt)
    if what not in OBSERVATIONS:
        return refuse(task, f"unknown observation {what!r}; options: {', '.join(OBSERVATIONS)}")
    if on_windows(task.inputs):
        code, output = await _ps(WIN_OBSERVATIONS[what], timeout=120 if what == "packages" else 45)
        if code == 127:
            return refuse(task, output)
        if code != 0:
            return fail(task, f"Windows could not report {what} (exit {code}): {output[-300:]}")
        return _observed(task, what, output, "windows")
    argv = OBSERVATIONS[what]
    if shutil.which(argv[0]) is None:
        return refuse(task, f"{argv[0]} is not available on this machine")

    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    raw, _ = await proc.communicate()
    return _observed(task, what, raw.decode(errors="replace").strip(), "linux")


def _observed(task: Task, what: str, output: str, side: str) -> TaskResult:
    lines = output.splitlines()
    return ok(
        task,
        summary="\n".join(lines[:25]) or "(no output)",
        data={"what": what, "lines": len(lines), "truncated": len(lines) > 25, "on": side},
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

# The house, not the self: paths Alfred may never edit, approval or no.
# alfred/ is HIM — mind, memory, judgment. The machine layer (deploy, the
# server, the entrypoints) stays owner-only for now. Everything else is the
# house he keeps, and a butler may renovate the house — with approval, and
# every change a git commit so any renovation is one revert from undone.
_HOUSE_PROTECTED = (
    "alfred/", ".git", "deploy/", "requirements.txt",
    "run_server.py", "run_core.py", "run_worker.py", "run_node.py", "run_voice.py",
)


async def _act_house_edit(args: dict) -> tuple[int, str]:
    """Edit a file of the OS itself — shell, apps, configs, docs. Alfred is
    not his OS: he may change the house and must leave himself alone."""
    import os
    # The house is a SIBLING repository (~/micron-os). Alfred's own repo is
    # not merely protected -- it is not even the operand.
    repo = Path(str(args.get("repo") or (Path.home() / "micron-os")))
    raw = str(args.get("path") or "").strip()
    content = args.get("content")
    if not raw or content is None:
        return 1, "house_edit needs a path and the full new content"
    target = (repo / raw).resolve()
    try:
        rel = target.relative_to(repo.resolve())
    except ValueError:
        return 1, f"{raw} escapes the house; refused"
    rel_s = str(rel)
    for guard in _HOUSE_PROTECTED:
        if rel_s == guard.rstrip("/") or rel_s.startswith(guard):
            return 1, (f"{rel_s} is protected: Alfred may renovate the house, "
                       "never himself nor the machine layer")
    target.parent.mkdir(parents=True, exist_ok=True)
    before = target.read_text() if target.exists() else None
    target.write_text(str(content))
    note = (f"replaced {len(before)} chars with {len(str(content))}"
            if before is not None else f"created ({len(str(content))} chars)")
    desc = str(args.get("description") or "house edit")[:120]
    code, out = await _run(["git", "-C", str(repo), "add", rel_s])
    if code == 0:
        code, out = await _run([
            "git", "-C", str(repo),
            "-c", "user.name=Alfred", "-c", "user.email=alfred@micronos.local",
            "commit", "-m", f"Alfred (owner approved): {desc}"])
    commit_note = "committed; one git revert undoes it" if code == 0 else                   f"applied but commit failed: {out.strip()[:80]}"
    return 0, f"{rel_s}: {note}; {commit_note}"


async def _act_self_update(args: dict) -> tuple[int, str]:
    """Update Micron OS: pull what the owner pushed to the repo, then restart
    the core a few seconds later — delayed and detached, so this task's
    result is recorded before the process replaces itself.

    This is NOT self-editing (deliberately declined): Alfred authors nothing
    here. He fetches commits the owner already put on GitHub, fast-forward
    only — if the local clone has drifted, the pull refuses rather than
    merging surprises into a running butler."""
    import os
    # two repositories, one household: pull the butler, then the house
    butler = Path(str(args.get("repo") or Path(__file__).resolve().parents[3]))
    house = Path(str(args.get("house") or (Path.home() / "micron-os")))
    outs = []
    for name, repo in (("butler", butler), ("house", house)):
        if not (repo / ".git").exists():
            outs.append(f"{name}: no repository at {repo} (skipped)")
            continue
        code, out = await _run(["git", "-C", str(repo), "pull", "--ff-only"])
        if code != 0:
            return code, f"{name}: {out}"
        tail = out.strip().splitlines()[-1] if out.strip() else "ok"
        outs.append(f"{name}: {tail}")
    out = "; ".join(outs)
    if "Already up to date" in out and "Updating" not in out:
        return 0, "Already up to date -- nothing to apply."
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
    "house_edit":  (_act_house_edit, "edit the OS itself (shell/apps/configs/docs), never himself"),
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
    if action == "house_edit":
        return f"Edit the house: {args.get('path')} — {str(args.get('description') or '')[:80]}"
    return f"{action} {args}"


# ---------------------------------------------------------------------------
# Windows: the same catalog, through PowerShell and winget
# ---------------------------------------------------------------------------

def on_windows(inputs: dict) -> bool:
    """Native Windows always; WSL unless the task asks for the Linux VM."""
    if platform.system() == "Windows":
        return True
    if not (wsl.is_wsl() and shutil.which("powershell.exe")):
        return False
    args = inputs.get("args") if isinstance(inputs.get("args"), dict) else {}
    on = str(inputs.get("on") or args.get("on") or "").strip().lower()
    return on not in {"linux", "wsl", "ubuntu"}


_UTF8 = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ProgressPreference = 'SilentlyContinue'; "
_NO_WINGET = (
    "if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { "
    "[Console]::Out.Write('winget is not installed; get \"App Installer\" from the Microsoft Store'); "
    "exit 127 }; "
)


async def _ps(script: str, timeout: float = 45) -> tuple[int, str]:
    code, out = await wsl.powershell(_UTF8 + script, timeout=timeout)
    return code, _clean(out)


def _clean(text: str) -> str:
    """winget draws spinners and progress bars with carriage returns; keep
    what each line finally said and drop the animation frames."""
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.split("\r")[-1].rstrip()
        bare = line.strip()
        if not bare or bare in {"-", "\\", "|", "/"}:
            continue
        if set(bare) <= set("█▒░ ") or "KB /" in bare or "MB /" in bare:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


# Read-only, like their Linux counterparts. Same names so the planner and
# the owner never need to know which house they are asking about.
WIN_OBSERVATIONS: dict[str, str] = {
    "disk": (
        "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object { "
        "'{0}  {1:N0} GB, {2:N0} GB used, {3:N0} GB free ({4:N0}% used)' -f "
        "$_.DeviceID, ($_.Size/1GB), (($_.Size-$_.FreeSpace)/1GB), ($_.FreeSpace/1GB), "
        "(100*($_.Size-$_.FreeSpace)/[math]::Max($_.Size,1)) }"
    ),
    "memory": (
        "$o = Get-CimInstance Win32_OperatingSystem; $t = $o.TotalVisibleMemorySize/1MB; "
        "$f = $o.FreePhysicalMemory/1MB; "
        "'RAM  {0:N1} GB, {1:N1} GB used, {2:N1} GB free' -f $t, ($t-$f), $f; "
        "Get-CimInstance Win32_PageFileUsage | ForEach-Object { "
        "'Page file {0}  {1} MB of {2} MB used' -f $_.Name, $_.CurrentUsage, $_.AllocatedBaseSize }"
    ),
    "cpu": (
        "$o = Get-CimInstance Win32_OperatingSystem; $up = (Get-Date) - $o.LastBootUpTime; "
        "Get-CimInstance Win32_Processor | ForEach-Object { "
        "'{0}: {1}% load, {2} cores / {3} threads' -f $_.Name.Trim(), $_.LoadPercentage, "
        "$_.NumberOfCores, $_.NumberOfLogicalProcessors }; "
        "'up {0}d {1}h {2}m' -f $up.Days, $up.Hours, $up.Minutes"
    ),
    "services": (
        "$s = @(Get-Service | Where-Object Status -eq 'Running' | Sort-Object DisplayName); "
        "'{0} services running' -f $s.Count; "
        "$s | ForEach-Object { '{0}  ({1})' -f $_.DisplayName, $_.Name }"
    ),
    "failed": (
        "$s = @(Get-CimInstance Win32_Service -Filter \"StartMode='Auto' AND State<>'Running'\" | "
        "Where-Object { $_.ExitCode -ne 0 }); "
        "if ($s.Count -eq 0) { 'no failed services' } else { $s | ForEach-Object { "
        "'{0}  ({1}) stopped, exit code {2}' -f $_.DisplayName, $_.Name, $_.ExitCode } }"
    ),
    "packages": (
        _NO_WINGET
        + "winget upgrade --accept-source-agreements --disable-interactivity | Out-String -Width 200"
    ),
    "network": (
        "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne '127.0.0.1' } | "
        "ForEach-Object { '{0}  {1}/{2}' -f $_.InterfaceAlias, $_.IPAddress, $_.PrefixLength }"
    ),
    "kernel": (
        "$o = Get-CimInstance Win32_OperatingSystem; "
        "'{0} {1} (build {2}), {3}' -f $o.Caption, $o.Version, $o.BuildNumber, $o.OSArchitecture"
    ),
    "logs": (
        "Get-WinEvent -FilterHashtable @{LogName='System','Application'; Level=1,2,3} "
        "-MaxEvents 30 -ErrorAction SilentlyContinue | ForEach-Object { "
        "'{0:MM-dd HH:mm} {1}  {2}: {3}' -f $_.TimeCreated, $_.LevelDisplayName, "
        "$_.ProviderName, ((\"$($_.Message)\" -split \"`n\")[0]).Trim() }"
    ),
}

# What the owner says -> the winget id it means. Anything else goes to
# winget as typed (its own ids and monikers, e.g. "vscode", work too).
WINGET_IDS = {
    "vscode": "Microsoft.VisualStudioCode", "vs code": "Microsoft.VisualStudioCode",
    "visual studio code": "Microsoft.VisualStudioCode", "code": "Microsoft.VisualStudioCode",
    "chrome": "Google.Chrome", "google chrome": "Google.Chrome",
    "firefox": "Mozilla.Firefox", "git": "Git.Git", "7zip": "7zip.7zip", "7-zip": "7zip.7zip",
    "vlc": "VideoLAN.VLC", "discord": "Discord.Discord", "spotify": "Spotify.Spotify",
    "steam": "Valve.Steam", "obs": "OBSProject.OBSStudio", "obs studio": "OBSProject.OBSStudio",
    "node": "OpenJS.NodeJS.LTS", "nodejs": "OpenJS.NodeJS.LTS", "node.js": "OpenJS.NodeJS.LTS",
    "notepad++": "Notepad++.Notepad++", "zoom": "Zoom.Zoom", "slack": "SlackTechnologies.Slack",
    "powertoys": "Microsoft.PowerToys", "windows terminal": "Microsoft.WindowsTerminal",
    "blender": "BlenderFoundation.Blender", "gimp": "GIMP.GIMP", "audacity": "Audacity.Audacity",
}

# The ground Alfred stands on, on Windows: WSL (his brain lives there),
# PowerShell (his only way in), winget itself, his model and his runtime.
PROTECTED_WIN_PACKAGES = ("wsl", "ubuntu", "powershell", "appinstaller", "winget",
                          "ollama", "python")
PROTECTED_WIN_SERVICES = {
    "wslservice", "lxssmanager", "vmcompute", "hns", "sshd", "rpcss", "dcomlaunch",
    "rpceptmapper", "winmgmt", "eventlog", "dhcp", "dnscache", "nlasvc", "netprofm",
    "mpssvc", "bfe", "windefend", "termservice", "lanmanworkstation", "plugplay",
    "power", "profsvc", "samss", "lsm", "schedule", "wuauserv", "trustedinstaller",
}


def winget_id(package: str) -> str:
    return WINGET_IDS.get(package.strip().lower(), package.strip())


def _deny_reason_windows(action: str, args: dict) -> str | None:
    if action == "service_ctl":
        name = str(args.get("name", "")).strip().lower()
        if name in PROTECTED_WIN_SERVICES and args.get("verb") in {"stop", "disable"}:
            return f"{args.get('name')} is protected; the owner must never be locked out"
    if action == "pkg_remove":
        pkg = winget_id(str(args.get("package", ""))).lower()
        hit = next((p for p in PROTECTED_WIN_PACKAGES if p in pkg), None)
        if hit:
            return f"removing {args.get('package')} would cut Alfred off from this machine"
    if action == "setting_set":
        return "desktop settings are GNOME-only; on Windows ask Alfred's hands instead"
    return None


async def _winget(verb: str, target: list[str], timeout: int = 900) -> tuple[int, str]:
    quoted = " ".join(wsl_quote(t) for t in target)
    code, out = await _ps(
        _NO_WINGET
        + f"winget {verb} {quoted} --silent --accept-source-agreements "
        "--disable-interactivity"
        + (" --accept-package-agreements" if verb in {"install", "upgrade"} else "")
        + " | Out-String -Width 200; exit $LASTEXITCODE",
        timeout=timeout,
    )
    return code, out


def wsl_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _win_pkg_target(package: str) -> list[str]:
    pkg = winget_id(package)
    if not pkg:
        raise KeyError("package")
    # Dotted names are winget ids; match them exactly so "Git.Git" never
    # turns into some other package that merely mentions git.
    return ["--id", pkg, "--exact"] if "." in pkg and " " not in pkg else [pkg]


async def _win_pkg_install(args: dict) -> tuple[int, str]:
    return await _winget("install", _win_pkg_target(str(args["package"])))

async def _win_pkg_remove(args: dict) -> tuple[int, str]:
    return await _winget("uninstall", _win_pkg_target(str(args["package"])))

async def _win_pkg_upgrade(args: dict) -> tuple[int, str]:
    if args.get("package"):
        return await _winget("upgrade", _win_pkg_target(str(args["package"])))
    return await _winget("upgrade", ["--all"], timeout=3600)


_SERVICE_VERBS = {
    "start": "Start-Service -InputObject $s -ErrorAction Stop",
    "stop": "Stop-Service -InputObject $s -Force -ErrorAction Stop",
    "restart": "Restart-Service -InputObject $s -Force -ErrorAction Stop",
    "enable": "Set-Service -Name $s.Name -StartupType Automatic -ErrorAction Stop",
    "disable": "Set-Service -Name $s.Name -StartupType Disabled -ErrorAction Stop",
}


async def _win_service_ctl(args: dict) -> tuple[int, str]:
    """Services need an administrator: one UAC prompt per approved change,
    like the bus door. The name is resolved (service or display name) and
    re-checked against the protected list on the Windows side, so a display
    name cannot smuggle a protected service past the check above."""
    verb = str(args["verb"])
    if verb not in _SERVICE_VERBS:
        return 1, f"verb {verb!r} not in catalog"
    name = wsl_quote(str(args["name"]))
    protected = ",".join(wsl_quote(n) for n in sorted(PROTECTED_WIN_SERVICES))
    find = (
        f"$s = Get-Service -Name {name} -ErrorAction SilentlyContinue; "
        f"if (-not $s) {{ $s = Get-Service -DisplayName {name} -ErrorAction SilentlyContinue }}; "
        f"if (-not $s) {{ [Console]::Out.Write('no Windows service called ' + {name}); exit 2 }}; "
        "$s = @($s)[0]; "
    )
    if verb in {"stop", "disable"}:
        find += (f"if (@({protected}) -contains $s.Name.ToLower()) {{ "
                 "[Console]::Out.Write($s.Name + ' is protected'); exit 3 }; ")
    log_name = f"alfred-{uuid.uuid4().hex}.txt"
    inner = (
        "$ErrorActionPreference = 'Stop'; "
        f"$log = Join-Path $env:TEMP '{log_name}'; "
        f"try {{ {find}{_SERVICE_VERBS[verb]}; "
        "$s = Get-Service -Name $s.Name; "
        "Set-Content -Path $log -Value ('{0} ({1}): {2}, starts {3}' -f "
        "$s.DisplayName, $s.Name, $s.Status, $s.StartType); exit 0 } "
        "catch { Set-Content -Path $log -Value ($_ | Out-String); exit 1 }"
    )
    outer = (
        find
        + "$p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
        f"-ArgumentList '-NoProfile','-EncodedCommand','{wsl.encode(inner)}'; "
        f"$log = Join-Path $env:TEMP '{log_name}'; "
        "if (Test-Path $log) { Get-Content $log -Raw; Remove-Item $log }; exit $p.ExitCode"
    )
    code, out = await _ps(outer, timeout=180)
    if code != 0 and ("canceled" in out.lower() or "cancelled" in out.lower()):
        return code, "Windows asked for administrator permission and it was declined"
    return code, out


WIN_ACTIONS = {
    "pkg_install": _win_pkg_install,
    "pkg_remove": _win_pkg_remove,
    "pkg_upgrade": _win_pkg_upgrade,
    "service_ctl": _win_service_ctl,
}


@handler("os.apply")
async def os_apply(task: Task, cfg: dict) -> TaskResult:
    action = str(task.inputs.get("action", ""))
    args = task.inputs.get("args") or {}

    if action not in ACTIONS:
        return refuse(task, f"'{action}' is not in the catalog: {', '.join(ACTIONS)}")

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

    windows = on_windows(task.inputs) and action in (*WIN_ACTIONS, "setting_set")
    reason = _deny_reason_windows(action, args) if windows else _deny_reason(action, args)
    if reason is not None:
        return refuse(task, f"denied: {reason}")

    fn = WIN_ACTIONS[action] if windows else ACTIONS[action][0]
    try:
        code, output = await fn(args)
    except KeyError as exc:
        return fail(task, f"{action} is missing argument {exc}")
    except Exception as exc:
        return fail(task, f"{action} raised {type(exc).__name__}: {exc}")

    tail = "\n".join(output.splitlines()[-6:])
    if code != 0:
        hint = ""
        if windows and code == 127:
            return refuse(task, output)
        if not windows and "sudo" in output and ("password" in output.lower() or "sudo:" in output):
            hint = (" — the worker lacks scoped sudo; run "
                    "./deploy/install-alfredos.sh sudo on this machine")
        return fail(task, f"{describe_action(action, args)} failed (exit {code}): {tail}{hint}")

    return ok(
        task,
        summary=f"{describe_action(action, args)} — done. {tail}",
        data={"action": action, "exit_code": code, "on": "windows" if windows else "linux"},
    )
