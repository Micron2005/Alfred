"""Model access. Stdlib only, so it runs anywhere including the Pi.

Two providers, chosen per-call-site by config:

    ollama  — local, via Ollama's HTTP API
    openai  — any OpenAI-compatible endpoint (hosted, LM Studio, llama.cpp
              server, OpenRouter, vLLM)

This split exists because of a specific constraint: an 8GB card fits a 7B
model, and a 7B model is a weak planner. Alfred's core can point at a hosted
endpoint for planning and synthesis while every worker stays local — the
inference layout becomes a config decision instead of an architectural one.

The system prompt is the asymmetry that keeps there being one Alfred:

    Alfred core -> persona.md, memory, speaks to the user
    Worker      -> WORKER_SYSTEM, no memory, returns an artifact
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

WORKER_SYSTEM = (
    "You are a task executor inside an automated pipeline. You have no name, "
    "no persona and no memory of anything outside this task. Do not greet, "
    "apologise, editorialise, or address a user. Return only the requested "
    "artifact. If the task is impossible with the given inputs, say exactly "
    "what is missing in one sentence."
)


def _post(url: str, payload: dict, timeout: int, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _ollama(prompt, system, model, cfg, timeout, json_mode, images=None):
    options = {"temperature": 0.1 if json_mode else 0.7}
    # num_ctx and num_predict, when set, cap context and output length. On an
    # 8GB card a smaller context is markedly faster and rarely a real limit
    # for conversation; they are configurable so heavy tasks can raise them.
    if cfg.get("num_ctx"):
        options["num_ctx"] = cfg["num_ctx"]
    if cfg.get("num_predict"):
        options["num_predict"] = cfg["num_predict"]
    payload = {
        "model": model, "prompt": prompt, "system": system, "stream": False,
        "options": options,
        # keep_alive: hold the model in VRAM between messages so a paused
        # conversation does not pay a full reload on the next word. The
        # single biggest latency win on a local box. "30m" by default; set
        # "-1" to pin it forever, "0" to unload immediately after each call.
        "keep_alive": cfg.get("keep_alive", "30m"),
    }
    if images:
        payload["images"] = images  # base64 strings; needs a vision model
    if json_mode:
        payload["format"] = "json"
    url = cfg.get("ollama_url", "http://127.0.0.1:11434") + "/api/generate"
    return url, payload, {}, lambda d: d.get("response", "")


def _openai(prompt, system, model, cfg, timeout, json_mode, images=None):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "temperature": 0.1 if json_mode else 0.7,
        "max_tokens": cfg.get("max_tokens", 4096),
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    key = os.environ.get(cfg.get("api_key_env", "ALFRED_API_KEY"), "")
    url = cfg.get("api_base", "https://api.openai.com/v1") + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return url, payload, headers, lambda d: d["choices"][0]["message"]["content"]


PROVIDERS = {"ollama": _ollama, "openai": _openai}


async def complete(
    prompt: str, cfg: dict, system: str = WORKER_SYSTEM,
    model: str | None = None, timeout: int = 300, json_mode: bool = False,
    section: str = "core", images: list[str] | None = None,
) -> str:
    """`section` selects which config block supplies the provider settings, so
    a worker can use local Ollama while the core calls somewhere else."""
    settings = cfg.get(section) or cfg.get("core", {})
    provider = settings.get("provider", "ollama")
    build = PROVIDERS.get(provider)
    if build is None:
        raise ValueError(f"unknown provider {provider!r}")

    chosen = model or settings.get("model", "qwen2.5:7b")
    url, payload, headers, extract = build(
        prompt, system, chosen, settings, timeout, json_mode, images=images
    )
    try:
        data = await asyncio.to_thread(_post, url, payload, timeout, headers)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{provider} returned {exc.code}: {exc.read()[:300]!r}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{provider} unreachable at {url}: {exc}") from exc
    return (extract(data) or "").strip()


async def complete_json(prompt: str, cfg: dict, system: str = WORKER_SYSTEM, **kw):
    raw = await complete(prompt, cfg, system=system, json_mode=True, **kw)
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)
