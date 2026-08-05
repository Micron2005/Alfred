"""Engineering capabilities. Configured onto the Zenbook, available anywhere.

Note the shape of every handler: read a lot, do the slow thing, return a
little. `code.test` may generate 4000 lines of pytest output; what goes back
to Alfred is "17 passed, 2 failed" plus the two failing test names. The full
log becomes an artifact he can ask for if he needs it.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from alfred import llm
from alfred.contracts import Task, TaskResult
from alfred.worker.handlers import fail, handler, ok


def _artifact_path(cfg: dict, task: Task, name: str) -> Path:
    directory = Path(cfg["core"]["artifact_dir"]) / task.id
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


@handler("code.write")
async def code_write(task: Task, cfg: dict) -> TaskResult:
    context = "\n".join(f"- {k}: {v}" for k, v in task.inputs.items())
    prompt = (
        f"Write {task.inputs.get('language', 'Python')} code for this task.\n\n"
        f"Task: {task.prompt}\n\n"
        f"Context:\n{context}\n\n"
        "Return only the code, no prose, no fences."
    )
    code = await llm.complete(prompt, cfg, model=cfg["worker"].get("code_model"))
    code = re.sub(r"^```[a-z]*\n|```$", "", code.strip(), flags=re.MULTILINE)

    filename = task.inputs.get("filename", "generated.py")
    path = _artifact_path(cfg, task, filename)
    path.write_text(code)

    lines = code.count("\n") + 1
    return ok(
        task,
        summary=f"Wrote {filename} ({lines} lines). Untested.",
        artifacts=[path.as_uri()],
        data={"lines": lines, "language": task.inputs.get("language", "python")},
    )


@handler("code.test")
async def code_test(task: Task, cfg: dict) -> TaskResult:
    """Mechanical verification. No model involved, so no opinion involved.

    This is the handler that makes Alfred's "verify results" step real rather
    than one language model vouching for another.
    """
    workdir = task.inputs.get("workdir")
    if not workdir or not Path(workdir).is_dir():
        return fail(task, f"workdir not found: {workdir!r}")

    command = task.inputs.get("command", "python -m pytest -q")
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    raw, _ = await proc.communicate()
    output = raw.decode(errors="replace")

    log_path = _artifact_path(cfg, task, "test-output.log")
    log_path.write_text(output)

    tail = "\n".join(output.strip().splitlines()[-3:])
    failures = re.findall(r"^(FAILED|ERROR) (\S+)", output, flags=re.MULTILINE)
    status = "ok" if proc.returncode == 0 else "error"

    result = TaskResult(
        task_id=task.id,
        worker_id="",
        status=status,
        summary=(
            f"`{command}` exited {proc.returncode}. {tail}"
            + (f" Failing: {', '.join(n for _, n in failures[:5])}" if failures else "")
        ),
        artifacts=[log_path.as_uri()],
        data={"exit_code": proc.returncode, "failures": [n for _, n in failures]},
    )
    if status == "error":
        result.error = f"tests failed (exit {proc.returncode})"
    return result


@handler("calc.engineering")
async def calc_engineering(task: Task, cfg: dict) -> TaskResult:
    """Numeric work, executed rather than predicted.

    The model writes a script; Python runs it. A language model asked for a
    torque figure will produce a confident number with no arithmetic behind
    it, which is exactly the failure mode you cannot afford in a design.
    """
    prompt = (
        f"Write a Python script that computes the answer to this and prints "
        f"clearly labelled results with units.\n\nTask: {task.prompt}\n\n"
        f"Given: {task.inputs}\n\n"
        "Use only the standard library and `math`. Show intermediate values. "
        "Return only the script."
    )
    script = await llm.complete(prompt, cfg)
    script = re.sub(r"^```[a-z]*\n|```$", "", script.strip(), flags=re.MULTILINE)
    path = _artifact_path(cfg, task, "calc.py")
    path.write_text(script)

    proc = await asyncio.create_subprocess_exec(
        "python3", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    raw, _ = await proc.communicate()
    output = raw.decode(errors="replace").strip()

    if proc.returncode != 0:
        return fail(task, f"calculation script failed:\n{output[-500:]}")
    return ok(
        task,
        summary=output[:1500],
        artifacts=[path.as_uri()],
        data={"exit_code": 0},
    )
