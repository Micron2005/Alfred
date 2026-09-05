#!/usr/bin/env python3
"""Micron OS shell server. Desktop only — this is Alfred's front door.

    python3 run_server.py --config configs/desktop.toml --port 8710

Wraps the same Alfred core that run_core.py drives, behind a small HTTP API,
and serves the Micron OS shell at http://localhost:8710. The REPL keeps
working; this is an additional interface, not a replacement.

Standard library only, on purpose: the shell server is part of the OS's
spine, and the spine should not acquire dependencies. The HTTP server runs
in threads; Alfred lives on one asyncio loop; requests bridge across with
run_coroutine_threadsafe. Conversations are serialized with a lock because
there is one Alfred — two simultaneous conversations would interleave his
memory writes.

Endpoints:
    GET  /                    the Micron OS shell if installed, else Alfred's own panel
    GET  /panel               Alfred's own panel: chat, machines, approvals, enrolment
    POST /api/chat            {"message": str, "project_id": str|null}
    GET  /api/history         recent conversation turns as [role, body, unix_time]
    GET  /api/status          machines, projects, notices (undelivered + recent)
    GET  /api/health          liveness for systemd

The panel is one dependency-free HTML file in panel/. It is not the house --
Micron OS remains the shell -- it is the butler's own door, so a desktop
without Micron OS still gets a page instead of a terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from alfred.bus import connect_core
from alfred.bus.discovery import beacon
from alfred.bus.hosting import stop_server
from alfred.bus.nats_bus import BusUnreachable, host_port
from alfred.config import load
from alfred.contracts import Assignment
from alfred.core.alfred import Alfred
from alfred.worker.runtime import WorkerRuntime
from alfred import shell_lock, wsl

log = logging.getLogger("alfred.server")
SHELL = Path(__file__).parent / "shell" / "index.html"
PANEL = Path(__file__).parent / "panel" / "index.html"
LOGIN_HTML = (Path(__file__).parent / "shell" / "login.html").read_text() \
    if (Path(__file__).parent / "shell" / "login.html").exists() else "<h1>Micron OS locked</h1>"
APPS_DIR = Path(__file__).parent / "apps"
APPDATA_DIR = Path("~/.alfred/appdata").expanduser()
INBOX_DIR = Path("~/.alfred/inbox").expanduser()
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_APPDATA_BYTES = 5 * 1024 * 1024
MIME_BY_EXT = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
               ".css": "text/css", ".json": "application/json",
               ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg"}
WALLPAPER = Path("~/.alfred/wallpaper.img").expanduser()
WALLPAPER_MIME = Path("~/.alfred/wallpaper.mime").expanduser()
MAX_WALLPAPER_BYTES = 15 * 1024 * 1024

# Chat can legitimately take minutes on CPU inference; systemd and browsers
# both need to know this is expected, not a hang.
CHAT_TIMEOUT_S = 600


class Bridge:
    """Holds the asyncio side and lets HTTP threads call into it safely."""

    def __init__(self, alfred: Alfred, loop: asyncio.AbstractEventLoop) -> None:
        self.alfred = alfred
        self.loop = loop
        self._talk_lock = asyncio.Lock()
        self.default_project: str | None = None
        self.join_url: str | None = None   # how other machines reach the bus
        self._door: dict | None = None      # WSL: is Windows forwarding the bus port?
        self._door_at = 0.0

    @property
    def bus_port(self) -> int:
        return host_port(self.alfred.cfg["bus"].get("url", "nats://127.0.0.1:4222"))[1]

    async def door(self, refresh: bool = False) -> dict | None:
        """Under WSL, whether the LAN can reach the bus we host. Asking
        Windows costs a PowerShell round trip, so the answer is kept a minute."""
        if not (wsl.is_wsl() and self.join_url):
            return None
        if refresh or self._door is None or time.time() - self._door_at > 60:
            self._door = await wsl.bridge_status(self.bus_port)
            self._door_at = time.time()
        return self._door

    def open_door(self) -> dict:
        async def _open() -> dict:
            ok, detail = await wsl.bridge(self.bus_port)
            await self.door(refresh=True)
            return {"ok": ok, "detail": detail, "wsl": self._door}
        future = asyncio.run_coroutine_threadsafe(_open(), self.loop)
        return future.result(timeout=150)

    def set_eyes(self, open_: bool) -> dict:
        self.alfred.sight.set_open(open_)
        return self.alfred.sight.status()

    def chat(self, message: str, project_id: str | None,
             attachments: list[str] | None = None) -> str:
        async def _serialized() -> str:
            async with self._talk_lock:  # one Alfred, one conversation at a time
                return await self.alfred.converse(
                    message, project_id or self.default_project,
                    attachments=attachments,
                )

        future = asyncio.run_coroutine_threadsafe(_serialized(), self.loop)
        return future.result(timeout=CHAT_TIMEOUT_S)

    def speech_available(self) -> set[str]:
        async def _caps() -> set[str]:
            return await self.alfred._network_capabilities()
        future = asyncio.run_coroutine_threadsafe(_caps(), self.loop)
        try:
            return {c for c in future.result(timeout=10) if c.startswith("speech.")}
        except Exception:
            return set()

    def transcribe(self, audio: bytes, fmt: str) -> str:
        """Household first: if any node offers speech.transcribe (the Pi,
        by design), the audio rides the bus there. Local libraries are the
        fallback, so phase 1 works and the Pi takes over the moment it
        joins — no config change, no restart."""
        import base64 as _b64
        if "speech.transcribe" in self.speech_available():
            from alfred.contracts import Task
            task = Task(capability="speech.transcribe", prompt="transcribe",
                        timeout_s=90, max_retries=0,
                        inputs={"audio_b64": _b64.b64encode(audio).decode(),
                                "format": fmt})
            future = asyncio.run_coroutine_threadsafe(
                self.alfred._dispatch(task), self.loop)
            result = future.result(timeout=120)
            if result.ok:
                return (result.data or {}).get("text", result.summary or "")
            raise RuntimeError(result.error or "household transcription failed")
        # local fallback
        from alfred.voice import transcribe_file
        import tempfile as _tmp
        from pathlib import Path as _P
        with _tmp.NamedTemporaryFile(suffix="." + fmt, delete=False) as tf:
            tf.write(audio); tmp_path = tf.name
        try:
            return transcribe_file(tmp_path)
        finally:
            _P(tmp_path).unlink(missing_ok=True)

    def synthesize(self, text: str) -> bytes:
        import base64 as _b64
        if "speech.synthesize" in self.speech_available():
            from alfred.contracts import Task
            task = Task(capability="speech.synthesize", prompt="speak",
                        timeout_s=60, max_retries=0, inputs={"text": text})
            future = asyncio.run_coroutine_threadsafe(
                self.alfred._dispatch(task), self.loop)
            result = future.result(timeout=90)
            if result.ok:
                return _b64.b64decode((result.data or {}).get("wav_b64", ""))
            raise RuntimeError(result.error or "household synthesis failed")
        from alfred.voice import synth_wav
        return synth_wav(text)

    def status(self) -> dict:
        async def _gather() -> dict:
            nodes = []
            live = await self.alfred.live_workers_by_node()
            seen = {p.node_id: p for p in await self.alfred.bus.seen_nodes()}
            for node_id in list(seen) + [
                r["node_id"] for r in self.alfred.state.all_nodes() if r["node_id"] not in seen
            ]:
                known = self.alfred.state.known_node(node_id)
                caps = json.loads((known or {}).get("capabilities") or "[]")
                worker = live.get(node_id)
                profile = seen.get(node_id)
                nodes.append({
                    "node_id": node_id,
                    "name": worker.worker_id if worker else (known or {}).get("name") or "",
                    "describe": profile.describe() if profile else (known or {}).get("hostname") or "",
                    "capabilities": worker.capabilities if worker else caps,
                    "assigned": bool(worker or caps),
                    "online": worker is not None,
                    "seen": profile is not None,
                    "local": node_id == self.alfred.local_node_id,
                })
            workers = [
                {"id": w.worker_id, "host": w.host, "node_id": w.node_id,
                 "queue": w.queue_depth, "caps": w.capabilities}
                for w in await self.alfred.bus.workers()
            ]
            return {
                "pending_actions": self.alfred.state.pending_actions(),
                "bus": {"kind": self.alfred.cfg["bus"]["kind"], "join_url": self.join_url,
                        "wsl": await self.door()},
                "eyes": self.alfred.sight.status(),
                "nodes": nodes,
                "workers": workers,
                "projects": self.alfred.state.active_projects(),
                "notices": self.alfred.state.undelivered(),
                "recent_notices": self.alfred.state.recent_notices(),
                "default_project": self.default_project,
            }

        future = asyncio.run_coroutine_threadsafe(_gather(), self.loop)
        return future.result(timeout=15)


def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MicronOS/0.1"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def _authed(self) -> bool:
            return shell_lock.token_valid(shell_lock.cookie_from(self.headers))

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if self.path == "/api/health":
                self._json(200, {"ok": True, "locked": shell_lock.is_set()})
                return
            if shell_lock.is_set() and not self._authed():
                if self.path == "/panel" or (self.path in {"/", "/index.html"} and not SHELL.exists()):
                    self._send(200, PANEL.read_bytes(), "text/html; charset=utf-8")  # asks for the password itself
                elif self.path in {"/", "/index.html"}:
                    self._send(200, LOGIN_HTML.encode(), "text/html; charset=utf-8")
                else:
                    self._json(401, {"error": "locked"})
                return
            if self.path == "/panel" or (self.path in {"/", "/index.html"} and not SHELL.exists()):
                self._send(200, PANEL.read_bytes(), "text/html; charset=utf-8")
            elif self.path in {"/", "/index.html"}:
                self._send(200, SHELL.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/history":
                self._json(200, {"turns": bridge.alfred.state.recent_turns_at(limit=40)})
            elif self.path == "/api/eyes":
                self._json(200, bridge.alfred.sight.status())
            elif self.path == "/api/status":
                try:
                    self._json(200, bridge.status())
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
            elif self.path == "/api/apps":
                # An app is a folder in apps/ with an app.json. That is the
                # entire registry — no database, no build step. Delete the
                # folder and the app has never existed.
                apps = []
                if APPS_DIR.is_dir():
                    for d in sorted(APPS_DIR.iterdir()):
                        manifest = d / "app.json"
                        if d.is_dir() and manifest.exists():
                            try:
                                meta = json.loads(manifest.read_text())
                                meta["id"] = d.name
                                apps.append(meta)
                            except Exception:
                                continue
                self._json(200, {"apps": apps})
            elif self.path.startswith("/apps/"):
                # Static serving, jailed to the apps directory.
                raw = self.path.split("?")[0].removeprefix("/apps/")
                rel = raw.rstrip("/") or ""
                target = (APPS_DIR / rel).resolve()
                if target.is_dir():
                    target = target / "index.html"
                try:
                    target.relative_to(APPS_DIR.resolve())  # traversal jail
                except ValueError:
                    self._json(404, {"error": "no such app"})
                    return
                if not target.is_file():
                    self._json(404, {"error": "no such app file"})
                    return
                mime = MIME_BY_EXT.get(target.suffix.lower(), "application/octet-stream")
                self._send(200, target.read_bytes(), mime)
            elif self.path.startswith("/api/appdata/"):
                app_id = self.path.split("?")[0].removeprefix("/api/appdata/").strip("/")
                store = (APPDATA_DIR / f"{app_id}.json").resolve()
                if not app_id or "/" in app_id or not str(store).startswith(str(APPDATA_DIR.resolve())):
                    self._json(400, {"error": "bad app id"})
                    return
                if store.exists():
                    self._send(200, store.read_bytes(), "application/json")
                else:
                    self._json(200, {})
            elif self.path.startswith("/wallpaper"):
                if WALLPAPER.exists():
                    mime = (WALLPAPER_MIME.read_text().strip()
                            if WALLPAPER_MIME.exists() else "image/jpeg")
                    self._send(200, WALLPAPER.read_bytes(), mime)
                else:
                    self._json(404, {"error": "no wallpaper set"})
            elif self.path == "/api/health":
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "no such path"})

        def do_POST(self) -> None:  # noqa: N802
            import re as _re
            if self.path == "/api/login":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if shell_lock.verify(body.get("password", "")):
                        token = shell_lock.issue_token()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Set-Cookie",
                            f"mos_session={token}; HttpOnly; SameSite=Strict; Path=/")
                        out = json.dumps({"ok": True}).encode()
                        self.send_header("Content-Length", str(len(out)))
                        self.end_headers(); self.wfile.write(out)
                    else:
                        self._json(401, {"error": "wrong password"})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return
            if self.path == "/api/logout":
                shell_lock.revoke(shell_lock.cookie_from(self.headers))
                self.send_response(200)
                self.send_header("Set-Cookie",
                    "mos_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                self.send_header("Content-Length", "0"); self.end_headers()
                return
            if self.path == "/api/setlock":
                if shell_lock.is_set() and not self._authed():
                    self._json(401, {"error": "locked"}); return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    pw = body.get("password", "")
                    if pw == "":
                        shell_lock.clear_password()
                        self._json(200, {"ok": True, "locked": False})
                    else:
                        shell_lock.set_password(pw)
                        self._json(200, {"ok": True, "locked": True})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return
            if shell_lock.is_set() and not self._authed():
                self._json(401, {"error": "locked"}); return
            if self.path == "/api/upload":
                # Raw bytes + X-Filename header: no multipart parsing.
                # Files land in the inbox; chat only accepts attachment
                # paths from inside that inbox.
                try:
                    import re as _re2, time as _time
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > MAX_UPLOAD_BYTES:
                        self._json(400, {"error": "file must be under 200MB"})
                        return
                    name = _re2.sub(r"[^A-Za-z0-9._-]", "_",
                                    self.headers.get("X-Filename", "upload.bin"))[-80:]
                    INBOX_DIR.mkdir(parents=True, exist_ok=True)
                    dest = INBOX_DIR / f"{int(_time.time())}_{name}"
                    with open(dest, "wb") as fh:
                        remaining = length
                        while remaining > 0:
                            chunk = self.rfile.read(min(1 << 20, remaining))
                            if not chunk:
                                break
                            fh.write(chunk); remaining -= len(chunk)
                    self._json(200, {"path": str(dest), "name": name})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return
            if self.path == "/api/tts":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    text = (body.get("text") or "").strip()[:2500]
                    if not text:
                        self._json(400, {"error": "no text"}); return
                    self._send(200, bridge.synthesize(text), "audio/wav")
                except RuntimeError as exc:
                    self._json(503, {"error": str(exc),
                        "hint": "install piper on the core, or bring the Pi online "
                                "with speech.synthesize"})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return
            if self.path == "/api/stt":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 30 * 1024 * 1024:
                        self._json(400, {"error": "audio must be under 30MB"}); return
                    fmt = "webm" if "webm" in self.headers.get("Content-Type", "") else "ogg"
                    text = bridge.transcribe(self.rfile.read(length), fmt)
                    self._json(200, {"text": text})
                except ImportError:
                    self._json(503, {"error": "no ears anywhere in the household",
                        "hint": "install faster-whisper on the core, or bring the "
                                "Pi online with speech.transcribe"})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return
            m = _re.match(r"^/api/actions/(\d+)/(approve|decline)$", self.path)
            if m:
                action_id, verb = int(m.group(1)), m.group(2)
                try:
                    if verb == "approve":
                        future = asyncio.run_coroutine_threadsafe(
                            bridge.alfred.approve_action(action_id), bridge.loop)
                        self._json(200, {"result": future.result(timeout=CHAT_TIMEOUT_S)})
                    else:
                        self._json(200, {"result": bridge.alfred.decline_action(action_id)})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return
            if self.path == "/api/eyes":
                # The owner's switch, same as saying "look away" / "eyes on".
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    self._json(200, bridge.set_eyes(bool(payload.get("open", True))))
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return
            if self.path == "/api/bridge":
                # WSL only: ask Windows (one UAC prompt) to forward the bus
                # port into this VM and allow it through the firewall.
                if not wsl.is_wsl():
                    self._json(400, {"error": "not running inside WSL"})
                    return
                try:
                    self._json(200, bridge.open_door())
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return
            m = _re.match(r"^/api/nodes/([\w.-]+)/(propose|assign)$", self.path)
            if m:
                # Giving a machine a job, from the shell rather than the REPL:
                # `propose` asks Alfred what the machine is good for; `assign`
                # commits {name, capabilities}. The node adopts it within a
                # heartbeat and starts advertising.
                node_id, verb = m.group(1), m.group(2)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if verb == "propose":
                        coro = bridge.alfred.propose_for(node_id, payload.get("hint") or "")
                    else:
                        caps = [c.strip() for c in payload.get("capabilities") or [] if c.strip()]
                        name = (payload.get("name") or "").strip()
                        if not name or not caps:
                            self._json(400, {"error": "name and capabilities are required"})
                            return
                        coro = bridge.alfred.enroll(
                            Assignment(node_id=node_id, name=name, capabilities=caps))
                    future = asyncio.run_coroutine_threadsafe(coro, bridge.loop)
                    result = future.result(timeout=CHAT_TIMEOUT_S)
                    if verb == "propose":
                        if result is None:
                            self._json(404, {"error": f"no machine announcing with id {node_id}"})
                        else:
                            self._json(200, {"proposal": asdict(result)})
                    else:
                        self._json(200, {"result": result})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return
            if self.path.startswith("/api/appdata/"):
                app_id = self.path.removeprefix("/api/appdata/").strip("/")
                if not app_id or "/" in app_id:
                    self._json(400, {"error": "bad app id"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > MAX_APPDATA_BYTES:
                        self._json(400, {"error": "app data over 5MB"})
                        return
                    body = self.rfile.read(length)
                    json.loads(body)  # must be valid JSON; apps store state, not blobs
                    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
                    (APPDATA_DIR / f"{app_id}.json").write_bytes(body)
                    self._json(200, {"ok": True})
                except json.JSONDecodeError:
                    self._json(400, {"error": "app data must be JSON"})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return
            if self.path == "/api/wallpaper":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > MAX_WALLPAPER_BYTES:
                        self._json(400, {"error": "wallpaper must be under 15MB"})
                        return
                    mime = self.headers.get("Content-Type", "image/jpeg")
                    if not mime.startswith("image/"):
                        self._json(400, {"error": "wallpaper must be an image"})
                        return
                    WALLPAPER.parent.mkdir(parents=True, exist_ok=True)
                    WALLPAPER.write_bytes(self.rfile.read(length))
                    WALLPAPER_MIME.write_text(mime)
                    self._json(200, {"ok": True})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return
            if self.path != "/api/chat":
                self._json(404, {"error": "no such path"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                message = (payload.get("message") or "").strip()
                if not message:
                    self._json(400, {"error": "message is empty"})
                    return
                attachments = []
                for raw in (payload.get("attachments") or [])[:4]:
                    resolved = Path(str(raw)).resolve()
                    try:
                        resolved.relative_to(INBOX_DIR.resolve())
                        if resolved.is_file():
                            attachments.append(str(resolved))
                    except ValueError:
                        pass  # only inbox files may be attached
                reply = bridge.chat(message, payload.get("project_id"),
                                    attachments or None)
                self._json(200, {"reply": reply})
            except Exception as exc:
                log.exception("chat failed")
                self._json(500, {"error": str(exc)})

        def log_message(self, fmt: str, *args) -> None:
            log.debug("http: " + fmt, *args)

    return Handler


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/desktop.toml")
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to reach the shell from other machines")
    ap.add_argument("--project", default=None, help="default project id for the shell")
    ap.add_argument("--open", action="store_true",
                    help="open the page in the default browser once the server is up")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)-16s %(levelname)-7s %(message)s"
    )

    cfg = load(args.config)
    try:
        bus, join_url = await connect_core(cfg)
    except (BusUnreachable, RuntimeError) as exc:
        log.error("Alfred cannot start: %s", exc)
        raise SystemExit(1)

    alfred = Alfred(bus, cfg)
    bridge = Bridge(alfred, asyncio.get_running_loop())
    bridge.join_url = join_url
    if args.project:
        bridge.default_project = args.project
    else:
        # The shell needs somewhere to file conversation; latest active
        # project, or a home project created on first boot.
        active = alfred.state.active_projects()
        bridge.default_project = (
            active[0]["id"] if active else alfred.state.create_project(
                "Household", "General running of the house"
            )
        )

    background = [asyncio.create_task(alfred.supervise()),
                  asyncio.create_task(alfred.sight.run())]
    if join_url:
        background.append(asyncio.create_task(beacon(join_url)))
        log.info("bus at %s; other machines: python run_node.py --bus %s (or --bus auto)",
                 join_url, join_url)
        door = await bridge.door()
        if door and not door["ok"]:
            log.warning("WSL: %s — other machines cannot reach the bus until Windows "
                        "forwards it; use the panel's 'Open the door' button", door["detail"])
    if cfg["worker"]["capabilities"]:
        background.append(asyncio.create_task(WorkerRuntime(bus, cfg).run()))

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(bridge))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    page_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    log.info("%s at http://%s:%d", "Micron OS shell" if SHELL.exists() else "Alfred's panel",
             page_host, args.port)
    if args.open:
        webbrowser.open(f"http://{page_host}:{args.port}/")

    stop = asyncio.Event()
    try:
        # systemd stops us with SIGTERM; without a handler that skips the
        # cleanup below and leaves nats-server running headless.
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    except (NotImplementedError, RuntimeError):
        pass  # Windows: Ctrl-C only
    try:
        await stop.wait()  # run until systemd or Ctrl-C says stop
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        httpd.shutdown()
        for task in background:
            task.cancel()
        await bus.close()
        stop_server()


if __name__ == "__main__":
    asyncio.run(main())
