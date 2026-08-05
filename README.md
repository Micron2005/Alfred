# Alfred

One coordinator, many hands.

## What goes on which machine

**One repository. Cloned to every machine. Identical code everywhere.** What
differs is a config file and which entry point you launch.

| Machine | Runs | Config | Also needs |
|---|---|---|---|
| Desktop | `run_core.py` + built-in worker | `configs/desktop.toml` | Ollama, 7B model |
| Zenbook | `run_worker.py` | `configs/zenbook.toml` | Ollama coder model, FreeCAD |
| MacBook | `run_worker.py` | `configs/macbook.toml` | `httpx`, `pypdf` |
| Raspberry Pi | `run_worker.py` | `configs/pi.toml` | `nats-server`, `mosquitto`, NFS export |
| Chromebook | browser, optionally `run_worker.py` | `configs/chromebook.toml` | Crostini for the worker |
| **Anything new** | **`run_node.py`** | **none** | just `--bus <url>` |

Step-by-step install commands per machine are in **DEPLOY.md**.

`run_node.py` needs no config and no capability list. The machine probes its
own hardware, announces itself, and waits; Alfred mentions it on your next
turn and you tell him what it is. He proposes a name and a workload filtered
against what the box can actually do, and the node adopts it live without
restarting. Assignments persist against a stable node id, so a machine that
reboots or moves networks resumes its role rather than re-enrolling.

Deploy is `git pull` on all five. There is no per-machine branch, no renamed
copy, and no file you have to remember to edit in two places.

`run_core.py` is the single exception: it runs on the desktop and nowhere
else. That is what "there is only one Alfred" means operationally — the
persona, the memory and the user-facing voice exist in exactly one process.

## Configs declare capabilities, not identities

The Zenbook's config does not say `role = "atlas"`. It says:

```toml
capabilities = ["code.write", "code.test", "calc.engineering"]
```

Move that file to a stronger machine and the engineering work follows it,
with no code change and no reference to update. Add a second machine with
the same capabilities and they share the load automatically. This is the
mechanism behind "workers are not permanently tied to one computer" — take
it out and that requirement becomes a comment rather than a fact.

A worker advertises the intersection of what its config declares and what it
can actually do. Missing library, missing capability — logged at startup, not
discovered halfway through a job.

## Build order

**Phase 1 — desktop only. No server, no network, nothing to install.**

```bash
git clone <your repo> && cd alfred
ollama pull qwen2.5:32b
python run_core.py --config configs/desktop.toml --new-project "Fabricator Mk I"
```

`bus.kind = "local"` runs Alfred and a worker in one process. Planning,
verification, state and synthesis all work here. Get this feeling right
before adding a single machine — the distributed version cannot be better
than this, only faster.

**Phase 2 — the Pi becomes the control plane.**

```bash
# on the Pi
nats-server -js -sd /var/lib/nats
python run_worker.py --config configs/pi.toml
```

Switch the desktop's `bus.kind` to `"nats"`. Nothing above the bus interface
changes. Then bring up the Zenbook and MacBook the same way.

**Phase 3 onward** — telemetry-based scoring (already in `scheduler.py`,
just needs `psutil` on each node), then the dashboard. The dashboard is an
observability tool for a system that has to exist first.

## Layout

```
alfred/
  contracts.py       the protocol. change here = redeploy everywhere
  config.py          per-machine capability declaration
  llm.py             Ollama client; note WORKER_SYSTEM vs Alfred's persona
  bus/
    base.py          the transport seam
    local.py         phase 1, and the permanent fallback path
    nats_bus.py      phase 2
  worker/
    runtime.py       identical on every machine
    handlers/        one function per capability
  core/              DESKTOP ONLY
    alfred.py        conversation loop + supervisor loop
    state.py         projects, decisions, questions, task ledger
    planner.py       request -> task DAG, and mechanical verification
    scheduler.py     capability match, load scoring, local fallback
    persona.md       Alfred's voice. exists in exactly one place
```

## Three rules worth not breaking

**Workers never write to state.** They receive a scoped context as task input
and return an artifact. Two components with independent memories will
eventually disagree about what was decided, and you will not notice until it
matters.

**Workers never use the persona.** They run under `llm.WORKER_SYSTEM`: no
name, no memory, no addressing a user. The moment a worker writes "I've
completed the analysis, sir", you have four Alfreds.

**Summaries flow downstream, artifacts do not.** In `_run_graph`, a task
inherits its dependencies' `summary` fields and nothing else. Alfred's
context window is the scarcest resource in the system; a worker exists to
read a lot and return a little, and passing bulk output between tasks undoes
the only thing it was there to do.

## Inference layout (8GB desktop)

An 8GB card fits a 7B model at Q4 and nothing larger without spilling to CPU.
That shapes three decisions:

- **Alfred runs `qwen2.5:7b`.** Honest expectation: a 7B model is a mediocre
  planner. It will emit malformed JSON and invent capabilities. The planner
  validates every field, feeds the specific error back, and retries three
  times before degrading to "handled without delegation" — so this fails
  softly rather than producing nonsense.
- **Only the Zenbook runs its own model.** The MacBook and Chromebook point
  `ollama_url` at the desktop, and use the *same model name* on purpose. Ask
  the desktop for a second model and Ollama evicts and reloads on every
  request; on 8GB that thrash costs far more than the inference.
- **The desktop worker runs `concurrency = 1`**, because it shares the GPU
  with Alfred himself.

If planning quality becomes the bottleneck, `[core] provider = "openai"` in
`configs/desktop.toml` routes only planning and synthesis to a hosted
endpoint. Every worker stays local. Set the key in the environment.

## CAD: both, deliberately

They do opposite halves of the job, so both are wired in.

| | Onshape | FreeCAD |
|---|---|---|
| Capabilities | `cad.measure`, `cad.variables`, `cad.export`, `cad.evaluate` | `cad.generate`, `cad.inspect` |
| Runs on | Pi (cloud API, no local compute) | Zenbook, desktop (CPU-bound kernel) |
| Can create geometry | No | Yes |
| Drives a model you built | Yes | No |
| Privacy | Free tier documents are public | Entirely local |

Use Onshape for the arm itself — you model it, Alfred sets a dimension and
reads the real mass back. Use FreeCAD for the parts that are tedious rather
than interesting: mounting plates, spacers, shaft adapters, brackets. Those
Alfred can produce outright.

`cad.generate` never asks the model to write a whole FreeCAD script — that
fails constantly at 7B. It asks for one function returning a `Part.Shape`,
inside a harness that owns the imports, the export and the validation. Then
the result is checked mechanically (script runs, shape valid, volume
non-zero, exactly one solid) and any failure goes back as the real traceback.
Budget is 5 attempts, tunable via `cad_attempts`.

Install: `sudo apt install freecad` (provides `freecadcmd`). Absent it, the
capability simply is not advertised.

Not built: FEM via CalculiX. It is the obvious next capability and a real
one — stress analysis on a bracket is exactly the kind of check worth having
— but it is fiddly enough that shipping it untested would be worse than
leaving the hook open.

## Onshape

`cad.*` is an HTTPS client — no local CAD software, no GPU — so it runs on
the Pi. Set `ONSHAPE_ACCESS_KEY` and `ONSHAPE_SECRET_KEY` in the worker's
environment (never in the config; configs go in git), and fill the
`[onshape]` ids from your document URL:

    /documents/{document_id}/w/{workspace_id}/e/{element_id}

**Put your driving dimensions in a Variable Studio.** Writing variables
through the API only works against a Variable Studio; variables defined by a
feature inside a Part Studio have to go through the features endpoint, which
is far more fragile. With a Variable Studio, the loop that matters works:
Alfred sets a dimension, reads the real mass properties back, feeds those
numbers to `calc.engineering`, and iterates on measured values rather than
guessed ones.

Alfred cannot author geometry from scratch in Onshape. Do not aim for it.

## Must change before it runs

1. **Hostnames.** Configs use `alfredpi.local` and `alfreddesktop.local`.
   Use mDNS or static DHCP leases — on plain DHCP the addresses move and
   workers fail to reconnect silently.
2. **The shared artifact mount.** Handlers write to `artifact_dir` and return
   `file://` URIs. Across machines that only works if the path is the same
   everywhere. On the Pi: export `/srv/alfred` over NFS; on every other node,
   mount it at `/mnt/alfred`. Skip this and the Zenbook returns a path Alfred
   cannot open — no error, just a summary with a dead link beneath it, which
   is the worst kind of failure because it reads as success.
3. **`claim_delay_s` on the desktop.** Ships at 0 for phase 1. Set it to ~15
   when the Zenbook joins, or the desktop wins every race and the network
   collapses back to one machine.
4. **`[onshape]` ids and API keys**, as above.

## Stubs to fill in

- SearXNG on the Pi backs `research.web` (`docker run -d -p 8888:8080
  searxng/searxng`). Swap in Brave or Tavily by editing one block.
- No dashboard yet. It is an observability tool for a system that has to
  exist first.
- Memory has only its exact half. The fuzzy half (embedded conversation
  chunks for recall) is a separate store — do not merge them, and never
  consult the fuzzy one for authoritative facts.
