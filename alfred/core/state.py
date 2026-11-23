"""Structured state. Desktop only. Alfred is the sole writer.

This is the *exact* half of memory. The fuzzy half — embedded conversation
chunks for "what did we say about torque?" — belongs in a separate store and
must never be consulted for authoritative facts, because retrieval returns
plausible text and plausible is not the same as true.

`decisions.superseded_by` is what makes Alfred feel like he remembers you.
"You chose NEMA 17 over NEMA 23 in March on weight grounds, and that
constraint has not changed" is a row lookup, not a similarity search that
might surface the conversation where you were still undecided.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT,
    status TEXT DEFAULT 'active', created_at REAL, updated_at REAL,
    root TEXT);

CREATE TABLE IF NOT EXISTS project_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT DEFAULT 'open',
    task_ids TEXT,
    created_at REAL,
    closed_at REAL);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    decision TEXT NOT NULL, rationale TEXT, made_at REAL,
    superseded_by INTEGER REFERENCES decisions(id));

CREATE TABLE IF NOT EXISTS open_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    question TEXT NOT NULL, blocking INTEGER DEFAULT 0,
    asked_at REAL, answered_at REAL, answer TEXT);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, project_id TEXT, capability TEXT, prompt TEXT,
    status TEXT DEFAULT 'queued', assigned_to TEXT,
    lease_expires_at REAL, attempt INTEGER DEFAULT 0,
    summary TEXT, error TEXT, created_at REAL, finished_at REAL);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, task_id TEXT,
    uri TEXT NOT NULL, kind TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, kind TEXT,
    body TEXT, created_at REAL, delivered_at REAL);

CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT,
    description TEXT NOT NULL, task_json TEXT NOT NULL,
    status TEXT DEFAULT 'pending',   -- pending | approved | declined | executed | failed
    created_at REAL, decided_at REAL, result TEXT);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL DEFAULT 'owner',   -- who/what the fact is about
    key TEXT NOT NULL,                        -- e.g. 'preferred_name', 'units'
    value TEXT NOT NULL,
    source TEXT,                              -- how he learned it (verbatim ask)
    confidence TEXT DEFAULT 'stated',         -- stated | inferred
    learned_at REAL, updated_at REAL,
    superseded_by INTEGER REFERENCES facts(id),
    UNIQUE(subject, key, superseded_by));

CREATE TABLE IF NOT EXISTS conversation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT, role TEXT NOT NULL, body TEXT NOT NULL, at REAL);

CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY, name TEXT, hostname TEXT, profile TEXT,
    capabilities TEXT, note TEXT, enrolled_at REAL, last_seen REAL);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_notices_undelivered ON notices(delivered_at);
"""


class State:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        project_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(projects)")
        }
        if "root" not in project_columns:
            self.db.execute("ALTER TABLE projects ADD COLUMN root TEXT")
        self.db.commit()

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.db.execute(sql, params)
        self.db.commit()
        return cur

    # ---- projects --------------------------------------------------------

    def create_project(self, name: str, goal: str = "",
                       root: str | None = None) -> str:
        pid = f"proj_{uuid.uuid4().hex[:8]}"
        now = time.time()
        self._write(
            "INSERT INTO projects (id,name,goal,created_at,updated_at,root) "
            "VALUES (?,?,?,?,?,?)",
            (pid, name, goal, now, now, root),
        )
        return pid

    def set_project_root(self, pid: str, root: str | None) -> None:
        self._write("UPDATE projects SET root=? WHERE id=?", (root, pid))

    def project(self, project_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def find_project(self, name_or_id: str) -> dict | None:
        exact_id = self.db.execute(
            "SELECT * FROM projects WHERE id=?", (name_or_id,)).fetchone()
        if exact_id:
            return dict(exact_id)

        active = self.active_projects()
        query = name_or_id.casefold()
        exact_names = [p for p in active if p["name"].casefold() == query]
        if len(exact_names) == 1:
            return exact_names[0]
        if len(exact_names) > 1:
            return None

        matches = [
            p for p in active
            if query in p["name"].casefold()
        ]
        return matches[0] if len(matches) == 1 else None

    def active_projects(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM projects WHERE status='active' ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def touch(self, project_id: str) -> None:
        self._write("UPDATE projects SET updated_at=? WHERE id=?", (time.time(), project_id))

    def log_step(self, pid: str, kind: str, body: str,
                 task_ids: list[str] | None = None) -> int:
        if kind not in {"next", "done", "problem", "note"}:
            raise ValueError(f"unsupported project log kind: {kind}")
        cur = self._write(
            "INSERT INTO project_log "
            "(project_id,kind,body,task_ids,created_at) VALUES (?,?,?,?,?)",
            (pid, kind, body, json.dumps(task_ids or []), time.time()),
        )
        self.touch(pid)
        return int(cur.lastrowid)

    def close_step(self, step_id: int, closed_by_kind: str | None = None) -> None:
        self._write(
            "UPDATE project_log SET status='closed',closed_at=? WHERE id=?",
            (time.time(), step_id),
        )

    def steps(self, pid: str, kind: str, open_only: bool = True,
              limit: int = 20) -> list[dict]:
        where = "project_id=? AND kind=?"
        params: list[Any] = [pid, kind]
        if open_only:
            where += " AND status='open'"
        order = "DESC" if kind == "done" else "ASC"
        params.append(limit)
        rows = self.db.execute(
            f"SELECT * FROM project_log WHERE {where} "
            f"ORDER BY created_at {order}, id {order} LIMIT ?",
            tuple(params),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["task_ids"] = json.loads(item["task_ids"] or "[]")
            result.append(item)
        return result

    def project_files(self, pid: str, limit: int = 20) -> list[str]:
        project = self.project(pid)
        if not project or not project.get("root"):
            return []
        root = Path(project["root"]).expanduser()
        if not root.is_dir():
            return []

        ignored = {".git", "node_modules", "__pycache__", ".venv", "venv"}
        files: list[tuple[float, str]] = []
        pending = [root]
        scanned = 0
        while pending and scanned < 2000:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if scanned >= 2000:
                            break
                        scanned += 1
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if (entry.name not in ignored
                                        and not entry.name.startswith(".")):
                                    pending.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                mtime = entry.stat(follow_symlinks=False).st_mtime
                                relative = Path(entry.path).relative_to(root).as_posix()
                                files.append((mtime, relative))
                        except OSError:
                            continue
            except OSError:
                continue
        files.sort(key=lambda item: (-item[0], item[1]))
        return [path for _, path in files[:limit]]

    # ---- decisions and questions ----------------------------------------

    def record_decision(self, project_id: str, decision: str, rationale: str,
                        supersedes: int | None = None) -> int:
        cur = self._write(
            "INSERT INTO decisions (project_id,decision,rationale,made_at) VALUES (?,?,?,?)",
            (project_id, decision, rationale, time.time()),
        )
        new_id = int(cur.lastrowid)
        if supersedes:
            self._write("UPDATE decisions SET superseded_by=? WHERE id=?", (new_id, supersedes))
        return new_id

    def standing_decisions(self, project_id: str) -> list[dict]:
        """Only decisions still in force. Superseded ones stay for history."""
        rows = self.db.execute(
            "SELECT * FROM decisions WHERE project_id=? AND superseded_by IS NULL "
            "ORDER BY made_at", (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def ask(self, project_id: str, question: str, blocking: bool = False) -> int:
        cur = self._write(
            "INSERT INTO open_questions (project_id,question,blocking,asked_at) VALUES (?,?,?,?)",
            (project_id, question, int(blocking), time.time()),
        )
        return int(cur.lastrowid)

    def open_questions(self, project_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM open_questions WHERE answered_at IS NULL"
        params: tuple = ()
        if project_id:
            sql += " AND project_id=?"
            params = (project_id,)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY blocking DESC, asked_at", params)]

    def answer(self, question_id: int, answer: str) -> None:
        self._write(
            "UPDATE open_questions SET answered_at=?, answer=? WHERE id=?",
            (time.time(), answer, question_id),
        )

    # ---- task ledger -----------------------------------------------------

    def enqueue(self, task: Any) -> None:
        self._write(
            "INSERT OR REPLACE INTO tasks (id,project_id,capability,prompt,status,"
            "attempt,created_at) VALUES (?,?,?,?,'queued',?,?)",
            (task.id, task.project_id, task.capability, task.prompt, task.attempt, time.time()),
        )

    def lease(self, task_id: str, worker_id: str, seconds: float) -> None:
        self._write(
            "UPDATE tasks SET status='running', assigned_to=?, lease_expires_at=? WHERE id=?",
            (worker_id, time.time() + seconds, task_id),
        )

    def finish(self, task_id: str, status: str, summary: str = "", error: str = "") -> None:
        self._write(
            "UPDATE tasks SET status=?, summary=?, error=?, finished_at=? WHERE id=?",
            (status, summary, error, time.time(), task_id),
        )

    def expired_leases(self) -> list[dict]:
        """Tasks whose worker went quiet. A closed laptop lid looks exactly
        like a crash from here, and both want the same response: requeue."""
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status='running' AND lease_expires_at < ?",
            (time.time(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_artifact(self, project_id: str, task_id: str, uri: str, kind: str = "") -> None:
        self._write(
            "INSERT INTO artifacts (project_id,task_id,uri,kind,created_at) VALUES (?,?,?,?,?)",
            (project_id, task_id, uri, kind, time.time()),
        )

    # ---- facts: the exact half of knowing the owner ---------------------

    def learn(self, key: str, value: str, source: str = "",
              subject: str = "owner", confidence: str = "stated") -> int:
        """Record a durable fact. Re-learning a key supersedes the old value
        rather than overwriting it — so "you told me metric in March, then
        imperial in June" is a history, not a lie. The current value is the
        one row per (subject,key) with superseded_by IS NULL."""
        now = time.time()
        prior = self.db.execute(
            "SELECT id FROM facts WHERE subject=? AND key=? AND superseded_by IS NULL",
            (subject, key)).fetchone()
        cur = self._write(
            "INSERT INTO facts (subject,key,value,source,confidence,learned_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (subject, key, value, source, confidence, now, now))
        new_id = int(cur.lastrowid)
        if prior:
            self._write("UPDATE facts SET superseded_by=? WHERE id=?", (new_id, prior["id"]))
        return new_id

    def forget(self, key: str, subject: str = "owner") -> bool:
        """Owner asked him to forget something. Supersede with a tombstone so
        it stops surfacing but the history is not silently rewritten."""
        row = self.db.execute(
            "SELECT id FROM facts WHERE subject=? AND key=? AND superseded_by IS NULL",
            (subject, key)).fetchone()
        if not row:
            return False
        cur = self._write(
            "INSERT INTO facts (subject,key,value,source,confidence,learned_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (subject, key, "(forgotten at owner's request)", "forget", "stated",
             time.time(), time.time()))
        self._write("UPDATE facts SET superseded_by=? WHERE id=?", (int(cur.lastrowid), row["id"]))
        self._write("UPDATE facts SET superseded_by=-1 WHERE id=?", (int(cur.lastrowid),))
        return True

    def known_facts(self, subject: str = "owner") -> list[dict]:
        """Current facts only — superseded and tombstoned rows excluded."""
        return [dict(r) for r in self.db.execute(
            "SELECT key, value, confidence FROM facts "
            "WHERE subject=? AND superseded_by IS NULL AND value NOT LIKE '(forgotten%' "
            "ORDER BY key", (subject,))]

    def recall_fact(self, key: str, subject: str = "owner") -> str | None:
        row = self.db.execute(
            "SELECT value FROM facts WHERE subject=? AND key=? AND superseded_by IS NULL "
            "AND value NOT LIKE '(forgotten%'", (subject, key)).fetchone()
        return row["value"] if row else None

    # ---- persisted conversation -----------------------------------------

    def log_turn(self, role: str, body: str, project_id: str | None = None,
                 at: float | None = None) -> None:
        self._write("INSERT INTO conversation (project_id,role,body,at) VALUES (?,?,?,?)",
                    (project_id, role, body[:4000], at or time.time()))

    def recent_turns(self, project_id: str | None, limit: int = 6) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT role, body FROM conversation WHERE project_id IS ? "
            "ORDER BY id DESC LIMIT ?", (project_id, limit)).fetchall()
        return [(r["role"], r["body"]) for r in reversed(rows)]

    def recent_turns_at(self, project_id: str | None,
                        limit: int = 40) -> list[tuple[str, str, float]]:
        rows = self.db.execute(
            "SELECT role, body, at FROM conversation WHERE project_id IS ? "
            "ORDER BY id DESC LIMIT ?", (project_id, limit)).fetchall()
        return [(r["role"], r["body"], r["at"]) for r in reversed(rows)]

    # ---- pending actions (owner approval gate for os.apply) --------------

    def park_action(self, project_id: str | None, description: str, task_json: str) -> int:
        cur = self._write(
            "INSERT INTO pending_actions (project_id,description,task_json,created_at) "
            "VALUES (?,?,?,?)",
            (project_id, description, task_json, time.time()),
        )
        return int(cur.lastrowid)

    def pending_actions(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT id,project_id,description,status,created_at FROM pending_actions "
            "WHERE status='pending' ORDER BY created_at")]

    def take_action(self, action_id: int) -> dict | None:
        """Atomically claim a pending action for execution. Returns the row
        or None if it was not pending — a double-click on Approve must not
        run an apt install twice."""
        cur = self._write(
            "UPDATE pending_actions SET status='approved', decided_at=? "
            "WHERE id=? AND status='pending'", (time.time(), action_id))
        if cur.rowcount == 0:
            return None
        row = self.db.execute(
            "SELECT * FROM pending_actions WHERE id=?", (action_id,)).fetchone()
        return dict(row) if row else None

    def decline_action(self, action_id: int) -> bool:
        cur = self._write(
            "UPDATE pending_actions SET status='declined', decided_at=? "
            "WHERE id=? AND status='pending'", (time.time(), action_id))
        return cur.rowcount > 0

    def settle_action(self, action_id: int, ok: bool, result: str) -> None:
        self._write(
            "UPDATE pending_actions SET status=?, result=? WHERE id=?",
            ("executed" if ok else "failed", result[:2000], action_id))

    # ---- nodes -----------------------------------------------------------

    def record_node(self, assignment, profile) -> None:
        self._write(
            "INSERT OR REPLACE INTO nodes (node_id,name,hostname,profile,"
            "capabilities,note,enrolled_at,last_seen) VALUES (?,?,?,?,?,?,?,?)",
            (assignment.node_id, assignment.name, profile.hostname,
             profile.to_json(), json.dumps(assignment.capabilities),
             assignment.note, time.time(), time.time()),
        )

    def all_nodes(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT node_id,name,capabilities,last_seen FROM nodes").fetchall()]

    def in_flight_tasks(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT capability,assigned_to,status FROM tasks "
            "WHERE status IN ('queued','claimed','running') "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        return [dict(r) for r in rows]

    def known_node(self, node_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        return dict(row) if row else None

    def nodes(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM nodes ORDER BY enrolled_at")]

    def coverage(self) -> dict[str, list[str]]:
        """Which machine covers which capabilities, for proposing a sensible
        role to a newcomer rather than duplicating what is already handled."""
        return {r["name"]: json.loads(r["capabilities"] or "[]") for r in self.nodes()}

    # ---- settings (owner switches that must survive a restart) ---------

    def setting(self, key: str, default: str = "") -> str:
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, time.time()),
        )

    # ---- notices (supervisor -> conversation) ---------------------------

    def notice(self, kind: str, body: str, project_id: str | None = None) -> None:
        """The supervisor loop never speaks to you. It leaves a note here and
        the conversation loop picks it up on your next turn."""
        self._write(
            "INSERT INTO notices (project_id,kind,body,created_at) VALUES (?,?,?,?)",
            (project_id, kind, body, time.time()),
        )

    def undelivered(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM notices WHERE delivered_at IS NULL ORDER BY created_at")]

    def recent_notices(self, limit: int = 8) -> list[dict]:
        """Delivered or not, the last few, oldest first: a panel that only
        showed undelivered ones would go blank the moment Alfred speaks."""
        rows = self.db.execute(
            "SELECT * FROM notices ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def mark_delivered(self, ids: list[int]) -> None:
        if ids:
            self.db.executemany(
                "UPDATE notices SET delivered_at=? WHERE id=?",
                [(time.time(), i) for i in ids],
            )
            self.db.commit()

    # ---- context assembly -----------------------------------------------

    def briefing(self, project_id: str) -> str:
        """The compact project view injected into Alfred's prompt each turn.

        Structured, not retrieved. Small on purpose — his context window is
        the scarcest resource in the system.
        """
        proj = self.project(project_id)
        if not proj:
            return ""
        parts = [f"Project: {proj['name']} — {proj['goal']}"]
        decisions = self.standing_decisions(project_id)
        if decisions:
            parts.append("Standing decisions:\n" + "\n".join(
                f"  - {d['decision']} ({d['rationale']})" for d in decisions))
        questions = self.open_questions(project_id)
        if questions:
            parts.append("Open questions:\n" + "\n".join(
                f"  - {q['question']}" + (" [blocking]" if q["blocking"] else "")
                for q in questions))
        rows = self.db.execute(
            "SELECT capability, status, summary, error FROM tasks WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT 8", (project_id,)).fetchall()
        if rows:
            parts.append(
                "Recent work (already reported to the owner at the time; context "
                "only, do not bring it up unless asked):\n" + "\n".join(
                    f"  - [{r['status']}] {r['capability']}: "
                    f"{(r['summary'] or r['error'] or '')[:120]}"
                    for r in rows))
        next_steps = self.steps(project_id, "next", limit=5)
        if next_steps:
            parts.append("Open next steps:\n" + "\n".join(
                f"  {index}. {step['body']}"
                for index, step in enumerate(next_steps, 1)))
        problems = self.steps(project_id, "problem", limit=5)
        if problems:
            parts.append("Open problems:\n" + "\n".join(
                f"  - {problem['body']}" for problem in problems))
        return "\n\n".join(parts)

    def resume_brief(self, pid: str) -> str:
        project = self.project(pid)
        if not project:
            return ""

        project_section = f"Project: {project['name']}"
        if project.get("goal"):
            project_section += f"\nGoal: {project['goal']}"
        if len(project_section) > 300:
            project_section = project_section[:297].rstrip() + "..."
        parts = [project_section]

        def add_section(title: str, lines: list[str], budget: int) -> None:
            if not lines:
                return
            section = f"{title}:\n"
            for line in lines:
                rendered = f"  - {line}\n"
                available = budget - len(section)
                if available <= 0:
                    break
                if len(rendered) > available:
                    section += (
                        rendered[:available - 3].rstrip() + "..."
                        if available > 3 else "." * available
                    )
                    break
                section += rendered
            parts.append(section.rstrip())

        if project.get("root"):
            root = f"Root: {project['root']}"
            parts.append(root if len(root) <= 250 else root[:247].rstrip() + "...")
        files = self.project_files(pid)
        add_section("Relevant files", files, 350)
        decisions = self.standing_decisions(pid)
        add_section("Standing decisions", [
            f"{item['decision']} ({item['rationale']})" for item in decisions
        ], 400)
        done = self.steps(pid, "done", open_only=False, limit=5)
        add_section("Completed work", [item["body"] for item in done], 450)
        problems = self.steps(pid, "problem", limit=20)
        add_section("Unresolved problems", [item["body"] for item in problems], 300)
        next_steps = self.steps(pid, "next", limit=20)
        add_section("Next steps", [
            f"{index}. {item['body']}"
            for index, item in enumerate(next_steps, 1)
        ], 350)
        questions = self.open_questions(pid)
        add_section("Open questions", [
            f"{item['question']}" + (
                " [blocking]" if item["blocking"] else "")
            for item in questions
        ], 250)
        rows = self.db.execute(
            "SELECT capability, status, summary, error FROM tasks WHERE project_id=? "
            "ORDER BY created_at DESC LIMIT 8", (pid,)).fetchall()
        add_section("Recent task outcomes", [
            f"[{row['status']}] {row['capability']}: "
            f"{(row['summary'] or row['error'] or '')[:120]}"
            for row in rows
        ], 300)
        brief = "\n\n".join(parts)
        return brief if len(brief) <= 3000 else brief[:2997].rstrip() + "..."

    def export(self, project_id: str) -> str:
        return json.dumps({
            "project": self.project(project_id),
            "decisions": self.standing_decisions(project_id),
            "open_questions": self.open_questions(project_id),
        }, indent=2)
