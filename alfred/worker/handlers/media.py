"""Alfred's eyes. Images, video, and files he inspects so you need not open them.

Honest scope, stated up front: a home-lab 7B setup does not "watch a movie"
the way a person does. What it genuinely does:

    vision.describe  looks at an image with a local vision model
    media.video      samples frames evenly across a video, looks at them
                     together, and transcribes the audio track — then answers
                     from both. A faithful gist, not a frame-by-frame viewing.
    media.inspect    what IS this file: type, size, and a safe text preview,
                     without you having to open it in anything.

Vision needs a vision-capable model pulled once on the machine running these:
    ollama pull llava:7b        (~4.7GB; moondream is a 1.7GB lighter option)
The main qwen2.5 model has no eyes; this one is loaded on demand.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from alfred import llm
from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
MAX_IMAGE_MB = 20
MAX_FRAMES = 6

VISION_SYSTEM = (
    llm.WORKER_SYSTEM
    + " Describe only what is actually visible. If asked a question the image "
    "cannot answer, say so rather than guessing."
)


def _vision_model(cfg: dict) -> str:
    return llm.vision_model(cfg)


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


@handler("vision.describe")
async def vision_describe(task: Task, cfg: dict) -> TaskResult:
    path = Path(str(task.inputs.get("path", ""))).expanduser()
    if not path.is_file():
        return fail(task, f"no such image: {path}")
    if path.suffix.lower() not in IMAGE_EXTS:
        return fail(task, f"{path.suffix} is not an image type I can look at")
    if path.stat().st_size > MAX_IMAGE_MB * 1024 * 1024:
        return fail(task, f"image over {MAX_IMAGE_MB}MB")

    question = task.inputs.get("question") or task.prompt or "Describe this image."
    try:
        answer = await llm.complete(
            f"{question}\n\nBe specific and concrete. Under 200 words.",
            cfg, system=VISION_SYSTEM, images=[_b64(path)],
            model=_vision_model(cfg), timeout=180,
        )
    except Exception as exc:
        return fail(
            task,
            f"vision model failed: {exc} — is it pulled? "
            f"ollama pull {_vision_model(cfg)}",
        )
    return ok(task, summary=answer, data={"file": path.name, "model": _vision_model(cfg)})


async def _ffprobe_duration(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    raw, _ = await proc.communicate()
    try:
        return float(json.loads(raw)["format"]["duration"])
    except Exception:
        return 0.0


@handler("media.video")
async def media_video(task: Task, cfg: dict) -> TaskResult:
    """Sample frames across the whole video + transcribe the audio, then
    answer from both. The honest version of "Alfred watched it"."""
    if shutil.which("ffmpeg") is None:
        return fail(task, "ffmpeg not installed (sudo apt install ffmpeg)")
    path = Path(str(task.inputs.get("path", ""))).expanduser()
    if not path.is_file():
        return fail(task, f"no such video: {path}")
    if path.suffix.lower() not in VIDEO_EXTS:
        return fail(task, f"{path.suffix} is not a video type I can watch")

    duration = await _ffprobe_duration(path)
    n_frames = min(int(task.inputs.get("frames", 5)), MAX_FRAMES)
    question = task.inputs.get("question") or task.prompt or "What happens in this video?"

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        # frames spread evenly across the runtime, not just the opening seconds
        stamps = [duration * (i + 0.5) / n_frames for i in range(n_frames)] if duration \
                 else [i * 2.0 for i in range(n_frames)]
        frames: list[str] = []
        for i, ts in enumerate(stamps):
            out = tmpdir / f"f{i}.jpg"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-ss", f"{ts:.2f}", "-i", str(path), "-frames:v", "1",
                "-vf", "scale=640:-1", "-y", str(out),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            if out.exists() and out.stat().st_size > 0:
                frames.append(_b64(out))
        if not frames:
            return fail(task, "could not extract a single frame; is the file a real video?")

        # audio track -> transcript, when the ears are installed
        transcript, transcript_note = "", ""
        wav = tmpdir / "audio.wav"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-y", str(wav),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if wav.exists() and wav.stat().st_size > 1000:
            try:
                from alfred.voice import transcribe_file
                transcript = await asyncio.to_thread(transcribe_file, str(wav))
                transcript = transcript[:4000]
            except ImportError:
                transcript_note = "audio present but not transcribed (pip install faster-whisper)"
            except Exception as exc:
                transcript_note = f"audio transcription failed: {exc}"

        prompt = (
            f"These {len(frames)} images are frames sampled evenly across a "
            f"{duration:.0f}-second video, in order.\n"
            + (f"The audio says: \"{transcript}\"\n" if transcript else "")
            + f"\nQuestion: {question}\n"
            "Answer from the frames and audio together. Say what happens in "
            "sequence. If something cannot be determined from these samples, "
            "say so. Under 250 words."
        )
        try:
            answer = await llm.complete(
                prompt, cfg, system=VISION_SYSTEM, images=frames,
                model=_vision_model(cfg), timeout=300,
            )
        except Exception as exc:
            return fail(task, f"vision model failed: {exc} — "
                              f"ollama pull {_vision_model(cfg)}")

    summary = answer + (f"\n\n({transcript_note})" if transcript_note else "")
    return ok(task, summary=summary, data={
        "file": path.name, "duration_s": round(duration, 1),
        "frames_used": len(frames), "audio_transcribed": bool(transcript),
    })


@handler("media.inspect")
async def media_inspect(task: Task, cfg: dict) -> TaskResult:
    """What is this file, without opening it in anything."""
    path = Path(str(task.inputs.get("path", ""))).expanduser()
    if not path.is_file():
        return fail(task, f"no such file: {path}")

    size = path.stat().st_size
    kind = ""
    if shutil.which("file"):
        proc = await asyncio.create_subprocess_exec(
            "file", "-b", str(path), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        raw, _ = await proc.communicate()
        kind = raw.decode(errors="replace").strip()

    head = path.read_bytes()[:4096]
    printable = sum(1 for b in head if 32 <= b < 127 or b in (9, 10, 13))
    is_texty = head and printable / len(head) > 0.85
    preview = head.decode(errors="replace")[:600] if is_texty else ""

    summary = f"{path.name}: {size/1024:.0f} KB, {kind or path.suffix or 'unknown type'}."
    if preview:
        summary += f" Begins:\n{preview}"
    else:
        summary += " Binary content; no text preview."
    return ok(task, summary=summary, data={
        "file": path.name, "bytes": size, "kind": kind, "texty": is_texty,
    })
