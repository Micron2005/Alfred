"""Hardware capabilities. Configured onto the Raspberry Pi.

The ESP32s keep speaking MQTT to Mosquitto exactly as they do now. This
handler is a thin bridge, not a migration — do not make microcontrollers
learn your control protocol.

Actuation is the one place in the whole system where a retry is dangerous.
A repeated web search wastes a few seconds; a repeated "rotate joint 3 by
90 degrees" moves the arm twice. Every actuating task must carry an
idempotency_key, and this handler refuses the ones that do not.
"""

from __future__ import annotations

import asyncio
import json

# Module level: no paho, no hw.* capabilities advertised. See research.py.
import paho.mqtt.client as mqtt

from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok


def _client(cfg: dict):
    hw = cfg.get("hardware", {})
    client = mqtt.Client()
    if hw.get("mqtt_user"):
        client.username_pw_set(hw["mqtt_user"], hw.get("mqtt_password", ""))
    client.connect(hw.get("mqtt_host", "127.0.0.1"), hw.get("mqtt_port", 1883), 30)
    return client


@handler("hw.mqtt")
async def hw_mqtt(task: Task, cfg: dict) -> TaskResult:
    """Publish a command and wait for the device to confirm its new state.

    Fire-and-forget is not good enough: without an acknowledgement, Alfred
    cannot tell "the arm moved" from "the arm is unplugged".
    """
    topic = task.inputs.get("topic")
    payload = task.inputs.get("payload")
    if not topic or payload is None:
        return fail(task, "hw.mqtt needs inputs.topic and inputs.payload")
    if not task.idempotency_key:
        return fail(task, "refusing actuation without an idempotency_key")

    ack_topic = task.inputs.get("ack_topic")
    ack_timeout = float(task.inputs.get("ack_timeout_s", 10))
    body = payload if isinstance(payload, str) else json.dumps(payload)

    loop = asyncio.get_running_loop()
    ack: asyncio.Future = loop.create_future()

    def _run() -> None:
        client = _client(cfg)
        if ack_topic:
            def on_message(_c, _u, msg):
                if not ack.done():
                    loop.call_soon_threadsafe(
                        ack.set_result, msg.payload.decode(errors="replace")
                    )
            client.on_message = on_message
            client.subscribe(ack_topic)
        client.loop_start()
        client.publish(topic, body, qos=1).wait_for_publish()
        if not ack_topic:
            loop.call_soon_threadsafe(ack.set_result, None)

    await asyncio.to_thread(_run)

    try:
        confirmation = await asyncio.wait_for(ack, ack_timeout)
    except asyncio.TimeoutError:
        return fail(task, f"published to {topic} but no ack on {ack_topic} in {ack_timeout}s")

    return ok(
        task,
        summary=(
            f"Published to {topic}."
            + (f" Device confirmed: {confirmation}" if confirmation else " No ack requested.")
        ),
        data={"topic": topic, "ack": confirmation},
    )


@handler("hw.sensor")
async def hw_sensor(task: Task, cfg: dict) -> TaskResult:
    """Sample retained sensor topics. Read-only, so retries are free."""
    topics: list[str] = task.inputs.get("topics", [])
    if not topics:
        return fail(task, "hw.sensor needs inputs.topics")

    window = float(task.inputs.get("window_s", 5))
    readings: dict[str, str] = {}
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    def _run() -> None:
        client = _client(cfg)

        def on_message(_c, _u, msg):
            readings[msg.topic] = msg.payload.decode(errors="replace")
            if len(readings) >= len(topics) and not done.done():
                loop.call_soon_threadsafe(done.set_result, True)

        client.on_message = on_message
        for topic in topics:
            client.subscribe(topic)
        client.loop_start()

    await asyncio.to_thread(_run)
    try:
        await asyncio.wait_for(done, window)
    except asyncio.TimeoutError:
        pass  # partial readings are still worth reporting

    missing = [t for t in topics if t not in readings]
    return ok(
        task,
        summary=(
            ", ".join(f"{k}={v}" for k, v in readings.items()) or "no readings"
        ) + (f" | silent: {', '.join(missing)}" if missing else ""),
        data={"readings": readings, "silent": missing},
    )
