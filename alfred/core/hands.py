"""The owner's grip on Alfred's hands. Desktop only.

By default every `ui.act` (a click, a keystroke, opening or closing a
window) is parked on the panel and waits for Approve, exactly like a change
to the OS. That is safe and slow. For a stretch of real work the owner can
say "drive for 10 minutes": until the clock runs out, `ui.act` tasks run
without a card, each one is written down as it happens, and the panel shows
the countdown next to one button, STOP.

STOP is the kill switch. It ends the grant at once, and anything still
queued in the current turn is refused rather than parked — a plan of twelve
clicks must not turn into twelve approval cards the moment the owner says
stop. Nothing here survives a restart: a fresh process has no grant.

Eyes are elsewhere (sight.py). Looking never grants driving, and driving
never opens the eyes.
"""

from __future__ import annotations

import re
import time
from collections import deque

MAX_MINUTES = 60
DEFAULT_MINUTES = 5

_STOP = re.compile(
    r"^\s*(alfred[,:]?\s*)?(stop( driving| now| it| that)?|halt|hands off|let go|that's enough)"
    r"[.!\s]*$", re.IGNORECASE)
_DRIVE = re.compile(
    r"\b(drive|take the wheel|take over|hands on|you can drive|go ahead and drive)"
    r"\b.*?\b(\d{1,3})\s*(m|min|mins|minute|minutes)\b", re.IGNORECASE)


class Hands:
    def __init__(self) -> None:
        self.until = 0.0            # grant expiry, epoch seconds; 0 = none
        self.granted_at = 0.0
        self.stopped_at = 0.0       # last STOP, for refusing the rest of a turn
        self.recent: deque[dict] = deque(maxlen=12)   # what the hands did, newest last

    # ---- the grant -------------------------------------------------------

    @property
    def driving(self) -> bool:
        return time.time() < self.until

    @property
    def remaining_s(self) -> int:
        return max(0, int(round(self.until - time.time())))

    def grant(self, minutes: float) -> dict:
        minutes = min(max(float(minutes), 0.5), MAX_MINUTES)
        self.granted_at = time.time()
        self.until = self.granted_at + minutes * 60
        return self.status()

    def stop(self) -> dict:
        """The kill switch. Idempotent; pressing it twice is fine."""
        self.stopped_at = time.time()
        self.until = 0.0
        return self.status()

    def stopped_since(self, t: float) -> bool:
        """Was STOP pressed after `t`? The conversation loop asks with the
        time the current request arrived."""
        return self.stopped_at > t

    # ---- what happened ---------------------------------------------------

    def record(self, description: str, outcome: str, ok: bool) -> None:
        self.recent.append({"at": time.time(), "what": description,
                            "outcome": outcome[:200], "ok": ok})

    # ---- spoken commands ----------------------------------------------------

    def command(self, message: str) -> str | None:
        """'stop' / 'hands off' / 'drive for 10 minutes' from the owner,
        handled before any model sees the message. None if it is not one."""
        if _STOP.match(message):
            was = self.driving
            self.stop()
            return ("Stopped. Hands off; every action needs your approval again."
                    if was else "Hands are off; nothing was running.")
        m = _DRIVE.search(message)
        if m:
            self.grant(int(m.group(2)))
            return (f"Driving for {self.remaining_s // 60} minutes. Every click and keystroke "
                    "is logged on the panel; say \"stop\" or press STOP to end it early.")
        return None

    # ---- for the panel and the briefing ----------------------------------

    def status(self) -> dict:
        return {
            "driving": self.driving,
            "remaining_s": self.remaining_s,
            "until": self.until if self.driving else None,
            "granted_at": self.granted_at if self.driving else None,
            "max_minutes": MAX_MINUTES,
            "default_minutes": DEFAULT_MINUTES,
            "recent": list(self.recent),
        }

    def briefing(self) -> str:
        if self.driving:
            return (f"Hands: the owner let you drive; {self.remaining_s // 60} min "
                    f"{self.remaining_s % 60:02d} s left. ui.act runs without asking until then. "
                    "Work in small steps (ui.windows first, one ui.act per step) and say what you did.")
        return ("Hands: every ui.act is parked for the owner's approval on the panel before it "
                "runs; plan it anyway and tell him it is waiting. He can say \"drive for N minutes\" "
                "to let you work without cards.")
