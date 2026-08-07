"""The household's ears and mouth, as capabilities.

Voice is not a feature of the shell; it is a capability of the house, and it
runs wherever the hardware suits it — which is the Pi: always on, and Piper
was designed for exactly that board. Any machine can capture audio and any
machine can play it; the transcribing and the speaking happen wherever these
capabilities are advertised.

    speech.transcribe   audio (base64) -> text        needs faster-whisper
    speech.synthesize   text -> wav audio (base64)    needs piper

Audio rides the bus base64-encoded inside the task, so clips are capped at
what a message bus should carry: ~700KB of audio, roughly 30-60 seconds of
compressed speech. That is a voice command, not a podcast — by design. For
long recordings, put the file on the shared mount and use media/document
capabilities instead.

The shell server prefers these over its local libraries when any node
advertises them, so once the Pi joins the household, the desktop no longer
needs piper or faster-whisper installed at all.
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
from pathlib import Path

from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok

MAX_AUDIO_B64 = 950_000    # ~700KB of audio; NATS default payload is 1MB
MAX_SPEAK_CHARS = 2500


@handler("speech.transcribe")
async def speech_transcribe(task: Task, cfg: dict) -> TaskResult:
    """Audio in, text out. inputs: audio_b64, format ('webm'|'ogg'|'wav')."""
    audio_b64 = task.inputs.get("audio_b64") or ""
    if not audio_b64:
        return fail(task, "no audio provided")
    if len(audio_b64) > MAX_AUDIO_B64:
        return fail(task, "audio too long for the bus; keep voice clips under a minute")
    try:
        raw = base64.b64decode(audio_b64)
    except Exception:
        return fail(task, "audio_b64 is not valid base64")

    suffix = "." + str(task.inputs.get("format", "webm")).lstrip(".")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(raw)
        tmp_path = tf.name
    try:
        from alfred import voice
        text = await asyncio.to_thread(voice.transcribe_file, tmp_path)
    except ImportError:
        return fail(task, "faster-whisper not installed on this node "
                          "(pip install faster-whisper --break-system-packages)")
    except Exception as exc:
        return fail(task, f"transcription failed: {exc}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not text:
        return ok(task, summary="", data={"text": "", "note": "no speech detected"})
    return ok(task, summary=text, data={"text": text})


@handler("speech.synthesize")
async def speech_synthesize(task: Task, cfg: dict) -> TaskResult:
    """Text in, spoken wav out (base64). inputs: text."""
    text = (task.inputs.get("text") or task.prompt or "").strip()[:MAX_SPEAK_CHARS]
    if not text:
        return fail(task, "nothing to say")
    try:
        from alfred import voice
        wav = await asyncio.to_thread(voice.synth_wav, text)
    except RuntimeError as exc:      # piper missing — voice.synth_wav says so
        return fail(task, str(exc))
    except Exception as exc:
        return fail(task, f"synthesis failed: {exc}")

    return ok(
        task,
        summary=f"spoke {len(text)} characters",
        data={"wav_b64": base64.b64encode(wav).decode(), "chars": len(text)},
    )
