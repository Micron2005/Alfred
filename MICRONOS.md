# Micron OS

> The long game — how Micron OS owns more of the body over time until
> almost nothing general-purpose is left underneath — is written out
> stage by stage in OWNERSHIP.md ("the road off the distro").
>
> How Alfred is kept fast on local hardware is in SPEED.md.

The old Alfred-OS project's north star — a real operating system with Alfred
at its heart — carried forward on the distributed architecture, with one
deliberate break from the old design: **Micron OS is a household, not a
computer.** The old version was one machine running its own built-in AI.
This version has one brain (Alfred, on the desktop) and every other device
is a terminal of the same house — its worker service is Alfred's hands, its
screen is a door into the same shell. Install it on the Zenbook, the
MacBook, a future machine: none of them get their own Alfred, all of them
get *him*.

    Desktop:        ./deploy/install-micronos.sh core     (+ kiosk if wanted)
    Every other:    ./deploy/install-micronos.sh device <desktop-hostname>

A device terminal shows "Terminal · zenbook" on its plaque and marks itself
in the Household rail; conversation, memory and approvals are the same
everywhere because there is only one Alfred behind every door. The route is
incremental on purpose: a custom ISO on day one is a maintenance mountain,
and every serious custom OS begins as a themed configuration of a base
distro. Ubuntu is the engine nobody sees; Micron OS is what you touch.

## v0 — Alfred is part of the machine  (BUILT)

- `run_server.py` — Alfred core behind HTTP; stdlib only, no new deps.
- `shell/index.html` — the Micron OS shell. One offline file. Aged brass
  over deep steel; gold is Alfred's voice, cyan is machine telemetry.
  Console left, Household / Projects / Notices rail right.
- `deploy/alfred-core.service` — Alfred starts when the desktop boots.
- `deploy/alfred-worker.service` — other machines offer their hands at boot.
- `deploy/install-micronos.sh core|worker|kiosk` — one-command install;
  `kiosk` opens the shell fullscreen at login.

Result: power on the desktop and Alfred is simply *there*, at
http://localhost:8710, from any machine in the house if started with
`--host 0.0.0.0`.

## v0.5 — Alfred changes the OS  (BUILT)

- `os.observe` — disk, memory, services, packages, logs. Read-only, runs
  immediately, on any enrolled machine ("how's the Zenbook's disk?").
- `os.apply` — install/remove packages, control services, GNOME settings,
  home-directory files. A closed catalog, never free-form shell, and it
  NEVER runs on Alfred's judgment: proposals park as Pending changes in the
  shell (and `/pending` in the REPL); only Approve executes. Double-click
  safe; every execution reported back as a notice.
- Denylist no approval crosses: Alfred's own services, ssh, systemd, sudo,
  ~/.ssh, his state database. The owner cannot be locked out; the butler
  cannot fire himself.
- `./deploy/install-micronos.sh sudo` — scoped passwordless sudo for exactly
  apt-get and systemctl. Deliberately never NOPASSWD:ALL: the catalog plus
  that scope is the entire blast radius of an approved change.

## v0.7 — apps are apps  (BUILT)

The old project's central mistake, corrected: its "apps" were components
compiled into one bundle — undeletable by construction. In Micron OS an app
is a folder:

    apps/<id>/app.json     name, icon, description
    apps/<id>/index.html   the app itself

Installing is adding the folder. Uninstalling is deleting it — the space
genuinely comes back, and the launcher updates on its next refresh because
the folder listing IS the registry. No build step, no database, no
entanglement with the shell. Apps get one service from the OS: per-app JSON
storage at /api/appdata/<id> (5MB, validated), so their state survives
without them touching Alfred's own database.

First app: **Map** — real streets (MapLibre + OpenFreeMap, no key, no
account), a true 2D/3D toggle (top-down or tilted perspective), and the
owner's marking system: categorized pins — Base, Contact, Interest, Supply,
Caution, plus any category you invent with its own color — each with a note
saying why it matters, filterable, persisted on this machine. The tiles come
from the internet; the marks never leave the house.

## v0.9 — voice and hands-over-files  (BUILT)

- Talk to him: mic button in the shell — click, speak, click — transcribed
  locally (faster-whisper) and sent as your message. Works on the desktop
  itself; other terminals need HTTPS for browser mic access (roadmap).
- Hear him: spoken-replies toggle in the shell, and "Speak lessons" in the
  Mentor — his replies read aloud by Piper, locally, no cloud voice.
  Setup on the desktop:  pip install piper-tts faster-whisper --break-system-packages
- Show him files: the paperclip uploads into ~/.alfred/inbox (and ONLY chat
  attachments from that inbox are accepted — jailed, tested). Routing is
  deterministic by type, never through the 7B planner:
    pdf/txt/md -> research.document        anything else -> media.inspect
    images/video -> vision, when eyes exist (see below)
- media.inspect works today with no model: what the file is, its size, and
  a safe text preview — analysis without opening it.

## The household's voice lives on the Pi

Voice is a capability, not a shell feature — so it runs where it belongs.
The Pi advertises speech.transcribe and speech.synthesize (Piper was made
for that board), and the shell's mic and spoken-replies route THROUGH the
household to it: audio rides the bus to the Pi, text comes back, spoken wav
comes back. The laptops and other terminals never need microphones or voice
libraries of their own — the room has ears, not each screen.

Local libraries on the desktop remain a fallback, so voice works in phase 1
before the Pi exists and the Pi silently takes over the instant it joins —
no config change, no restart (verified: capability appears on the bus within
one heartbeat, and the endpoints fall back locally when it leaves). Voice
clips ride the bus base64-encoded, capped at ~1 minute — a command, not a
podcast; longer audio goes on the shared mount instead.

    Pi setup:  pip install faster-whisper piper-tts --break-system-packages

## Eyes, parked deliberately

vision.describe (images) and media.video (frames + audio transcript) are
written, registered, and tested mechanically — but not advertised, because
no vision model is pulled yet. Until then an uploaded image degrades
honestly to media.inspect instead of pretending. To turn the eyes on later:
    ollama pull llava:7b        # then uncomment the two lines in configs/desktop.toml
Honest scope when they arrive: video = frames sampled across the runtime
plus the audio transcript — a faithful gist, not movie-watching.

## v1.1 — a darker map  (BUILT)

The map now defaults to a **dark** style with a Dark / Night / Light
switcher, honouring the one concrete complaint about the old map: it could
not go darker. All three styles are keyless (OpenFreeMap); the choice
persists. How the map otherwise works is unchanged, as requested.

## What Micron OS deliberately does not carry

Micron OS is a new thing. The owner's earlier projects were mined for
lessons, never for cargo. On record, declined by the owner:

- **Batman lore and any preloaded identity** — fresh start; Alfred learns
  his employer from nothing.
- **The self-coding agent** — Alfred editing his own source was judged too
  dangerous and was never built. (self_update is not it: he fetches what the
  owner pushed; he authors nothing.)
- **The HUD with hand-tracking, camera, and face recognition** — iffy in
  practice, heavy, and not what makes the OS useful. Not ported. A plain
  status dashboard was also removed at the owner's request; it lives in git
  history if ever wanted.
- **Spotify integration** — not wanted.
- **A built-in CAD studio UI** — CAD in Micron OS is a set of worker
  capabilities (Onshape via API, FreeCAD on the Zenbook) that Alfred uses on
  your behalf; there is no CAD editor bolted into the shell.
- **A USB-resident Alfred running on top of another OS** — too laggy;
  contrary to the all-local speed goal. The HONEST version — a bootable
  Micron OS travel stick that turns any computer into a real terminal, syncs
  memory home, and leaves no trace — is specified in docs/USB_TERMINAL.md
  (spec only, not built).

## v1 — it feels like his OS   (next, each item independent)

- Plymouth boot splash + GRUB theme — your own emblem, designed fresh.
- Voice in the shell: browser mic -> whisper endpoint -> reply audio.
  `alfred/voice.py` already has both halves; needs two API routes.
- Notices as desktop notifications (`notify-send` from the supervisor).
- Enrollment in the shell: adopt new machines from the Household rail
  instead of the REPL.

## v2 — the household

- Old-project apps returning as capabilities + shell panels: proactive
  briefing (agenda/weather/memory), file manager over the artifact store,
  a system monitor fed by worker telemetry.
- `hw.*` device controls as shell switches (the Pi is already the gateway).
- Login greeting in his voice, using whatever he has learned to call you.

## v3 — the full vision, if it still calls to you

- Electron/Cage kiosk session selectable at the login screen: the machine
  boots *into* Micron OS.
- Debian preseed / Ubuntu autoinstall ISO that lays all of this down
  unattended — the honest version of "my own ISO".
- D-Bus host control (the old host_control.py, done properly).

## Identity

The house bears the owner's name; the butler keeps his own. Micron OS is
the operating system; Alfred is the one who runs it. Services, state and
code keep the `alfred-` names because they are his; everything the owner
sees says Micron OS.

## Principles carried from the old project

- There is only one Alfred. The shell talks to him; it is not him.
- Local-first: the shell renders and the core answers with the network cable
  cut. No CDN fonts, no cloud dependencies in the spine.
- The user cannot be locked out of his own house.
