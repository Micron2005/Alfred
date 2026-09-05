# Alfred

The butler. His mind (core, planner, memory), his hands (worker handlers),
his senses (capabilities, probe), and his runners. This repository is HIM --
and only him.

His house is a separate repository: **MicronOS** (cloned locally to ~/micron-os) -- the shell, the apps,
the configs, the deploy. He may renovate the house (house_edit,
owner-approved, git-revertible). He may never edit this repository:
ALFRED IS NOT HIS OS, and the code layout now says so.

Run him: see micron-os/DEPLOY.md -- the house fetches its butler.

## Other machines (the Zenbook, a Pi, a spare laptop)

The desktop is the brain. It hosts the bus (`nats-server`, installed by
`install.sh`, started and stopped by `run_server.py`/`run_core.py`) and
broadcasts its address on the LAN. Nothing else plans, remembers or answers;
other machines are hands that pull work from the bus and send results back.

    # desktop: start.sh / start.bat -- runs the server and opens his page
    python run_server.py --config configs/desktop.toml --open

    # any other machine: join.sh / join.bat -- announces itself, you give it
    # a job from the page ("Give it a job"); no config file needed
    python run_node.py                                          # finds the beacon
    python run_node.py --bus nats://192.168.1.50:4222           # if broadcast is blocked

    # or a machine with a config that says what it offers
    python run_worker.py --config configs/zenbook.toml          # url = "auto"

The worker only needs to reach the desktop's port 4222 (open it in the
desktop firewall); the desktop never connects to the worker. A worker started
before the desktop waits and says so once in a while.

**Windows, with Alfred inside WSL.** The bus then lives in a Linux VM with a
private address, and Windows must forward the port in (`netsh interface
portproxy` plus a firewall rule, both needing administrator rights; the VM's
address also changes on reboot). The page checks this and shows **Open the
door** when it is not right; one UAC prompt and it is done, and the join
command it prints uses the Windows address. LAN discovery (`--bus auto`) does
not cross that boundary, so a laptop is told the address explicitly. With
WSL's mirrored networking none of this is needed and the page says so.

**Eyes.** On the desktop, Alfred glances at the screen every 20 s with the
local vision model (`ollama pull llava:7b`; under WSL the frame comes from the
Windows desktop via PowerShell). The page shows the indicator at all times and
the switch -- **Look away** / **Eyes on**, also as spoken commands -- and
**What's on my screen?** for a fresh look. Frames go to Ollama on the same
machine and nowhere else; seeing grants no permission to act, and other
machines' screens are looked at only when asked (`screen.view`). See SIGHT.md.

The page at http://127.0.0.1:8710 (`panel/index.html`, served when the Micron
OS shell is not installed, always at `/panel`) is the whole control surface:
chat, which machines are online, "Give it a job" for a new one, approve or
decline parked changes, the join command to copy, the lock. The REPL
(`run_core.py`, `/nodes`, `/assign`) and the API (`GET /api/status`,
`POST /api/nodes/<id>/assign`) do the same things for scripts. `[bus] kind =
"local"` is single-machine only: nothing can join it.
