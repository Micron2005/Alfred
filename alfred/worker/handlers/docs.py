"""Documentation capabilities. Light enough for the Chromebook.

Deliberately the smallest handler module. The Chromebook is the least
reliable node in the house — it is off whenever the lid is closed — so it
gets work that nothing else is waiting on.
"""

from __future__ import annotations

from pathlib import Path

from alfred import llm
from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import handler, ok


@handler("docs.write")
async def docs_write(task: Task, cfg: dict) -> TaskResult:
    """Turn accumulated task results into a document.

    Written in neutral technical register, never in Alfred's voice. A worker
    that starts writing "I've prepared this for you, sir" gives you a second
    Alfred, and two Alfreds eventually disagree about what was decided.
    """
    sections = task.inputs.get("sections", [])
    body = "\n\n".join(
        f"## {s.get('heading', 'Section')}\n{s.get('body', '')}" for s in sections
    )

    text = await llm.complete(
        f"Write technical documentation for: {task.prompt}\n\n"
        f"Material to organise:\n{body}\n\n"
        "Markdown. Neutral technical register, third person, no addressing a "
        "reader directly. Do not invent facts absent from the material.",
        cfg,
    )

    directory = Path(cfg["core"]["artifact_dir"]) / task.id
    directory.mkdir(parents=True, exist_ok=True)
    # The output is Markdown whatever the planner called it; a .docx that is
    # really Markdown opens as garbage.
    path = (directory / str(task.inputs.get("filename") or "document")).with_suffix(".md")
    path.write_text(text)

    return ok(
        task,
        summary=f"Wrote {path.name}, {len(text.split())} words, {len(sections)} sections.",
        artifacts=[path.as_uri()],
    )
