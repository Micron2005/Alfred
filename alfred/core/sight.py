"""Alfred's eyes: a standing gaze at the owner's screen.

SIGHT.md, made concrete. While Alfred is awake and the owner has not told
him to look away, he glances at the screen every `glance_s` seconds --
captures one frame, hands it to a *local* vision model, keeps a one-line
note of what he saw, and forgets the frame. No stream, no recording, no
upload: the frame lives in a temp directory for the length of one model
call and the notes live in memory, a dozen at most.

The notes reach the conversation through `briefing()`, so "can you see my
screen?" is answered from what he is actually looking at, and a fresh look
is taken when the owner asks about the screen directly. Seeing grants no
right to act: this module has no hands, and nothing in it touches the
approval gate in front of `os.apply`.

The owner's two words -- "look away" and "eyes on" -- flip `open`, which
persists across restarts; the panel shows the same switch and an
always-visible indicator of which way it is set.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from pathlib import Path

from alfred import llm
from alfred.core.state import State
from alfred.worker.handlers.screen import capture

log = logging.getLogger("alfred.sight")

GLANCE_SYSTEM = (
    "You are the eyes of an assistant, looking at the owner's computer screen. "
    "Report only what is visible. No greetings, no advice, no speculation."
)
GLANCE_PROMPT = (
    "In one or two plain sentences: which application is in focus and what is "
    "the person doing? Quote any visible error message verbatim. Nothing else."
)
LOOK_PROMPT = (
    "Answer the question using only what this screen shows. Be specific and "
    "concrete; quote on-screen text where it helps. Under 150 words.\n\nQuestion: "
)

_AWAY = re.compile(
    r"(alfred[,:]?\s*)?(please\s+)?(look away|eyes off|close your eyes|"
    r"stop (looking|watching)( (at )?my screen)?)(,? please)?[.!]*"
)
_ON = re.compile(
    r"(alfred[,:]?\s*)?(please\s+)?(eyes on|open your eyes|you can look( again| now)?|"
    r"look again|start (looking|watching)( (at )?my screen)?)(,? please)?[.!]*"
)
_ABOUT_SCREEN = re.compile(r"\b(my |the |this )?screen\b|\bwhat am i (doing|looking at)\b")


class Sight:
    def __init__(self, cfg: dict, state: State, busy: Callable[[], bool]) -> None:
        opts = cfg.get("sight", {})
        core = cfg.get("core", {})
        self.state = state
        self.busy = busy
        self.enabled: bool = bool(opts.get("enabled", True))
        self.glance_s: float = float(opts.get("glance_s", 20))
        self.model: str = opts.get("model") or core.get("vision_model") or "llava:7b"
        # Frames go to Ollama on this machine and nowhere else, whatever the
        # core uses for words. Kept separate from cfg so a hosted provider in
        # [core] can never receive a screenshot by accident.
        self._cfg = {"core": {
            "provider": "ollama",
            "ollama_url": opts.get("ollama_url") or core.get("ollama_url", "http://127.0.0.1:11434"),
            "keep_alive": core.get("keep_alive", "30m"),
            "num_ctx": 2048, "num_predict": 200,
        }}
        self.open: bool = state.setting("eyes", "open") == "open"
        self.available: bool = False
        self.reason: str = "not checked yet"
        self._checked_at: float = 0.0
        self.glances: deque[dict] = deque(maxlen=12)
        self.last: dict | None = None
        self.looking: bool = False

    # ---- switches -------------------------------------------------------

    @property
    def watching(self) -> bool:
        return self.enabled and self.open and self.available

    def set_open(self, value: bool) -> None:
        self.open = value
        self.state.set_setting("eyes", "open" if value else "away")
        if not value:
            self.glances.clear()
            self.last = None
        log.info("eyes %s", "on" if value else "away")

    def command(self, message: str) -> str | None:
        """The owner's spoken switch, handled without a model call."""
        m = message.lower().strip()
        if _AWAY.fullmatch(m):
            self.set_open(False)
            return "Looking away. Say \"eyes on\" when you want me to look again."
        if _ON.fullmatch(m):
            self.set_open(True)
            if not self.enabled:
                return "My eyes are switched off in the config ([sight] enabled = false)."
            return "Eyes on."
        return None

    @staticmethod
    def asks_about_screen(message: str) -> bool:
        return bool(_ABOUT_SCREEN.search(message.lower()))

    # ---- readiness ------------------------------------------------------

    def _tags(self) -> list[str]:
        url = self._cfg["core"]["ollama_url"] + "/api/tags"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        return [m.get("name", "") for m in data.get("models", [])]

    async def check(self, force: bool = False) -> bool:
        """Is a local vision model actually there? Re-asked every minute so
        pulling the model later is picked up without a restart."""
        if not force and time.time() - self._checked_at < 60:
            return self.available
        self._checked_at = time.time()
        try:
            names = await asyncio.to_thread(self._tags)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.available = False
            self.reason = (f"Ollama is not answering at {self._cfg['core']['ollama_url']}"
                           f" ({exc.__class__.__name__})")
            return False
        want = self.model if ":" in self.model else self.model + ":latest"
        if want not in names and self.model not in names:
            self.available = False
            self.reason = f"vision model not pulled — run: ollama pull {self.model}"
            return False
        if not self.available:
            log.info("eyes ready: %s", self.model)
        self.available = True
        self.reason = ""
        return True

    # ---- looking --------------------------------------------------------

    async def glance(self, question: str | None = None) -> str:
        """One frame, one local model call, one line of memory. The frame is
        gone when this returns."""
        self.looking = True
        try:
            with tempfile.TemporaryDirectory() as td:
                shot = Path(td) / "screen.png"
                grabbed, note = await capture(shot)
                if not grabbed:
                    self.available = False
                    self.reason = note
                    raise RuntimeError(note)
                frame = base64.b64encode(shot.read_bytes()).decode()
            prompt = LOOK_PROMPT + question if question else GLANCE_PROMPT
            text = await llm.complete(
                prompt, self._cfg, system=GLANCE_SYSTEM, images=[frame],
                model=self.model, timeout=180,
            )
        finally:
            self.looking = False
        entry = {"at": time.time(), "text": text, "question": question}
        self.glances.append(entry)
        self.last = entry
        return text

    async def run(self) -> None:
        """The standing gaze. Skips a beat while Alfred is mid-reply so the
        one local GPU is not asked to think and look at the same time."""
        if not self.enabled:
            log.info("eyes disabled in config")
            return
        while True:
            try:
                if not self.open:
                    await asyncio.sleep(1)
                    continue
                if not await self.check():
                    await asyncio.sleep(15)
                    continue
                if self.busy():
                    await asyncio.sleep(2)
                    continue
                await self.glance()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("glance failed: %s", exc)
                await asyncio.sleep(self.glance_s)
                continue
            await asyncio.sleep(self.glance_s)

    # ---- what the rest of Alfred sees -----------------------------------

    def status(self) -> dict:
        last = None
        if self.last:
            last = {**self.last, "ago_s": int(time.time() - self.last["at"])}
        return {
            "enabled": self.enabled, "open": self.open, "available": self.available,
            "watching": self.watching, "looking": self.looking, "reason": self.reason,
            "model": self.model, "glance_s": self.glance_s, "last": last,
            "glances": [{**g, "ago_s": int(time.time() - g["at"])} for g in self.glances],
        }

    def briefing(self) -> str:
        if not self.enabled:
            return ""
        if not self.open:
            return ("Your eyes: you are looking away from the owner's screen at their "
                    "request. If asked, say so; they can say \"eyes on\".")
        if not self.available:
            return (f"Your eyes: closed — {self.reason}. If asked whether you can see "
                    "the screen, say plainly that you cannot right now, and why.")
        if not self.glances:
            return "Your eyes: open, first glance at the owner's screen still pending."
        now = time.time()
        lines = [
            f"  - {int(now - g['at'])}s ago: {g['text']}"
            for g in list(self.glances)[-4:] if not g.get("question")
        ]
        return (
            "Your eyes (a local vision model glances at the owner's screen every "
            f"{int(self.glance_s)}s; frames never leave this machine):\n"
            + "\n".join(lines)
            + "\nUse this only when relevant or when asked what you see; do not "
            "narrate the screen unprompted. A real problem is worth one brief "
            "mention, once. Seeing the screen gives you no permission to act on it."
        )
