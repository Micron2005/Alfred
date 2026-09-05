"""Research capabilities. Configured onto the MacBook.

This is the clearest example of a worker as a context firewall. The MacBook
reads 200 pages of datasheets and hands back 300 words of extracted figures.
Alfred never sees the 200 pages. That, not spare CPU, is why the node exists.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

# Module level on purpose. If either is missing, this whole module fails to
# import, the registry logs it, and the worker simply never advertises
# research.*. Importing inside the function instead would let a worker claim
# work it cannot do and only discover the problem afterwards.
import httpx
import pypdf

from alfred import llm
from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok, refuse

EXTRACT_SYSTEM = (
    llm.WORKER_SYSTEM
    + " Every factual claim you output must be traceable to the supplied text. "
    "If the text does not contain the answer, say so instead of inferring."
)

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 Alfred/0.1")
DDG_HTML = "https://html.duckduckgo.com/html/"
PAGE_CHARS = 12000


class _Text(HTMLParser):
    """Visible text only. Raw HTML is mostly markup and script; feeding it
    to the model spends the context budget on angle brackets."""
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            chunk = " ".join(data.split())
            if chunk:
                self.parts.append(chunk)


def _visible_text(html: str) -> str:
    parser = _Text()
    try:
        parser.feed(html)
    except Exception:
        return html
    return "\n".join(parser.parts)


async def _searxng(client: httpx.AsyncClient, base: str, query: str) -> list[str]:
    resp = await client.get(base + "/search",
                            params={"q": query, "format": "json", "language": "en"})
    return [r["url"] for r in resp.json().get("results", [])[:6]]


async def _duckduckgo(client: httpx.AsyncClient, query: str) -> list[str]:
    """No-key fallback when the household has no SearXNG. Scrapes the HTML
    results page; brittle by nature, so it fails loudly rather than quietly
    returning nothing."""
    resp = await client.get(DDG_HTML, params={"q": query})
    resp.raise_for_status()
    urls: list[str] = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', resp.text):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        target = qs.get("uddg", [href])[0]
        if target.startswith("http") and "duckduckgo.com" not in target:
            urls.append(target)
    return urls[:6]


@handler("research.document")
async def research_document(task: Task, cfg: dict) -> TaskResult:
    """Read local PDFs and text, return only what was asked for.

    """
    paths = [Path(p) for p in task.inputs.get("paths", [])]
    if not paths:
        return refuse(task, "no paths supplied")

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
            pages_read += 1

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
    engine = "given urls"
    if not urls:
        # Self-hosted SearXNG on the Pi: no API key, no rate limit, no quota
        # to run out of in the middle of a long research task.
        #   docker run -d -p 8888:8080 searxng/searxng
        # Without one, DuckDuckGo's HTML page is the no-key fallback.
        query = str(task.inputs.get("query") or task.prompt)
        search_url = cfg.get("research", {}).get("searxng_url")
        errors: list[str] = []
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT},
                                     follow_redirects=True) as client:
            if search_url:
                try:
                    urls, engine = await _searxng(client, search_url, query), "searxng"
                except Exception as exc:
                    errors.append(f"searxng at {search_url}: {exc}")
            if not urls:
                try:
                    urls, engine = await _duckduckgo(client, query), "duckduckgo"
                except Exception as exc:
                    errors.append(f"duckduckgo: {exc}")
        if not urls:
            return fail(task, "search returned no results"
                        + (f" ({'; '.join(errors)})" if errors else ""))

    pages: list[str] = []
    fetched: list[str] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT}) as client:
        for url in urls[:8]:
            try:
                resp = await client.get(url)
                if resp.status_code >= 400:
                    pages.append(f"[{url}] fetch failed: HTTP {resp.status_code}")
                    continue
                text = _visible_text(resp.text) if "html" in resp.headers.get(
                    "content-type", "html") else resp.text
                pages.append(f"[{url}]\n{text[:PAGE_CHARS]}")
                fetched.append(url)
            except Exception as exc:
                pages.append(f"[{url}] fetch failed: {exc}")

    findings = await llm.complete(
        f"Question: {task.prompt}\n\nSources:\n" + "\n\n".join(pages) + "\n\n"
        "Give findings in under 300 words. Attach the source URL to every "
        "claim. For prices, plans or specs, a vendor's own page outranks any "
        "third-party page; where only third parties were read, say the figure "
        "is second-hand and may be out of date. State plainly what remains "
        "unanswered.",
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
        data={"fetched": fetched, "requested": len(urls), "engine": engine},
    )
