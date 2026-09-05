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

    # desktop
    python run_server.py --config configs/desktop.toml
    #   -> "bus at nats://192.168.1.50:4222; other machines: python run_node.py --bus ..."

    # zenbook (or any laptop): its config says what it offers
    python run_worker.py --config configs/zenbook.toml          # url = "auto"

    # anything else, no config: announces itself, you give it a job
    python run_node.py                                          # finds the beacon
    python run_node.py --bus nats://192.168.1.50:4222           # if broadcast is blocked

The worker only needs to reach the desktop's port 4222 (open it in the
desktop firewall); the desktop never connects to the worker. A worker started
before the desktop waits and says so once in a while. `/nodes` in the REPL, or
`GET /api/status`, shows who is online; `POST /api/nodes/<id>/assign` with
`{"name": ..., "capabilities": [...]}` enrols an announcing machine. `[bus]
kind = "local"` is single-machine only: nothing can join it.
