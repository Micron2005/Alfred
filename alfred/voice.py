"""Alfred's ears and mouth. Desktop only, like everything user-facing.

Fully local by design:

    ears  — faster-whisper (OpenAI Whisper, CT2 runtime). CPU int8 is fast
            enough that the GPU stays free for Alfred's own model.
    mouth — Piper TTS via its CLI. CPU, near-instant, and the en_GB voices
            suit him. The CLI is used instead of the Python API because the
            API surface has changed between piper-tts releases and the CLI
            has not.

Push-to-talk, not a wake word. Wake words are the flakiest part of every
home assistant; a butler who occasionally answers the television is worse
than one you summon deliberately. Recording stops on trailing silence.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("alfred.voice")

SAMPLE_RATE = 16000  # what whisper expects
FRAME_MS = 30
DEFAULT_VOICE = "en_GB-alan-medium"


# ---------------------------------------------------------------------------
# Listening
# ---------------------------------------------------------------------------

class Ears:
    def __init__(
        self,
        model_size: str = "base.en",
        silence_after_s: float = 1.2,
        max_utterance_s: float = 30.0,
        energy_floor: float = 0.006,
    ) -> None:
        self.silence_after_s = silence_after_s
        self.max_utterance_s = max_utterance_s
        self.energy_floor = energy_floor
        self._model = None
        self._model_size = model_size

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # deferred: slow import

            log.info("loading whisper %s (first run downloads it)", self._model_size)
            # int8 on CPU on purpose — the GPU belongs to Alfred's own model.
            self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
        return self._model

    def record(self) -> np.ndarray | None:
        """Record one utterance: start immediately, stop on trailing silence.

        Returns float32 mono at 16kHz, or None if nothing was said.
        """
        import sounddevice as sd

        frame_len = int(SAMPLE_RATE * FRAME_MS / 1000)
        frames: list[np.ndarray] = []
        started_speaking = False
        silent_for = 0.0
        began = time.time()

        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=frame_len
        ) as stream:
            while True:
                frame, _ = stream.read(frame_len)
                frame = frame.reshape(-1)
                frames.append(frame)
                loud = float(np.sqrt(np.mean(frame**2))) > self.energy_floor

                if loud:
                    started_speaking = True
                    silent_for = 0.0
                elif started_speaking:
                    silent_for += FRAME_MS / 1000
                    if silent_for >= self.silence_after_s:
                        break

                elapsed = time.time() - began
                if elapsed > self.max_utterance_s:
                    break
                if not started_speaking and elapsed > 6.0:
                    return None  # opened the mic and said nothing

        audio = np.concatenate(frames)
        # Trim the trailing silence so whisper is not asked to transcribe it.
        keep = len(audio) - int(self.silence_after_s * SAMPLE_RATE * 0.8)
        return audio[: max(keep, frame_len)]

    def transcribe(self, audio: np.ndarray) -> str:
        model = self._load()
        segments, _info = model.transcribe(
            audio, language="en", beam_size=1, vad_filter=True
        )
        return " ".join(s.text.strip() for s in segments).strip()


# ---------------------------------------------------------------------------
# Speaking
# ---------------------------------------------------------------------------

def _find_player() -> list[str] | None:
    if shutil.which("aplay"):
        return ["aplay", "-q"]
    if shutil.which("paplay"):
        return ["paplay"]
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
    return None


def speakable(text: str) -> str:
    """What Alfred says aloud is not identical to what he prints.

    Code blocks are unreadable as speech; markdown furniture is noise. The
    screen keeps the full reply — this is only the spoken rendition.
    """
    out = text
    n_blocks = len(re.findall(r"```", out)) // 2
    out = re.sub(r"```.*?```", " The code is on screen. ", out, flags=re.DOTALL)
    out = re.sub(r"`([^`]*)`", r"\1", out)
    out = re.sub(r"^#+\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", out)
    out = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", out)          # links -> text
    out = re.sub(r"^\s*[-•]\s*", "", out, flags=re.MULTILINE)   # bullets
    out = re.sub(r"file://\S+", "a file on disk", out)
    out = re.sub(r"\s+", " ", out).strip()
    if n_blocks > 1:
        out = out.replace(" The code is on screen. ", " ", n_blocks - 1)
    return out


class Mouth:
    def __init__(self, voice: str = DEFAULT_VOICE, data_dir: str | None = None) -> None:
        self.voice = voice
        self.data_dir = Path(data_dir or "~/.alfred/voices").expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._player = _find_player()
        self._ready = False

    def _ensure_voice(self) -> None:
        """Download the voice model once. ~60MB, then permanent."""
        if self._ready:
            return
        if not any(self.data_dir.glob(f"{self.voice}*.onnx")):
            log.info("downloading piper voice %s (one time)", self.voice)
            subprocess.run(
                ["python3", "-m", "piper.download_voices",
                 "--data-dir", str(self.data_dir), self.voice],
                check=True, capture_output=True,
            )
        self._ready = True

    def say(self, text: str) -> None:
        text = speakable(text)
        if not text:
            return
        if self._player is None:
            log.warning("no audio player found (install alsa-utils); staying silent")
            return
        self._ensure_voice()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav = tmp.name
        try:
            subprocess.run(
                ["piper", "--data-dir", str(self.data_dir),
                 "--model", self.voice, "--output_file", wav],
                input=text.encode(), check=True, capture_output=True,
            )
            subprocess.run(self._player + [wav], check=False)
        except FileNotFoundError:
            log.warning("piper not on PATH; pip install piper-tts")
        except subprocess.CalledProcessError as exc:
            log.warning("piper failed: %s", exc.stderr.decode(errors="replace")[:200])
        finally:
            Path(wav).unlink(missing_ok=True)


# ---------------------------------------------------------------------------

async def say_async(mouth: Mouth, text: str) -> None:
    """Playback off the event loop, so the supervisor keeps ticking while
    Alfred talks."""
    await asyncio.to_thread(mouth.say, text)


# ---------------------------------------------------------------------------
# Module-level helpers for the shell server: speak text to wav bytes, and
# transcribe an uploaded audio file. Lazy singletons; honest errors when the
# libraries are not installed.
# ---------------------------------------------------------------------------

_EARS_MODEL = None

def transcribe_file(path: str, model_size: str = "base.en") -> str:
    """Transcribe any audio file (webm/ogg/wav — decoded by faster-whisper).
    Raises ImportError when faster-whisper is absent."""
    global _EARS_MODEL
    from faster_whisper import WhisperModel

    if _EARS_MODEL is None:
        _EARS_MODEL = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = _EARS_MODEL.transcribe(str(path), vad_filter=True, beam_size=1)
    return " ".join(seg.text.strip() for seg in segments).strip()


def synth_wav(text: str, voice: str = DEFAULT_VOICE) -> bytes:
    """Text to wav bytes via Piper. Raises RuntimeError when piper is absent."""
    if shutil.which("piper") is None:
        raise RuntimeError("piper not installed (pip install piper-tts)")
    data_dir = Path("~/.alfred/voices").expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    if not any(data_dir.glob(f"{voice}*.onnx")):
        subprocess.run(
            ["python3", "-m", "piper.download_voices", "--data-dir", str(data_dir), voice],
            check=True, capture_output=True,
        )
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav = tmp.name
    try:
        subprocess.run(
            ["piper", "--data-dir", str(data_dir), "--model", voice, "--output_file", wav],
            input=speakable(text).encode(), check=True, capture_output=True,
        )
        return Path(wav).read_bytes()
    finally:
        Path(wav).unlink(missing_ok=True)
