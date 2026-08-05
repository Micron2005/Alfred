"""Research capabilities. Configured onto the MacBook.

This is the clearest example of a worker as a context firewall. The MacBook
reads 200 pages of datasheets and hands back 300 words of extracted figures.
Alfred never sees the 200 pages. That, not spare CPU, is why the node exists.
"""

from __future__ import annotations

import json
from pathlib import Path

# Module level on purpose. If either is missing, this whole module fails to
# import, the registry logs it, and the worker simply never advertises
# research.*. Importing inside the function instead would let a worker claim
# work it cannot do and only discover the problem afterwards.
import httpx
import pypdf

from alfred import llm
from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok

EXTRACT_SYSTEM = (
    llm.WORKER_SYSTEM
    + " Every factual claim you output must be traceable to the supplied text. "
    "If the text does not contain the answer, say so instead of inferring."
)


@handler("research.document")
async def research_document(task: Task, cfg: dict) -> TaskResult:
    """Read local PDFs and text, return only what was asked for.

    """
    paths = [Path(p) for p in task.inputs.get("paths", [])]
    if not paths:
        return fail(task, "no paths supplied")

    chunks: list[str] = []
    pages_read = 0
    for path in paths:
        if not path.exists():
            chunks.append(f"[missing: {path}]")
            continue
        if path.suffix.lower() == ".pdf":
            reader = pypdf.PdfReader(str(path))
            for page_no, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                if text:
                    chunks.append(f"[{path.name} p{page_no}]\n{text}")
                pages_read += 1
        else:
            chunks.append(f"[{path.name}]\n{path.read_text(errors='replace')}")

    corpus = "\n\n".join(chunks)
    # Crude but honest windowing. Swap for embedding retrieval when the
    # corpus outgrows the worker's context.
    budget = int(task.inputs.get("char_budget", 60000))
    truncated = len(corpus) > budget

    answer = await llm.complete(
        f"Question: {task.prompt}\n\nSource text:\n{corpus[:budget]}\n\n"
        "Answer in under 300 words. Cite the bracketed page markers for every "
        "figure you give. List anything the sources do not settle.",
        cfg,
        system=EXTRACT_SYSTEM,
    )

    return ok(
        task,
        summary=answer,
        data={
            "pages_read": pages_read,
            "chars_read": len(corpus),
            "truncated": truncated,
            "sources": [p.name for p in paths],
        },
    )


@handler("research.web")
async def research_web(task: Task, cfg: dict) -> TaskResult:
    """Fetch and distil web sources.

    Left deliberately thin — plug in whatever search you prefer. The contract
    that matters is the return shape: findings short, every claim carrying a
    URL, unknowns stated rather than smoothed over.
    """
    urls: list[str] = task.inputs.get("urls", [])
    if not urls:
        # Self-hosted SearXNG on the Pi: no API key, no rate limit, no quota
        # to run out of in the middle of a long research task.
        #   docker run -d -p 8888:8080 searxng/searxng
        search_url = cfg.get("research", {}).get("searxng_url")
        if not search_url:
            return fail(task, "no inputs.urls and no research.searxng_url configured")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    search_url + "/search",
                    params={"q": task.inputs.get("query", task.prompt),
                            "format": "json", "language": "en"},
                )
                urls = [r["url"] for r in resp.json().get("results", [])[:6]]
        except Exception as exc:
            return fail(task, f"search failed: {exc}")
    if not urls:
        return fail(task, "search returned no results")

    pages: list[str] = []
    fetched: list[str] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for url in urls[:8]:
            try:
                resp = await client.get(url)
                pages.append(f"[{url}]\n{resp.text[:20000]}")
                fetched.append(url)
            except Exception as exc:
                pages.append(f"[{url}] fetch failed: {exc}")

    findings = await llm.complete(
        f"Question: {task.prompt}\n\nSources:\n" + "\n\n".join(pages) + "\n\n"
        "Give findings in under 300 words. Attach the source URL to every "
        "claim. State plainly what remains unanswered.",
        cfg,
        system=EXTRACT_SYSTEM,
    )

    notes = Path(cfg["core"]["artifact_dir"]) / task.id
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "sources.json").write_text(json.dumps(fetched, indent=2))

    return ok(
        task,
        summary=findings,
        artifacts=[(notes / "sources.json").as_uri()],
        data={"fetched": fetched, "requested": len(urls)},
    )
