"""Alfred, implementing the Micron OS Assistant Interface.

Micron OS defines the socket (assistant_api.AssistantProvider); this file is
Alfred plugging into it. Nothing in the OS knows Alfred; it knows only this
contract -- anyone's assistant can stand exactly here.
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
from pathlib import Path

from alfred.bus import build_bus
from alfred.config import load
from alfred.core.alfred import Alfred
from alfred.worker.runtime import WorkerRuntime


class AlfredProvider:
    name = "Alfred"

    def __init__(self, loop) -> None:
        self.loop = loop
        self.cfg = load(str(Path(__file__).resolve().parent.parent
                            / "configs" / "desktop.toml"))
        self.bus = None
        self.alfred = None
        self.default_project = None
        self._background: list = []

    async def start(self) -> None:
        self.bus = build_bus(self.cfg)
        await self.bus.connect()
        self.alfred = Alfred(self.bus, self.cfg)
        active = self.alfred.state.active_projects()
        self.default_project = (
            active[0]["id"] if active else self.alfred.state.create_project(
                "Household", "General running of the house"))
        self._background.append(asyncio.create_task(self.alfred.supervise()))
        if self.cfg["worker"]["capabilities"]:
            self._background.append(
                asyncio.create_task(WorkerRuntime(self.bus, self.cfg).run()))
        self._talk_lock = asyncio.Lock()

    async def chat(self, message, project_id=None, attachments=None) -> str:
        async with self._talk_lock:       # one Alfred, one conversation
            return await self.alfred.converse(
                message, project_id or self.default_project,
                attachments=attachments)

    async def status(self) -> dict:
        import json as _json
        nodes = []
        for profile in await self.bus.seen_nodes():
            known = self.alfred.state.known_node(profile.node_id)
            caps = _json.loads((known or {}).get("capabilities") or "[]")
            nodes.append({
                "node_id": profile.node_id,
                "name": (known or {}).get("name") or "",
                "describe": profile.describe(),
                "capabilities": caps,
                "assigned": bool(caps),
            })
        return {
            "pending_actions": self.alfred.state.pending_actions(),
            "nodes": nodes,
            "workers": [
                {"id": w.worker_id, "queue": w.queue_depth, "caps": w.capabilities}
                for w in await self.bus.workers()
            ],
            "projects": self.alfred.state.active_projects(),
            "notices": self.alfred.state.undelivered(),
            "default_project": self.default_project,
        }

    async def approve_action(self, action_id: int) -> dict:
        return await self.alfred.approve_action(action_id)

    async def decline_action(self, action_id: int) -> dict:
        return self.alfred.decline_action(action_id)

    async def speech_capabilities(self) -> set[str]:
        caps = await self.alfred._network_capabilities()
        return {c for c in caps if c.startswith("speech.")}

    async def transcribe(self, audio: bytes, fmt: str) -> str:
        caps = await self.speech_capabilities()
        if "speech.transcribe" in caps:
            from alfred.contracts import Task
            task = Task(capability="speech.transcribe", prompt="transcribe",
                        timeout_s=90, max_retries=0,
                        inputs={"audio_b64": base64.b64encode(audio).decode(),
                                "format": fmt})
            result = await self.alfred._dispatch(task)
            if result.ok:
                return (result.data or {}).get("text", result.summary or "")
            raise RuntimeError(result.error or "household transcription failed")
        from alfred.voice import transcribe_file
        with tempfile.NamedTemporaryFile(suffix="." + fmt, delete=False) as tf:
            tf.write(audio); tmp = tf.name
        try:
            return await asyncio.to_thread(transcribe_file, tmp)
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def synthesize(self, text: str) -> bytes:
        caps = await self.speech_capabilities()
        if "speech.synthesize" in caps:
            from alfred.contracts import Task
            task = Task(capability="speech.synthesize", prompt="speak",
                        timeout_s=60, max_retries=0, inputs={"text": text})
            result = await self.alfred._dispatch(task)
            if result.ok:
                return base64.b64decode((result.data or {}).get("wav_b64", ""))
            raise RuntimeError(result.error or "household synthesis failed")
        from alfred.voice import synth_wav
        return await asyncio.to_thread(synth_wav, text)

    async def stop(self) -> None:
        for t in self._background:
            t.cancel()
        if self.bus is not None:
            await self.bus.close()


def create_provider(loop):
    return AlfredProvider(loop)
