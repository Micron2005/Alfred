# Alfred's sight (spec'd by the owner, Aug 2026 — build when eyes go on)

The owner's words: not screen-sharing with start/stop — Alfred simply SEES
the screen whenever he is awake, the way a friend beside you sees it.
"I pull up Spider-Man 1 and he is watching with me."

## One state, no modes (binding, owner's confirmation)

There is nothing to switch. His eyes are open from the moment he wakes;
"you can see my screen, right?" is answered by seeing, not by enabling.
The "modes" below are MANNERS, not settings — he reads the room (a film,
an IDE, a tutorial) and adjusts cadence and chattiness himself, the way a
friend does. The only spoken commands in all of sight are "look away" and
"eyes on" — privacy, not operation.

## Shape
- **Always-on gaze**: continuous screen capture from startup; no share
  button, no stop button. Frames go to the local vision model (llava-class,
  one `ollama pull` away) — GLANCES every few seconds, not 24fps; a friend
  who looks up often, not a camera pipeline.
- **Ears with the eyes**: system audio through the local speech model gives
  him the dialog. Frames + dialog = he genuinely follows a movie, comments,
  and remembers it afterward (rolling notes into his memory).
- **Movie mode**: lower glance cadence + full transcription, tuned so the
  chat model and vision model share the 8GB GPU without choking.

## Butler's discretion (binding)
- A visible mark in the shell whenever the eyes are open. Never unseen watching.
- "Look away" / "eyes on" — instant, no argument. Passwords and private
  moments are not his business unless invited.
- Every frame and every word DIES ON THIS MACHINE. Local models only; sight
  never touches the network. This is why an always-watching companion is
  sane to build at all.
- Sight never grants action: seeing the screen and touching the machine
  remain separate powers; the approval gate is unchanged.

## Learning together (the owner's third use — the real purpose)

"This is why I wanted Alfred to see my screen — so he can watch and learn
as well, even if I am learning with him."

Sight is not surveillance and not just error-catching: it is SHARED
EXPERIENCE. A tutorial watched is watched by both; a video's explanation
heard is heard by both; his rolling notes make it remembered by both.
The owner and the butler learn as a pair. This only makes sense because
every frame dies on the owner's own hardware — a local companion can be
present in a way no cloud service honestly could.

## Screens of his own — background watching (owner's spec)

Sight is not only "what the owner watches." Alfred can watch a video the
owner does NOT have open — a tutorial set going in the background while the
owner codes or reads. His attention and the owner's become independent
streams: he watches, notes, and reports back ("that video answered your
worm-gear question"). The owner may hand him a video, or he may pull one up
himself when asked to study something.

### Whose screen, and the courtesy of asking (binding)
- **The owner's screen is shared space** — he sees it always; that is the
  companionship, no permission needed.
- **The other machines are the household's screens.** He may light one up to
  watch/process on ONLY two triggers:
    1. the owner tells him to, or
    2. he ASKS and the owner says yes.
- **The two reasons he asks are considerate ones**, and both are in
  character: capability ("let me use the laptop, I'll process it faster") or
  courtesy ("let me take this to the other screen so I don't crowd yours").
  A butler steps into another room by asking, never by assuming.
- Distributing a watch-job across machines rides the same bus and enrollment
  as any other work: only nodes that advertise media/vision capabilities are
  eligible, and the owner's approval gates lighting up a machine.

## Workbench mode (the owner's second use, same day)

"I don't have the ESP32-S3 selected as COM1 and he can see it and tell me;
if I wrote something wrong he can see it and tell me I missed something."

- **Eyes + facts beat eyes alone.** Sight gives context (which IDE, which
  dropdown, what just errored); system knowledge gives truth (which serial
  devices actually exist, e.g. /dev/ttyACM0). Together: "the board is on
  ttyACM0 but the IDE is pointed elsewhere" — caught before the flash fails.
- **For code, sight finds the file; reading reviews it.** The vision model
  catches the obvious on screen (error text, red flags); real review happens
  by reading the actual file with perfect fidelity, not pixels of it.
- **Colleague, not backseat driver (binding):** he mentions a spotted
  problem once, briefly, then lets the owner work. No nagging, no running
  commentary unless invited.
