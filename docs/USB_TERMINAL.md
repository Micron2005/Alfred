# The Micron OS travel stick — specification

Status: SPEC ONLY. Nothing here is built yet. Written so that when it is
built, it behaves the way the owner intends and harms no device it touches.

## What the owner wants

A bootable USB stick that turns any computer into a Micron OS terminal.
Plug it in, boot from it, and:

- Alfred works exactly as he does at home — talk to him, use the apps.
- The home household SEES the travelling terminal appear, like any other
  device, and can hand it work.
- Work products (a saved 3D file, notes) land either on the stick or back on
  the main computer at home.
- Memory made on the road syncs back to the main Alfred.
- Pull the stick, reboot: the host computer is untouched, no trace left.

The scene: at a friend's house, plug in, boot, "Alfred, save this 3D file" —
and it is saved, and remembered, as if you were home.

## Why boot-from-USB, never run-on-top

Booting FROM the stick loads Micron OS into the host's real RAM and runs on
its real CPU; the stick is just the disk. That is fast and clean. Running
Alfred ON TOP of the host's existing Windows/Mac would be slow (his stack off
flash, fighting the host OS) and is exactly the auto-execute-on-insert
pattern that malware uses — which every OS blocks for good reason. One
deliberate boot-menu keypress (F12) is the honest, safe path. It also
guarantees the "no trace left" property: nothing is ever written to the
host's disk.

## The hard part, named plainly: reaching home

At your friend's house the stick is on THEIR network, behind THEIR router.
Your home Alfred is behind YOUR router. They cannot see each other by
default — this is the one genuinely complex piece, and pretending otherwise
would build something that only works on paper. Three honest options, in
order of how we would likely choose:

1. **Overlay VPN (recommended): Tailscale.**
   The stick and the home machines all join one private mesh network
   (WireGuard underneath). To the software it looks like they are on the same
   LAN no matter whose router they sit behind. The stick boots, joins the
   mesh, and the home household sees the terminal appear — exactly the
   experience wanted. Free for personal use, no ports opened on the home
   router (safer than exposing anything to the internet).
   - Effort: install tailscale on the stick image and on the home core;
     authenticate once.
   - Safety: nothing at home is exposed to the public internet.

2. **Offline mode (always works, no home needed).**
   If there is no network home — or you just want it self-contained — the
   stick runs a COMPLETE Alfred locally: its own model, its own state. It is
   a full Alfred in your pocket that happens not to sync. Talk, use apps,
   save files to the stick. On next boot at home it can sync what changed.
   This is the fallback that must always work, and it is the simplest to
   build first.

3. **Direct connection (not recommended): port-forwarding home.**
   Opening a port on the home router so the stick can reach in. Rejected:
   it exposes the home network to the internet, which is exactly the kind of
   attack surface SECURITY.md exists to avoid. Listed only to say why not.

## Where saved work goes

- **To the stick:** always available. A `travel/` area on the stick's
  persistent partition. Simple, works offline, comes home in your pocket.
- **To home:** when the mesh (option 1) is up, `save this to home` routes the
  artifact to the home artifact store over the private network — the same
  NFS/artifact path the household already uses, just reached over Tailscale.
- Default: save to the stick AND queue a sync-home, so a file is never lost
  if the network drops mid-transfer.

## Memory sync — carefully, because memory is truth

Road memory must merge back without corrupting home memory. The facts store
already supersedes rather than overwrites and timestamps every change, which
is exactly what a clean merge needs:

- Each side keeps its own conversation log; they concatenate by timestamp.
- Facts merge by (subject, key): the newest stated value wins, older ones
  become superseded history — never a silent overwrite, never a lost
  correction. If both sides changed the same fact while apart, both are kept
  with their timestamps and Alfred can ask which stands.
- Sync is one-way-safe: the stick pushes road changes home; it never deletes
  home data. Worst case is a duplicate to reconcile, never a loss.

## The stick's layout (two partitions)

1. **Read-only system:** the Micron OS image — kernel, base, Alfred, apps,
   the model. Read-only so a host cannot corrupt it and so every boot is
   clean and identical.
2. **Persistent data:** encrypted. Your facts, conversation, travel/ files,
   and the Tailscale key. Encryption matters most here — a travel stick is
   the thing most likely to be lost, so losing it must not mean handing a
   stranger your data or a key into your home network.

## Not harming the host — the owner's stated priority

- Boot-from-USB touches nothing on the host disk; it uses the host's RAM and
  CPU only while running. Remove the stick, reboot, the host is exactly as it
  was. This is inherent to how live USB systems work — it is not a feature we
  must be careful to preserve, it is the default we get for free.
- We never auto-run on the host OS, so there is nothing to clean up and no
  way to leave malware-like residue.
- On the friend's network we only make OUTBOUND connections (to the mesh);
  we open nothing, expose nothing, and touch no other device on their LAN.

## Honest limits

- The model runs on the HOST's hardware. A weak host means CPU-speed Alfred —
  correct, just slower. The stick carries the brain; the host lends the
  muscle.
- First boot on unfamiliar hardware may need generic drivers; Ubuntu's live
  images are good at this but not perfect. Well-known laptops: fine. Exotic
  hardware: maybe not.
- Booting from USB requires the host owner to allow the boot-menu key. On a
  locked-down corporate machine, that may be disabled — nothing we can or
  should override.

## Build order, when we build it

1. Offline mode first (option 2): a bootable stick with a complete local
   Alfred, saving to travel/. Fully useful on its own, harms nothing, and
   proves the live-USB image.
2. Add the encrypted persistent partition + memory sync-at-home-boot.
3. Add Tailscale mesh (option 1): the "home sees the terminal, save-to-home"
   experience.

Each stage is usable and safe on its own. We never ship a stage that could
lose data or touch a host it should not.
