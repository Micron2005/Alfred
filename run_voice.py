#!/usr/bin/env python3
"""Alfred with a voice. Desktop only, same core, different interface.

    python3 run_voice.py --config configs/desktop.toml --project <id>

Press Enter, speak, pause — he transcribes, thinks, answers on screen and
aloud. Type instead of pressing bare Enter and it is treated as a typed
message, so the keyboard never stops working. All the /commands from
run_core.py work here too.

Voice is an interface, not a capability: nothing about the bus, workers, or
memory changes. This file is run_core.py with ears and a mouth.
"""

import argparse
import asyncio
import json
import logging

from alfred.bus import build_bus
from alfred.capabilities import eligible_for
from alfred.config import load
from alfred.contracts import Assignment
from alfred.core.alfred import Alfred
from alfred.voice import Ears, Mouth, say_async
from alfred.worker.runtime import WorkerRuntime
from run_core import handle_command


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/desktop.toml")
    ap.add_argument("--project", default=None)
    ap.add_argument("--new-project", default=None)
    ap.add_argument("--voice", default="en_GB-alan-medium",
                    help="any piper voice id, e.g. en_GB-northern_english_male-medium")
    ap.add_argument("--whisper", default="base.en",
                    help="tiny.en is faster, small.en is more accurate")
    ap.add_argument("--mute", action="store_true", help="ears only, no spoken replies")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)  # quiet console; his words are the output

    cfg = load(args.config)
    bus = build_bus(cfg)
    await bus.connect()

    alfred = Alfred(bus, cfg)
    project_id = args.project
    if args.new_project:
        project_id = alfred.state.create_project(args.new_project)
        print(f"created project {project_id}")

    background = [asyncio.create_task(alfred.supervise())]
    if cfg["worker"]["capabilities"]:
        background.append(asyncio.create_task(WorkerRuntime(bus, cfg).run()))

    ears = Ears(model_size=args.whisper)
    mouth = Mouth(voice=args.voice)

    print("Alfred is listening. Enter = talk, or just type. /help for commands.\n")
    try:
        while True:
            typed = (await asyncio.to_thread(input, "[enter to talk] > ")).strip()
            if typed.lower() in {"quit", "exit"}:
                break
            if typed.startswith("/"):
                print("\n" + await handle_command(alfred, typed) + "\n")
                continue

            if typed:
                message = typed
            else:
                print("  listening...")
                audio = await asyncio.to_thread(ears.record)
                if audio is None:
                    print("  heard nothing\n")
                    continue
                message = await asyncio.to_thread(ears.transcribe, audio)
                if not message:
                    print("  could not make that out\n")
                    continue
                print(f"  you: {message}")

            print()
            reply = await alfred.converse(message, project_id)
            print(reply + "\n")
            if not args.mute:
                await say_async(mouth, reply)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        for task in background:
            task.cancel()
        await bus.close()


if __name__ == "__main__":
    asyncio.run(main())
