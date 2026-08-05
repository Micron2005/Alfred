# What goes on which machine

Same repository on every box. `git clone`, then run one command. What differs
is which entry point and which config.

---

## Desktop — Alfred himself

The only machine that runs `run_core.py`. The persona, the memory and the
user-facing voice exist here and nowhere else.

```bash
git clone <your repo> alfred && cd alfred
pip install psutil
ollama pull qwen2.5:7b
mkdir -p ~/.alfred

python run_core.py --config configs/desktop.toml --new-project "Fabricator Mk I"
```

Runs Alfred core **and** a fallback worker in the same process. Ships with
`bus.kind = "local"`, so this alone is a complete working system — no server,
no network, nothing else to install. Do this first.

Also handles: Onshape (`cad.*`), and FreeCAD if you install it here too.

**When the Zenbook joins**, edit `configs/desktop.toml`:

```toml
[bus]
kind = "nats"
url  = "nats://alfredpi.local:4222"

[worker]
claim_delay_s = 15        # was 0 — stop the desktop winning every race
```

---

## Raspberry Pi — control plane + hardware + Onshape

Everything stateful lives here because it is the only always-on box.

```bash
# infrastructure
nats-server -js -sd /var/lib/nats &
sudo apt install mosquitto
docker run -d -p 8888:8080 searxng/searxng

# shared artifact store — every machine mounts this at /mnt/alfred
sudo mkdir -p /srv/alfred && sudo chown $USER /srv/alfred
echo "/srv/alfred *(rw,sync,no_subtree_check)" | sudo tee -a /etc/exports
sudo exportfs -ra

# the worker
pip install nats-py paho-mqtt psutil
export ONSHAPE_ACCESS_KEY=... ONSHAPE_SECRET_KEY=...
python run_worker.py --config configs/pi.toml
```

Onshape lives here because it is a cloud API — no CAD software, no GPU, just
HTTPS. No reason to spend the Zenbook's cycles on an HTTP request.

---

## Zenbook — engineering + FreeCAD

```bash
sudo mount -t nfs alfredpi.local:/srv/alfred /mnt/alfred
pip install nats-py psutil
sudo apt install freecad          # provides freecadcmd
ollama pull qwen2.5-coder:7b

python run_worker.py --config configs/zenbook.toml
```

Offers `code.write`, `code.test`, `calc.engineering`, `cad.generate`,
`cad.inspect`. The only machine besides the desktop running its own model.

FreeCAD is CPU-bound, which is why generation lives here and not on the Pi.
Without FreeCAD installed the `cad.*` capabilities are simply not advertised
and nothing breaks.

---

## MacBook — research

```bash
sudo mount -t nfs alfredpi.local:/srv/alfred /mnt/alfred
pip install nats-py httpx pypdf psutil

python run_worker.py --config configs/macbook.toml
```

Runs no model. `configs/macbook.toml` points `ollama_url` at the desktop and
uses **the same model name** on purpose — ask an 8GB card for a second model
and Ollama evicts and reloads on every request.

---

## Chromebook — dashboard, light docs

Enable Linux (Crostini) first. If it is blocked by policy, skip the worker
entirely; the dashboard is only a browser tab.

```bash
sudo mount -t nfs alfredpi.local:/srv/alfred /mnt/alfred
pip install nats-py
python run_worker.py --config configs/chromebook.toml
```

Holds no state — it is off whenever the lid is closed.

---

## Any new machine — `run_node.py`

No config file, no capability list, no name.

```bash
git clone <your repo> alfred && cd alfred
pip install nats-py psutil
python run_node.py --bus nats://alfredpi.local:4222
```

That is the whole setup. See it first without committing to anything:

```bash
python run_node.py --probe-only
```

```
old-dell (Linux 6.8 x86_64): 8 cores, 16.0GB RAM, no GPU, has docker
  id        : old-dell-4c1a9f
  binaries  : git, python3, gcc, make, docker
  packages  : httpx, pypdf, psutil
  could run : code.test, research.document, research.web, ...
```

### Then, in Alfred's console

```
> /nodes
  [old-dell-4c1a9f] old-dell (Linux 6.8 x86_64): 8 cores, 16.0GB RAM, no GPU
      UNASSIGNED

> /adopt old-dell-4c1a9f the old dell in the basement, always plugged in

  proposed name : basement-dell
  capabilities  : research.web, research.document
  concurrency   : 2
  reason        : plenty of RAM, no GPU, always on — good for background reading
  accept? [y/N] y

  enrolled basement-dell: research.web, research.document
```

The node adopts that within one heartbeat. No restart.

Alfred also raises it himself: his supervisor loop notices unassigned
machines and mentions them on your next turn rather than interrupting.

### Setting it yourself

```
> /assign old-dell-4c1a9f basement-dell research.web,research.document
```

Both paths validate against the probed hardware, and refusals say why:

```
enrolled garage-box: code.test
  (declined: hw.mqtt (missing paho); cad.generate (missing freecadcmd))
```

**A model can never assign a capability a machine cannot perform.** The
eligible set is computed from `alfred/capabilities.py` against the probe; the
model only ranks within it. Assigning FreeCAD work to a box without FreeCAD
is not a mistake it can make, because that option is never on the table.

### Console commands

```
/nodes                            machines seen, assigned and not
/probe <node_id>                  full hardware profile
/adopt <node_id> [hint]           Alfred proposes a name and workload
/assign <node_id> <name> <caps>   set it yourself
```

Enrollment goes through commands rather than free conversation on purpose. It
changes what the whole network will do, and that should not depend on a 7B
model correctly parsing intent out of a sentence. Alfred still proposes — the
command is how a proposal becomes real.

---

## Re-enrollment is not needed

Assignments are keyed to a stable node id in `~/.alfred/node_id` and stored
durably on Alfred's side. A machine that reboots, changes IP, or is unplugged
for a month comes back as itself and resumes its role.

To change a role, `/assign` it again.
