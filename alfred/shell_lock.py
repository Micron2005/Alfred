"""Shell lock. The password done honestly this time.

What made the old one theatre: it was checked in browser JavaScript, so
anyone could open the dev console and walk past it, and the password sat in
plain sight. This one:

  - is verified on the SERVER; the browser never sees the hash and cannot
    skip the check
  - stores a salted PBKDF2 hash, never the password itself — a stolen file
    does not reveal the password
  - issues a signed, expiring session token; tampering with the cookie is
    detected because the signature will not match

Honest about scope: this stops someone at an unlocked terminal from being
Alfred as you. It is a lock on the front door. It is NOT encryption and NOT
protection against a remote attacker — the localhost bind and the OS
firewall do that. See SECURITY.md.

The owner is never locked out: if no password is set, the shell is simply
open (single-user home machine, localhost only). Setting one is opt-in.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

LOCK_FILE = Path("~/.alfred/lock.json").expanduser()
SESSION_TTL_S = 12 * 3600
PBKDF2_ROUNDS = 240_000


def _server_secret() -> bytes:
    """Key for signing session tokens. Generated once, kept 0600, never in
    the repo. Rotating it (deleting the file) just logs everyone out."""
    path = Path("~/.alfred/session.key").expanduser()
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    os.chmod(path, 0o600)
    return key


def is_set() -> bool:
    return LOCK_FILE.exists()


def set_password(password: str) -> None:
    if not password or len(password) < 4:
        raise ValueError("password too short")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({
        "salt": salt.hex(), "hash": digest.hex(), "rounds": PBKDF2_ROUNDS,
    }))
    os.chmod(LOCK_FILE, 0o600)


def clear_password() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def verify(password: str) -> bool:
    if not is_set():
        return True  # no lock set -> open
    try:
        rec = json.loads(LOCK_FILE.read_text())
        salt = bytes.fromhex(rec["salt"])
        want = bytes.fromhex(rec["hash"])
        got = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, int(rec.get("rounds", PBKDF2_ROUNDS)))
        return hmac.compare_digest(got, want)  # constant-time; no timing leak
    except Exception:
        return False


# ---- session tokens: value.expiry.signature -------------------------------

def issue_token() -> str:
    expiry = str(int(time.time()) + SESSION_TTL_S)
    value = secrets.token_urlsafe(16)
    payload = f"{value}.{expiry}"
    sig = hmac.new(_server_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def token_valid(token: str | None) -> bool:
    if not is_set():
        return True
    if not token:
        return False
    try:
        value, expiry, sig = token.split(".")
        payload = f"{value}.{expiry}"
        want = hmac.new(_server_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, want):
            return False  # forged or tampered
        return int(expiry) > time.time()
    except Exception:
        return False


def cookie_from(headers) -> str | None:
    raw = headers.get("Cookie", "")
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith("mos_session="):
            return part[len("mos_session="):]
    return None
