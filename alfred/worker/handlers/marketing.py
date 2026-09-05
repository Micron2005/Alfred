"""Marketing capabilities. Light enough for any node; stdlib only.

Two jobs, kept deliberately separate:

    marketing.audit   fetch a live page and check the things a search engine
                      or a shared link actually sees — title, description,
                      Open Graph tags, headline, sitemap. Mechanical checks
                      first, then a short critique of the message itself.
    marketing.draft   write copy — a post, a cold email, ad lines, a landing
                      hero, a comparison page, a weekly plan — grounded in a
                      product brief so the facts (price, audience, proof)
                      come from a file the owner controls, not from the model.

Briefs are markdown files, one per product, in configs/briefs/. The brief is
the memory that makes the drafts consistent from week to week; without one
the worker still drafts, but says so, and the planner's verifier flags the
result as ungrounded.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from alfred import llm
from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok

DEFAULT_BRIEFS_DIR = Path(__file__).resolve().parents[3] / "configs" / "briefs"
FETCH_TIMEOUT = 20
PAGE_TEXT_BUDGET = 12000
USER_AGENT = "Mozilla/5.0 (compatible; Alfred marketing audit)"

DRAFT_SYSTEM = (
    llm.WORKER_SYSTEM
    + " You write marketing copy for a small, founder-run software company. "
    "Every fact — price, feature, audience, claim — must come from the brief "
    "or the task; never invent numbers, testimonials, customers or social "
    "proof of any kind ('many founders', 'shops love it', 'trusted by') "
    "unless the brief states it. Plain, "
    "specific, confident. No hype words, no exclamation marks, no emoji "
    "unless the channel is explicitly stated to want them."
)

AUDIT_SYSTEM = (
    llm.WORKER_SYSTEM
    + " You critique a web page's marketing message from its visible text. "
    "Be blunt and specific. Quote the page where you can. Do not comment on "
    "anything you cannot see in the supplied text."
)

DRAFT_KINDS = {
    "post": "a social media post",
    "thread": "a multi-part social thread",
    "email": "a cold outreach email",
    "followup": "a follow-up email to someone who went quiet",
    "ad": "short paid-ad copy (headline + body variants)",
    "landing": "landing page hero copy (headline, subhead, CTA, three proof points)",
    "comparison": "a 'versus' comparison page against a named competitor",
    "script": "a 30-second video script with on-screen actions",
    "plan": "a one-week marketing plan with daily actions and where to post",
    "answer": "a helpful forum or group reply that mentions the product once, honestly",
}


# --------------------------------------------------------------------------
# Briefs
# --------------------------------------------------------------------------

def briefs_dir(cfg: dict) -> Path:
    configured = cfg.get("marketing", {}).get("briefs_dir")
    return Path(configured).expanduser() if configured else DEFAULT_BRIEFS_DIR


def _brief_files(cfg: dict) -> list[Path]:
    folder = briefs_dir(cfg)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.md") if not p.name.startswith("README"))


def list_briefs(cfg: dict) -> list[str]:
    return [p.stem for p in _brief_files(cfg)]


def load_brief(cfg: dict, product: str | None) -> tuple[str | None, str]:
    """Return (brief_name, brief_text). Picks the only brief when there is
    exactly one and none was named; otherwise a missing brief is a missing
    brief — the draft proceeds ungrounded and says so."""
    briefs = _brief_files(cfg)
    if product:
        slug = product.strip().lower().replace(" ", "-")
        for path in briefs:
            if path.stem.lower() == slug or slug in path.stem.lower():
                return path.stem, path.read_text(errors="replace")
        return None, ""
    if len(briefs) == 1:
        return briefs[0].stem, briefs[0].read_text(errors="replace")
    return None, ""


# --------------------------------------------------------------------------
# marketing.draft
# --------------------------------------------------------------------------

@handler("marketing.draft")
async def marketing_draft(task: Task, cfg: dict) -> TaskResult:
    kind = str(task.inputs.get("kind", "post")).lower()
    kind_desc = DRAFT_KINDS.get(kind, kind)
    channel = task.inputs.get("channel", "")
    audience = task.inputs.get("audience", "")
    goal = task.inputs.get("goal", "")
    count = _int(task.inputs.get("count"), 1, 1, 5)

    brief_name, brief = load_brief(cfg, task.inputs.get("product"))
    grounding = (
        f"Product brief ({brief_name}):\n{brief}" if brief
        else "No product brief is on file. Use ONLY facts stated in the task; "
             "where a fact is needed and missing, write [FILL IN] rather than guess. "
             "Make no claims about results, adoption or satisfaction."
    )
    upstream = task.inputs.get("upstream") or []
    research = ("\n\nFindings from earlier steps:\n" + "\n".join(upstream)) if upstream else ""

    spec = [f"Write {count} version(s) of {kind_desc}."]
    if channel:
        spec.append(f"Channel: {channel} — match its length and conventions.")
    if audience:
        spec.append(f"Audience: {audience}.")
    if goal:
        spec.append(f"Goal: {goal}.")
    spec.append("Task: " + task.prompt)

    text = await llm.complete(
        f"{grounding}{research}\n\n" + "\n".join(spec) + "\n\n"
        "Markdown. Separate versions with a '---' line. After the copy, add a "
        "short 'Notes' section: which brief facts you used, and anything the "
        "brief did not settle.",
        cfg,
        system=DRAFT_SYSTEM,
    )

    directory = Path(cfg["core"]["artifact_dir"]).expanduser() / task.id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / task.inputs.get("filename", f"{kind}.md")
    path.write_text(text)

    # The owner wants to read the copy, not a description of it. Summaries
    # carry it whole when it fits, and the artifact always has the full text.
    summary = text if len(text) <= 2500 else text[:2500] + f"\n\n[continues in {path.name}]"
    return ok(
        task,
        summary=summary,
        artifacts=[path.as_uri()],
        data={"kind": kind, "brief": brief_name, "words": len(text.split())},
    )


# --------------------------------------------------------------------------
# marketing.audit
# --------------------------------------------------------------------------

class _PageParser(HTMLParser):
    """Pull out what search engines and link previews read, plus visible text."""

    SKIP = {"script", "style", "noscript", "svg", "template"}
    VOID = {"meta", "link", "img", "br", "hr", "input", "source", "wbr", "area", "base", "col", "embed", "track"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.h1: list[str] = []
        self.h2: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.images_without_alt = 0
        self.text: list[str] = []
        self._stack: list[str] = []
        self._current_link: str | None = None
        self._link_text: list[str] = []
        self._heading: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in self.VOID:
            self._stack.append(tag)
        if tag == "meta":
            key = a.get("name") or a.get("property")
            if key and a.get("content") is not None:
                self.meta[key.lower()] = a["content"].strip()
        elif tag == "link" and (a.get("rel") or "").lower() == "canonical":
            self.meta["canonical"] = a.get("href", "")
        elif tag == "a":
            self._current_link = a.get("href", "")
            self._link_text = []
        elif tag == "img" and not (a.get("alt") or "").strip():
            self.images_without_alt += 1

    def handle_endtag(self, tag):
        if tag == "a" and self._current_link is not None:
            self.links.append((self._current_link, " ".join(self._link_text).strip()))
            self._current_link = None
        if tag in ("h1", "h2") and tag in self._stack:
            heading = " ".join(self._heading).strip()
            if heading:
                (self.h1 if tag == "h1" else self.h2).append(heading)
            self._heading = []
        while self._stack and self._stack.pop() != tag:
            pass

    def handle_data(self, data):
        if any(t in self.SKIP for t in self._stack):
            return
        chunk = " ".join(data.split())
        if not chunk:
            return
        if "title" in self._stack and not self.title:
            self.title = chunk
            return
        if "h1" in self._stack or "h2" in self._stack:
            self._heading.append(chunk)
        if self._current_link is not None:
            self._link_text.append(chunk)
        self.text.append(chunk)


def _fetch(url: str) -> tuple[int, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            body = resp.read(2_000_000).decode("utf-8", errors="replace")
            return resp.status, resp.geturl(), body
    except urllib.error.HTTPError as exc:
        return exc.code, url, ""
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"could not fetch {url}: {exc}") from exc


def _probe(url: str, want: str) -> str:
    """Status of a well-known file, or why it does not count. A sitemap that
    redirects to the homepage answers 200 and is still not a sitemap."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if resp.geturl().rstrip("/") != url.rstrip("/"):
                return f"redirects to {resp.geturl()}"
            if want not in ctype.lower():
                return f"served as {ctype.split(';')[0] or 'unknown type'}, not {want}"
            return "ok"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except Exception as exc:
        return f"unreachable ({exc.__class__.__name__})"


CTA_WORDS = ("trial", "sign up", "signup", "get started", "start", "book", "demo",
             "buy", "try", "subscribe", "contact", "download")


def _checks(page: _PageParser, final_url: str, sitemap: str,
            robots: str) -> tuple[list[str], dict]:
    """Deterministic findings. Each is a plain sentence the owner can act on."""
    issues: list[str] = []
    m = page.meta
    title = page.title
    desc = m.get("description", "")
    host = urllib.parse.urlparse(final_url).netloc

    if not title:
        issues.append("No <title>. Search results will show the bare URL.")
    elif len(title) < 20 or title.lower().strip() == host.split(".")[0].lower():
        issues.append(f"Title is just '{title}' — say what it is and for whom, "
                      "e.g. 'Brand – Category software for Audience'. Aim for 30–60 characters.")
    elif len(title) > 65:
        issues.append(f"Title is {len(title)} characters; search engines cut it near 60.")

    if not desc:
        issues.append("No meta description. Search engines will pick a random sentence.")
    elif len(desc) < 70:
        issues.append(f"Meta description is only {len(desc)} characters ('{desc}'). "
                      "Use 120–160: who it is for, what it does, the price or hook.")
    elif len(desc) > 165:
        issues.append(f"Meta description is {len(desc)} characters; it will be truncated near 160.")

    for key, why in (("og:title", "headline"), ("og:description", "summary"),
                     ("og:image", "preview image")):
        if not m.get(key):
            issues.append(f"Missing {key}: links shared on Facebook, LinkedIn, Slack or "
                          f"iMessage will show no {why}.")
    if not m.get("twitter:card"):
        issues.append("Missing twitter:card; X/Twitter links will show as plain text.")

    if not page.h1:
        issues.append("No <h1>. The page has no stated headline for search engines.")
    elif len(page.h1) > 1:
        issues.append(f"{len(page.h1)} <h1> tags; keep one headline per page.")

    if not m.get("canonical"):
        issues.append("No canonical link; www/non-www and trailing-slash variants "
                      "may compete with each other in search.")

    ctas = [t for _, t in page.links if any(w in t.lower() for w in CTA_WORDS)]
    if not ctas:
        issues.append("No obvious call-to-action link text (trial, sign up, demo...).")

    if sitemap != "ok":
        issues.append(f"/sitemap.xml is not a real sitemap ({sitemap}); publish one and "
                      "submit it in Google Search Console so inner pages get indexed.")
    if robots != "ok":
        issues.append(f"/robots.txt is not served ({robots}).")

    words = sum(len(t.split()) for t in page.text)
    if words < 300:
        issues.append(f"Only about {words} words of visible text; thin pages rank poorly.")
    if page.images_without_alt:
        issues.append(f"{page.images_without_alt} image(s) without alt text.")

    facts = {
        "title": title, "title_length": len(title),
        "description": desc, "description_length": len(desc),
        "og": {k: v for k, v in m.items() if k.startswith(("og:", "twitter:"))},
        "canonical": m.get("canonical", ""),
        "h1": page.h1, "h2": page.h2[:12],
        "cta_links": sorted(set(ctas))[:10],
        "words": words,
        "sitemap": sitemap, "robots": robots,
    }
    return issues, facts


@handler("marketing.audit")
async def marketing_audit(task: Task, cfg: dict) -> TaskResult:
    url = str(task.inputs.get("url") or _first_url(task.prompt) or "").strip()
    if not url:
        return fail(task, "no inputs.url and no URL in the prompt")
    if not re.match(r"^https?://", url):
        url = "https://" + url

    try:
        status, final_url, body = _fetch(url)
    except RuntimeError as exc:
        return fail(task, str(exc))
    if status >= 400 or not body:
        return fail(task, f"{url} returned HTTP {status}")

    page = _PageParser()
    page.feed(body)
    root = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(final_url))
    sitemap = _probe(root + "/sitemap.xml", "xml")
    robots = _probe(root + "/robots.txt", "text/plain")
    issues, facts = _checks(page, final_url, sitemap, robots)

    brief_name, brief = load_brief(cfg, task.inputs.get("product"))
    visible = " ".join(page.text)[:PAGE_TEXT_BUDGET]
    critique = await llm.complete(
        (f"Product brief for comparison:\n{brief}\n\n" if brief else "")
        + f"Question from the owner: {task.prompt}\n\n"
        f"Page: {final_url}\nTitle: {page.title!r}\nHeadline(s): {page.h1}\n\n"
        f"Visible text:\n{visible}\n\n"
        + ("Judge the page against the brief's first-priority audience and quote "
           "prices only with the plan name the brief gives them.\n\n" if brief else "")
        + "In under 250 words: (1) who this page appears to be for, in one line; "
        "(2) whether the headline states a clear outcome for that reader; "
        "(3) the single biggest weakness in the message; (4) one concrete "
        "rewrite of the headline and one of the meta description.",
        cfg,
        system=AUDIT_SYSTEM,
    )

    report = (
        f"# Marketing audit — {final_url}\n\n"
        f"HTTP {status}. Title: {page.title!r} ({len(page.title)} chars). "
        f"Description: {facts['description']!r} ({facts['description_length']} chars).\n\n"
        "## Mechanical findings\n"
        + ("\n".join(f"- {i}" for i in issues) if issues else "- Nothing flagged.")
        + "\n\n## Message critique\n" + critique
        + "\n\n## Extracted\n```json\n" + json.dumps(facts, indent=2) + "\n```\n"
    )
    directory = Path(cfg["core"]["artifact_dir"]).expanduser() / task.id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "audit.md"
    path.write_text(report)

    summary = (
        f"{final_url}: {len(issues)} mechanical issue(s).\n"
        + "\n".join(f"- {i}" for i in issues[:8])
        + (f"\n- ...and {len(issues) - 8} more in {path.name}" if len(issues) > 8 else "")
        + "\n\nMessage: " + critique
    )
    return ok(
        task,
        summary=summary,
        artifacts=[path.as_uri()],
        data={"fetched": True, "url": final_url, "status": status,
              "issues": issues, "checks": facts, "brief": brief_name},
    )


def _first_url(text: str) -> str | None:
    match = re.search(r"(https?://[^\s)\]]+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b)", text, re.I)
    return match.group(0) if match else None


def _int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default
