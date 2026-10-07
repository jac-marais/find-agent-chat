#!/usr/bin/env python3
"""Search coding-agent chat transcripts through a local SQLite FTS5 index.

Supported harnesses:
  claude  Claude Code   ~/.claude/projects/<encoded-cwd>/<session>.jsonl
  codex   Codex         ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl
  cursor  Cursor        ~/.cursor/projects/<encoded-cwd>/agent-transcripts/<id>/<id>.jsonl

The index lives at ~/.cache/find-agent-chat/index.sqlite (override with
FIND_AGENT_CHAT_INDEX). Full-text searches only use the existing index;
`name` refreshes selected sources automatically unless `--no-refresh`
is supplied.

Usage:
  python3 search_chats.py index                    # add/refresh changed transcripts
  python3 search_chats.py index --rebuild          # start from scratch
  python3 search_chats.py "error enum"             # every word must appear (stemmed)
  python3 search_chats.py '"helm deploy"' webshop # quoted phrase plus a word
  python3 search_chats.py search index --days 7    # `search` only needed when the
                                                   # first word is index/search/name
  python3 search_chats.py name "Weekly team sync status" --source claude --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
CODEX_INDEX = Path.home() / ".codex" / "session_index.jsonl"
CURSOR_PROJECTS_ROOT = Path.home() / ".cursor" / "projects"
CURSOR_DB = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Cursor"
    / "User"
    / "globalStorage"
    / "state.vscdb"
)
INDEX_PATH = Path(
    os.environ.get("FIND_AGENT_CHAT_INDEX")
    or Path.home() / ".cache" / "find-agent-chat" / "index.sqlite"
)

ALL_SOURCES = ("claude", "codex", "cursor")
SCHEMA_VERSION = "2"
MAX_TEXT_CHARS = 20_000
STALE_AFTER_SECONDS = 86_400
META_ROLE = "meta"
META_WEIGHT = 2.0
TOOL_ROLES = frozenset({"tool_use", "tool_result", "function_call", "custom_tool_call",
                        "function_call_output", "custom_tool_call_output"})
TOOL_WEIGHT = 0.4
CODEX_INJECTED_PREFIXES = ("<environment_context>", "<user_instructions>", "<permissions")

SCHEMA = """
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    agent_name TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    first_ts REAL NOT NULL DEFAULT 0,
    last_ts REAL NOT NULL DEFAULT 0,
    total_lines INTEGER NOT NULL DEFAULT 0,
    subagent INTEGER NOT NULL DEFAULT 0,
    first_user_text TEXT NOT NULL DEFAULT '',
    file_size INTEGER NOT NULL,
    file_mtime REAL NOT NULL
);
CREATE INDEX sessions_last_ts ON sessions(last_ts);
CREATE VIRTUAL TABLE messages USING fts5(
    text,
    session_ref UNINDEXED,
    role UNINDEXED,
    line_no UNINDEXED,
    tokenize = 'porter unicode61'
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def clip(text: str, limit: int = 160) -> str:
    clean = " ".join(text.split())
    return clean[: limit - 3] + "..." if len(clean) > limit else clean


def parse_iso_ts(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError):
        return 0.0


def record_ts(record: dict[str, Any]) -> float:
    ts = record.get("timestamp")
    if isinstance(ts, (int, float)):
        return ts / 1000 if ts > 1e12 else float(ts)
    if isinstance(ts, str):
        return parse_iso_ts(ts)
    return 0.0


def format_ts(ts: float) -> str:
    if ts == 0.0:
        return "unknown"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OSError, ValueError):
        return "unknown"


def format_age(ts: float) -> str:
    if ts == 0.0:
        return ""
    delta = time.time() - ts
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


@dataclass
class ParsedSession:
    source: str
    path: Path
    session_id: str = ""
    project: str = ""
    title: str = ""
    agent_name: str = ""
    branch: str = ""
    cwd: str = ""
    first_ts: float = 0.0
    last_ts: float = 0.0
    total_lines: int = 0
    subagent: bool = False
    first_user_text: str = ""
    messages: list[tuple[str, int, str]] = field(default_factory=list)
    _seen: set[int] = field(default_factory=set, repr=False)

    def note_ts(self, ts: float) -> None:
        if ts <= 0:
            return
        if self.first_ts == 0.0 or ts < self.first_ts:
            self.first_ts = ts
        if ts > self.last_ts:
            self.last_ts = ts

    def add(self, role: str, line_no: int, text: str) -> None:
        text = text.strip()
        if not text:
            return
        # Codex mirrors each message as response_item and event_msg; index one copy.
        key = hash(text)
        if key in self._seen:
            return
        self._seen.add(key)
        self.messages.append((role, line_no, text[:MAX_TEXT_CHARS]))

    def meta_text(self) -> str:
        parts = [self.title, self.agent_name, self.session_id, self.project,
                 self.branch, self.path.stem]
        return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Claude Code (Cursor message records share the shape)
# ---------------------------------------------------------------------------

def extract_text_claude(record: dict[str, Any]) -> tuple[str, str, str]:
    """Return (conversational text, tool-call input text, tool-result text)."""
    spoken: list[str] = []
    tool_input: list[str] = []
    msg = record.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            spoken.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    spoken.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        t = block.get("text")
                        if isinstance(t, str):
                            spoken.append(t)
                    elif block.get("type") == "tool_use":
                        inp = block.get("input")
                        if isinstance(inp, dict):
                            for val in inp.values():
                                if isinstance(val, str) and len(val) < 2000:
                                    tool_input.append(val)
    tool_result = ""
    tr = record.get("toolUseResult")
    if isinstance(tr, dict) and isinstance(tr.get("content"), str):
        tool_result = tr["content"]
    return "\n".join(spoken), "\n".join(tool_input), tool_result


def claude_project_name(path: Path) -> str:
    segments = [s for s in path.parent.name.split("-") if s]
    return segments[-1] if len(segments) >= 3 else path.parent.name


def iter_json_lines(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line_no, raw in enumerate(f, start=1):
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(record, dict):
                yield line_no, record


def parse_claude(path: Path) -> ParsedSession | None:
    sess = ParsedSession(source="claude", path=path, project=claude_project_name(path))
    try:
        for line_no, record in iter_json_lines(path):
            sess.total_lines = line_no
            sess.note_ts(record_ts(record))
            sid = record.get("sessionId")
            if isinstance(sid, str) and not sess.session_id:
                sess.session_id = sid
            cwd = record.get("cwd")
            if isinstance(cwd, str) and not sess.cwd:
                sess.cwd = cwd
            branch = record.get("gitBranch")
            if isinstance(branch, str) and not sess.branch:
                sess.branch = branch
            rtype = record.get("type")
            if rtype == "custom-title" and isinstance(record.get("customTitle"), str):
                sess.title = record["customTitle"]
            if rtype == "agent-name" and isinstance(record.get("agentName"), str):
                sess.agent_name = record["agentName"]
            # isMeta records are harness-injected caveats and command echoes, not conversation.
            if record.get("isMeta"):
                continue
            spoken, tool_input, tool_result = extract_text_claude(record)
            if rtype == "user" and not sess.first_user_text and spoken.strip() \
                    and not spoken.startswith("<command-name>"):
                sess.first_user_text = clip(spoken, 200)
            sess.add(str(rtype or "?"), line_no, spoken)
            sess.add("tool_use", line_no, tool_input)
            sess.add("tool_result", line_no, tool_result)
    except OSError:
        return None
    return sess


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def load_codex_titles() -> dict[str, str]:
    titles: dict[str, str] = {}
    try:
        with CODEX_INDEX.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(r, dict) and isinstance(r.get("id"), str):
                    name = r.get("thread_name")
                    if isinstance(name, str) and name:
                        titles[r["id"]] = name
    except OSError:
        pass
    return titles


def extract_text_codex(record: dict[str, Any]) -> str:
    parts: list[str] = []
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ""
    ptype = payload.get("type")
    if ptype in ("message", "agent_message", "user_message", "agent_reasoning", "reasoning"):
        m = payload.get("message")
        if isinstance(m, str):
            parts.append(m)
        for key in ("content", "summary"):
            blocks = payload.get(key)
            if isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, dict):
                        t = block.get("text")
                        if isinstance(t, str):
                            parts.append(t)
    elif ptype in ("function_call", "custom_tool_call"):
        for key in ("name", "arguments", "input"):
            v = payload.get(key)
            if isinstance(v, str) and len(v) < 2000:
                parts.append(v)
    elif ptype in ("function_call_output", "custom_tool_call_output"):
        v = payload.get("output")
        if isinstance(v, str) and len(v) < 2000:
            parts.append(v)
        elif isinstance(v, dict):
            c = v.get("content")
            if isinstance(c, str) and len(c) < 2000:
                parts.append(c)
    return "\n".join(parts)


def parse_codex(path: Path, titles: dict[str, str]) -> ParsedSession | None:
    sess = ParsedSession(source="codex", path=path)
    try:
        for line_no, record in iter_json_lines(path):
            sess.total_lines = line_no
            sess.note_ts(record_ts(record))
            rtype = record.get("type")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if rtype == "session_meta":
                git = payload.get("git")
                sess.session_id = str(payload.get("id") or payload.get("session_id") or "")
                sess.cwd = str(payload.get("cwd") or "")
                sess.branch = str(git.get("branch") or "") if isinstance(git, dict) else ""
                sess.agent_name = str(payload.get("agent_nickname") or "")
                sess.subagent = (payload.get("thread_source") or "user") != "user"
                continue
            # session_meta and turn_context embed the system prompt and environment
            # config; indexing them makes every session match generic words.
            if rtype == "turn_context":
                continue
            text = extract_text_codex(record)
            if not text.strip():
                continue
            ptype = payload.get("type")
            role = payload.get("role") if ptype == "message" else ptype
            # Developer/system messages and the injected <environment_context> /
            # <user_instructions> user turns carry AGENTS.md and config, not conversation.
            if role in ("developer", "system") or text.lstrip().startswith(CODEX_INJECTED_PREFIXES):
                continue
            if ptype == "user_message" and not sess.first_user_text:
                sess.first_user_text = clip(text, 200)
            sess.add(str(role or rtype or "?"), line_no, text)
    except OSError:
        return None
    if not sess.session_id:
        return None
    sess.title = titles.get(sess.session_id, "")
    sess.project = Path(sess.cwd).name if sess.cwd else ""
    return sess


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

def load_cursor_headers() -> dict[str, dict[str, Any]]:
    info: dict[str, dict[str, Any]] = {}
    if not CURSOR_DB.exists():
        return info
    try:
        conn = sqlite3.connect(f"file:{CURSOR_DB}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT composerId, createdAt, lastUpdatedAt, isSubagent, value FROM composerHeaders"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return info
    for cid, created, updated, is_sub, raw in rows:
        try:
            value = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            value = {}
        ws = value.get("workspaceIdentifier")
        uri = ws.get("uri") if isinstance(ws, dict) else None
        fs_path = uri.get("fsPath") if isinstance(uri, dict) else None
        info[str(cid)] = {
            "title": value.get("name") if isinstance(value.get("name"), str) else "",
            "cwd": fs_path or "",
            "created": (created or 0) / 1000,
            "updated": (updated or 0) / 1000,
            "subagent": bool(is_sub),
        }
    return info


def parse_cursor(path: Path, headers: dict[str, dict[str, Any]]) -> ParsedSession | None:
    header = headers.get(path.stem, {})
    sess = ParsedSession(
        source="cursor", path=path, session_id=path.stem,
        title=header.get("title", ""), cwd=header.get("cwd", ""),
        subagent=bool(header.get("subagent")),
        first_ts=header.get("created", 0.0) or 0.0,
        last_ts=header.get("updated", 0.0) or 0.0,
    )
    # .../projects/<encoded-cwd>/agent-transcripts/<id>/<id>.jsonl
    segments = [s for s in path.parents[2].name.split("-") if s]
    sess.project = Path(sess.cwd).name if sess.cwd else (segments[-1] if segments else "")
    try:
        for line_no, record in iter_json_lines(path):
            sess.total_lines = line_no
            role = record.get("role")
            spoken, tool_input, tool_result = extract_text_claude(record)
            if role == "user" and not sess.first_user_text and spoken.strip():
                sess.first_user_text = clip(spoken, 200)
            sess.add(str(role or "?"), line_no, spoken)
            sess.add("tool_use", line_no, tool_input)
            sess.add("tool_result", line_no, tool_result)
        if sess.last_ts == 0.0:
            sess.last_ts = path.stat().st_mtime
    except OSError:
        return None
    return sess


# ---------------------------------------------------------------------------
# Index storage
# ---------------------------------------------------------------------------

def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open an existing index without creating the database or changing its journal."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def index_stats(conn: sqlite3.Connection) -> tuple[int, int, float]:
    sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    messages = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    row = conn.execute("SELECT value FROM meta WHERE key='last_index_at'").fetchone()
    return sessions, messages, float(row[0]) if row else 0.0


def delete_session(conn: sqlite3.Connection, session_pk: int) -> None:
    conn.execute("DELETE FROM messages WHERE session_ref = ?", (session_pk,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_pk,))


def insert_session(conn: sqlite3.Connection, sess: ParsedSession, st: os.stat_result) -> None:
    cur = conn.execute(
        """INSERT INTO sessions (source, path, session_id, project, title, agent_name, branch,
           cwd, first_ts, last_ts, total_lines, subagent, first_user_text, file_size, file_mtime)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sess.source, str(sess.path), sess.session_id, sess.project, sess.title,
         sess.agent_name, sess.branch, sess.cwd, sess.first_ts, sess.last_ts,
         sess.total_lines, int(sess.subagent), sess.first_user_text, st.st_size, st.st_mtime),
    )
    pk = cur.lastrowid
    rows = [(text, pk, role, line_no) for role, line_no, text in sess.messages]
    meta = sess.meta_text()
    if meta:
        rows.append((meta, pk, META_ROLE, 0))
    conn.executemany(
        "INSERT INTO messages (text, session_ref, role, line_no) VALUES (?,?,?,?)", rows
    )


def refresh_meta_row(conn: sqlite3.Connection, session_pk: int) -> None:
    row = conn.execute(
        """SELECT title, agent_name, session_id, project, branch, path
           FROM sessions WHERE id = ?""", (session_pk,)
    ).fetchone()
    if row is None:
        return
    conn.execute("DELETE FROM messages WHERE session_ref = ? AND role = ?",
                 (session_pk, META_ROLE))
    meta = " ".join(value for value in (row[0], row[1], row[2], row[3], row[4],
                                         Path(row[5]).stem) if value)
    if meta:
        conn.execute(
            "INSERT INTO messages (text, session_ref, role, line_no) VALUES (?,?,?,?)",
            (meta, session_pk, META_ROLE, 0),
        )


@dataclass
class SourceStats:
    files: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped: int = 0
    seconds: float = 0.0


def index_source(
    conn: sqlite3.Connection,
    source: str,
    files: Iterable[Path],
    parse: Callable[[Path], ParsedSession | None],
) -> SourceStats:
    stats = SourceStats()
    started = time.time()
    existing = {
        path: (pk, size, mtime)
        for pk, path, size, mtime in conn.execute(
            "SELECT id, path, file_size, file_mtime FROM sessions WHERE source = ?", (source,)
        )
    }
    seen: set[str] = set()
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        stats.files += 1
        key = str(f)
        seen.add(key)
        prev = existing.get(key)
        if prev and prev[1] == st.st_size and prev[2] == st.st_mtime:
            stats.unchanged += 1
            continue
        parsed = parse(f)
        if parsed is None:
            stats.skipped += 1
            continue
        if prev:
            delete_session(conn, prev[0])
            stats.updated += 1
        else:
            stats.added += 1
        insert_session(conn, parsed, st)
    for key, (pk, _, _) in existing.items():
        if key not in seen:
            delete_session(conn, pk)
            stats.removed += 1
    stats.seconds = time.time() - started
    return stats


def refresh_source_metadata(conn: sqlite3.Connection, source: str) -> None:
    """Refresh names and metadata whose source can change without transcript edits."""
    if source == "codex":
        titles = load_codex_titles()
        rows = conn.execute(
            "SELECT id, session_id, title FROM sessions WHERE source = 'codex'"
        ).fetchall()
        updates = [
            (titles[session_id], pk) for pk, session_id, title in rows
            if session_id in titles and titles[session_id] != title
        ]
        conn.executemany("UPDATE sessions SET title = ? WHERE id = ?", updates)
        for _, pk in updates:
            refresh_meta_row(conn, pk)
    elif source == "cursor":
        headers = load_cursor_headers()
        rows = conn.execute(
            """SELECT id, session_id, title, cwd, project, first_ts, last_ts, subagent
               FROM sessions WHERE source = 'cursor'"""
        ).fetchall()
        updates = []
        for (pk, session_id, old_title, old_cwd, old_project,
             old_first_ts, old_last_ts, old_subagent) in rows:
            header = headers.get(session_id)
            if header is None:
                continue
            cwd = header.get("cwd") or old_cwd
            project = Path(cwd).name if cwd else old_project
            first_ts = header.get("created") or old_first_ts
            last_ts = header.get("updated") or old_last_ts
            update = (
                header.get("title") or old_title, cwd, project, first_ts, last_ts,
                int(header.get("subagent", False)), pk,
            )
            if update[:-1] != (old_title, old_cwd, old_project, old_first_ts,
                               old_last_ts, old_subagent):
                updates.append(update)
        conn.executemany(
            """UPDATE sessions SET title = ?, cwd = ?, project = ?, first_ts = ?,
               last_ts = ?, subagent = ? WHERE id = ?""",
            updates,
        )
        for _, _, _, _, _, _, pk in updates:
            refresh_meta_row(conn, pk)


def iter_claude_files() -> Iterator[Path]:
    if not CLAUDE_PROJECTS_ROOT.exists():
        return
    for project_dir in sorted(CLAUDE_PROJECTS_ROOT.iterdir()):
        if not project_dir.is_dir():
            continue
        for f in sorted(project_dir.iterdir()):
            if f.suffix == ".jsonl" and f.is_file():
                yield f


def iter_codex_files() -> Iterator[Path]:
    if CODEX_SESSIONS_ROOT.exists():
        yield from sorted(CODEX_SESSIONS_ROOT.glob("*/*/*/*.jsonl"))


def iter_cursor_files() -> Iterator[Path]:
    if CURSOR_PROJECTS_ROOT.exists():
        yield from sorted(CURSOR_PROJECTS_ROOT.glob("*/agent-transcripts/*/*.jsonl"))


def source_jobs(sources: set[str]) -> list[tuple[str, Iterable[Path], Callable[[Path], ParsedSession | None]]]:
    jobs: list[tuple[str, Iterable[Path], Callable[[Path], ParsedSession | None]]] = []
    if "claude" in sources:
        jobs.append(("claude", iter_claude_files(), parse_claude))
    if "codex" in sources:
        titles = load_codex_titles()
        jobs.append(("codex", iter_codex_files(), lambda p: parse_codex(p, titles)))
    if "cursor" in sources:
        headers = load_cursor_headers()
        jobs.append(("cursor", iter_cursor_files(), lambda p: parse_cursor(p, headers)))
    return jobs


def run_index(
    sources: set[str], rebuild: bool, index_path: Path, out: Any = None
) -> int:
    out = sys.stdout if out is None else out
    started = time.time()
    target = index_path
    if rebuild or not index_path.exists():
        # Build beside the live index and swap it in, so a concurrent search
        # never sees a half-written file.
        target = index_path.with_name(index_path.name + ".building")
        for stale in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
            stale.unlink(missing_ok=True)
        conn = connect(target)
        create_schema(conn)
    else:
        conn = connect(index_path)
        if schema_version(conn) != SCHEMA_VERSION:
            conn.close()
            print("Index schema is out of date; rebuilding.", file=out)
            return run_index(sources, True, index_path, out=out)

    conn.execute("BEGIN")
    for source, files, parse in source_jobs(sources):
        stats = index_source(conn, source, files, parse)
        refresh_source_metadata(conn, source)
        print(f"{source:7s} {stats.files:5d} files: +{stats.added} added, "
              f"~{stats.updated} updated, -{stats.removed} removed, "
              f"{stats.unchanged} unchanged, {stats.skipped} unreadable ({stats.seconds:.1f}s)",
              file=out)
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('last_index_at', ?)", (str(time.time()),)
    )
    conn.execute("COMMIT")
    if target != index_path:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    sessions, messages, _ = index_stats(conn)
    conn.close()

    if target != index_path:
        for suffix in ("-wal", "-shm"):
            Path(str(index_path) + suffix).unlink(missing_ok=True)
            Path(str(target) + suffix).unlink(missing_ok=True)
        os.replace(target, index_path)

    size_mb = index_path.stat().st_size / 1e6
    print(f"Index: {sessions:,} sessions, {messages:,} messages, {size_mb:.0f} MB "
          f"at {index_path} (total {time.time() - started:.1f}s)", file=out)
    return 0


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

@dataclass
class QueryTerm:
    text: str
    phrase: bool


TERM_RE = re.compile(r'"([^"]+)"|(\S+)')
HAS_TOKEN_RE = re.compile(r"\w")


def parse_query(words: list[str]) -> list[QueryTerm]:
    terms: list[QueryTerm] = []
    for phrase, word in TERM_RE.findall(" ".join(words)):
        text = phrase or word
        if HAS_TOKEN_RE.search(text):
            terms.append(QueryTerm(text=text, phrase=bool(phrase)))
    return terms


def term_expression(term: QueryTerm, is_last: bool) -> str:
    quoted = '"' + term.text.replace('"', '""') + '"'
    if not term.phrase and is_last and len(term.text) >= 3:
        quoted += "*"
    return quoted


@dataclass
class Candidate:
    role: str
    line_no: int
    score: float
    snippet: str


@dataclass
class SessionHit:
    pk: int
    source: str
    path: Path
    session_id: str
    project: str
    title: str
    agent_name: str
    branch: str
    cwd: str
    first_ts: float
    last_ts: float
    total_lines: int
    first_user_text: str
    score: float = 0.0
    match_count: int = 0
    snippets: list[str] = field(default_factory=list)


@dataclass
class NameHit:
    pk: int
    source: str
    path: Path
    session_id: str
    project: str
    title: str
    agent_name: str
    branch: str
    cwd: str
    first_ts: float
    last_ts: float
    total_lines: int
    first_user_text: str
    match_type: str


NAME_MATCH_RANK = {
    "exact_title": 0,
    "exact_agent_name": 1,
    "partial_title": 2,
    "partial_agent_name": 3,
}


def normalize_name(value: str) -> str:
    """Normalize human-entered names while retaining meaningful punctuation."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def name_match_type(query: str, title: str, agent_name: str) -> str | None:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return None
    normalized_title = normalize_name(title)
    normalized_agent_name = normalize_name(agent_name)
    if normalized_query == normalized_title and normalized_title:
        return "exact_title"
    if normalized_query == normalized_agent_name and normalized_agent_name:
        return "exact_agent_name"
    if normalized_query in normalized_title and normalized_title:
        return "partial_title"
    if normalized_query in normalized_agent_name and normalized_agent_name:
        return "partial_agent_name"
    return None


def find_by_name(
    conn: sqlite3.Connection,
    query: str,
    *,
    sources: set[str] | None = None,
    include_subagents: bool = False,
    days: int | None = None,
    project: str | None = None,
    limit: int | None = None,
) -> list[NameHit]:
    """Find sessions by title or agent name without searching message bodies."""
    normalized_query = normalize_name(query)
    if not normalized_query:
        return []
    sources = set(ALL_SOURCES) if sources is None else sources
    clauses = [f"source IN ({','.join('?' * len(sources))})"]
    params: list[Any] = sorted(sources)
    if not include_subagents:
        clauses.append("subagent = 0")
    if days is not None:
        clauses.append("last_ts >= ?")
        params.append(time.time() - days * 86400)
    if project:
        needle = project.casefold()
        clauses.append("(instr(lower(project), ?) > 0 OR instr(lower(cwd), ?) > 0 "
                       "OR instr(lower(path), ?) > 0)")
        params.extend([needle, needle, needle])
    rows = conn.execute(
        """SELECT id, source, path, session_id, project, title, agent_name, branch, cwd,
                  first_ts, last_ts, total_lines, first_user_text
           FROM sessions WHERE """ + " AND ".join(clauses),
        params,
    ).fetchall()
    hits: list[NameHit] = []
    for row in rows:
        match_type = name_match_type(normalized_query, row[5], row[6])
        if match_type is None:
            continue
        hits.append(NameHit(row[0], row[1], Path(row[2]), *row[3:], match_type))
    hits.sort(key=lambda h: (
        NAME_MATCH_RANK[h.match_type], -h.last_ts, h.source, str(h.path)
    ))
    return hits if limit is None else hits[:limit]


def session_filters(args: argparse.Namespace, sources: set[str]) -> tuple[str, list[Any]]:
    clauses = [f"sessions.source IN ({','.join('?' * len(sources))})"]
    params: list[Any] = sorted(sources)
    if not args.include_subagents:
        clauses.append("sessions.subagent = 0")
    if args.days is not None:
        clauses.append("sessions.last_ts >= ?")
        params.append(time.time() - args.days * 86400)
    if args.project:
        needle = args.project.lower()
        clauses.append("(instr(lower(sessions.project), ?) > 0 OR instr(lower(sessions.cwd), ?) > 0"
                       " OR instr(lower(sessions.path), ?) > 0)")
        params.extend([needle, needle, needle])
    return " AND ".join(clauses), params


@dataclass
class TermStats:
    best: float = 0.0
    count: int = 0


def match_term(
    conn: sqlite3.Connection, expression: str, where: str, params: list[Any]
) -> dict[int, TermStats]:
    """Per session: best role-weighted BM25 score and row count for one query term."""
    rows = conn.execute(
        f"""SELECT messages.session_ref, messages.role, count(*), min(messages.rank)
            FROM messages JOIN sessions ON sessions.id = messages.session_ref
            WHERE messages MATCH ? AND {where}
            GROUP BY messages.session_ref, messages.role""",
        [expression, *params],
    ).fetchall()
    stats: dict[int, TermStats] = {}
    for pk, role, count, best_rank in rows:
        s = stats.setdefault(pk, TermStats())
        s.best = max(s.best, -best_rank * role_weight(role))
        s.count += count
    return stats


def rank_sessions(per_term: list[dict[int, TermStats]]) -> tuple[list[tuple[int, float]], bool]:
    """Sessions containing every term, else the partial matches; True when complete."""
    full: list[tuple[int, float]] = []
    partial: list[tuple[int, int, float]] = []
    for pk in set().union(*per_term):
        stats = [t[pk] for t in per_term if pk in t]
        score = sum(s.best for s in stats) + math.log1p(sum(s.count for s in stats))
        if len(stats) == len(per_term):
            full.append((pk, score))
        else:
            partial.append((pk, len(stats), score))
    if full:
        return full, True
    partial.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [(pk, score) for pk, _, score in partial], False


def role_weight(role: str) -> float:
    if role == META_ROLE:
        return META_WEIGHT
    if role in TOOL_ROLES:
        return TOOL_WEIGHT
    return 1.0


def weighted_score(c: Candidate) -> float:
    return c.score * role_weight(c.role)


def load_hits(conn: sqlite3.Connection, ranked: list[tuple[int, float]]) -> list[SessionHit]:
    hits: list[SessionHit] = []
    for pk, score in ranked:
        row = conn.execute(
            """SELECT source, path, session_id, project, title, agent_name, branch, cwd,
                      first_ts, last_ts, total_lines, first_user_text
               FROM sessions WHERE id = ?""", (pk,)
        ).fetchone()
        hit = SessionHit(pk, row[0], Path(row[1]), *row[2:])
        hit.score = score
        hits.append(hit)
    return hits


def finish_hits(
    conn: sqlite3.Connection, hits: list[SessionHit], any_expression: str, max_snippets: int
) -> None:
    for hit in hits:
        hit.match_count = conn.execute(
            "SELECT count(*) FROM messages WHERE messages MATCH ? AND session_ref = ? AND role != ?",
            (any_expression, hit.pk, META_ROLE),
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT role, line_no, rank, snippet(messages, 0, '**', '**', '...', 24)
               FROM messages WHERE messages MATCH ? AND session_ref = ?
               ORDER BY rank LIMIT ?""",
            (any_expression, hit.pk, max(max_snippets * 4, 12)),
        ).fetchall()
        cands = sorted((Candidate(r, l, -rank, s) for r, l, rank, s in rows),
                       key=weighted_score, reverse=True)
        shown = [c for c in cands if c.role != META_ROLE] or cands
        for c in shown[:max_snippets]:
            label = "title/id" if c.role == META_ROLE else f"{c.role} L{c.line_no}"
            hit.snippets.append(f"[{label}] {' '.join(c.snippet.split())}")


def print_results(hits: list[SessionHit], query: str, mode: str, total: int,
                  stats: tuple[int, int, float]) -> None:
    sessions, messages, built = stats
    print(f'# Chat Search: "{query}" ({mode})')
    print(f"Index: {sessions:,} sessions, {messages:,} messages, refreshed {format_age(built) or 'unknown'}")
    print(f"Found {total} matching session(s), showing {len(hits)}.\n")
    for i, h in enumerate(hits, 1):
        label = h.title or h.agent_name or h.session_id[:12] or h.path.stem[:12]
        age = format_age(h.last_ts)
        print(f"## {i}. [{h.source}] {label}{f' ({age})' if age else ''}")
        print(f"- Path: `{h.path}`")
        print(f"- Session: `{h.session_id}`")
        if h.title:
            print(f"- Title: {h.title}")
        if h.agent_name:
            print(f"- Agent: {h.agent_name}")
        if h.branch:
            print(f"- Branch: `{h.branch}`")
        if h.cwd:
            print(f"- CWD: `{h.cwd}`")
        print(f"- Period: {format_ts(h.first_ts)} to {format_ts(h.last_ts)}")
        print(f"- Lines: {h.total_lines:,} | Matches: {h.match_count} | Score: {h.score:.1f}")
        if h.first_user_text:
            print(f"- First ask: {h.first_user_text}")
        if h.snippets:
            print("- Matching snippets:")
            for snip in h.snippets:
                print(f"  - {snip}")
        print()


def hit_json(h: SessionHit) -> dict[str, Any]:
    return {
        "source": h.source,
        "path": str(h.path),
        "session_id": h.session_id,
        "project": h.project,
        "title": h.title,
        "agent_name": h.agent_name,
        "branch": h.branch,
        "cwd": h.cwd,
        "first_ts": format_ts(h.first_ts),
        "last_ts": format_ts(h.last_ts),
        "total_lines": h.total_lines,
        "match_count": h.match_count,
        "score": round(h.score, 2),
        "snippets": h.snippets,
        "first_user_text": h.first_user_text,
    }


def name_hit_json(h: NameHit) -> dict[str, Any]:
    return {
        "source": h.source,
        "path": str(h.path),
        "session_id": h.session_id,
        "project": h.project,
        "title": h.title,
        "agent_name": h.agent_name,
        "branch": h.branch,
        "cwd": h.cwd,
        "first_ts": format_ts(h.first_ts),
        "last_ts": format_ts(h.last_ts),
        "total_lines": h.total_lines,
        "match_type": h.match_type,
        "first_user_text": h.first_user_text,
    }


def print_name_results(
    hits: list[NameHit], query: str, mode: str, total: int,
    stats: tuple[int, int, float],
) -> None:
    sessions, messages, built = stats
    print(f'# Chat Name Search: "{query}" ({mode})')
    print(f"Index: {sessions:,} sessions, {messages:,} messages, refreshed {format_age(built) or 'unknown'}")
    print(f"Found {total} matching session(s), showing {len(hits)}.\n")
    for i, h in enumerate(hits, 1):
        label = h.title or h.agent_name or h.session_id[:12] or h.path.stem[:12]
        age = format_age(h.last_ts)
        print(f"## {i}. [{h.source}] {label}{f' ({age})' if age else ''}")
        print(f"- Match: {h.match_type}")
        print(f"- Path: `{h.path}`")
        print(f"- Session: `{h.session_id}`")
        if h.title:
            print(f"- Title: {h.title}")
        if h.agent_name:
            print(f"- Agent: {h.agent_name}")
        if h.project:
            print(f"- Project: {h.project}")
        if h.branch:
            print(f"- Branch: `{h.branch}`")
        if h.cwd:
            print(f"- CWD: `{h.cwd}`")
        print(f"- Period: {format_ts(h.first_ts)} to {format_ts(h.last_ts)}")
        print(f"- Lines: {h.total_lines:,}")
        if h.first_user_text:
            print(f"- First ask: {h.first_user_text}")
        print()


def index_command() -> str:
    return f"python3 {Path(__file__).resolve()} index"


def name_index_command() -> str:
    return f"python3 {Path(__file__).resolve()} name"


def run_search(args: argparse.Namespace, sources: set[str], index_path: Path) -> int:
    if not index_path.exists():
        print(f"No index at {index_path}. Build it first:\n  {index_command()}",
              file=sys.stderr)
        return 2
    conn = connect(index_path)
    if schema_version(conn) != SCHEMA_VERSION:
        print(f"Index at {index_path} has an old schema. Rebuild it:\n  {index_command()}",
              file=sys.stderr)
        return 2
    stats = index_stats(conn)
    if time.time() - stats[2] > STALE_AFTER_SECONDS:
        print(f"Note: index last refreshed {format_age(stats[2]) or 'never'}; "
              f"newer chats are missing until you run:\n  {index_command()}",
              file=sys.stderr)

    terms = parse_query(args.query)
    if not terms:
        print("Query has no searchable words.", file=sys.stderr)
        return 2
    where, params = session_filters(args, sources)
    expressions = [term_expression(t, i == len(terms) - 1) for i, t in enumerate(terms)]
    ranked, complete = rank_sessions([match_term(conn, e, where, params) for e in expressions])
    mode = "all words" if complete else "some words; no session contained all of them"

    query_str = " ".join(args.query)
    if not ranked:
        print(f'No sessions matched "{query_str}". Try other words, drop --days/--project, '
              "or add --include-subagents. If the chat is recent, refresh the index first.",
              file=sys.stderr)
        return 1

    hits = load_hits(conn, ranked)
    if args.recent:
        hits.sort(key=lambda h: (h.last_ts, h.score), reverse=True)
    elif complete:
        hits.sort(key=lambda h: (h.score, h.last_ts), reverse=True)
    total = len(hits)
    hits = hits[: args.limit]
    finish_hits(conn, hits, " OR ".join(expressions), args.snippets)
    conn.close()

    if args.json:
        print(json.dumps([hit_json(h) for h in hits], indent=2))
    else:
        print_results(hits, query_str, mode, total, stats)
    return 0


def run_name(args: argparse.Namespace, sources: set[str], index_path: Path) -> int:
    query = " ".join(args.name)
    if not normalize_name(query) or args.limit < 1 or not sources:
        print("Supply a nonempty name, at least one source, and a positive --limit.",
              file=sys.stderr)
        return 2
    if args.no_refresh:
        if not index_path.exists():
            print(f"No cached index at {index_path}; omit --no-refresh to build it automatically.",
                  file=sys.stderr)
            return 2
        mode = "cached mode (--no-refresh)"
        try:
            conn = connect_read_only(index_path)
        except sqlite3.Error as exc:
            print(f"Could not open cached index at {index_path}: {exc}", file=sys.stderr)
            return 2
    else:
        print("Name lookup: refreshing the selected source(s).", file=sys.stderr)
        try:
            code = run_index(sources, False, index_path, out=sys.stderr)
        except (OSError, sqlite3.Error) as exc:
            print(f"Could not refresh index at {index_path}: {exc}", file=sys.stderr)
            return 2
        if code:
            return code
        mode = "refreshed"
        try:
            conn = connect_read_only(index_path)
        except sqlite3.Error as exc:
            print(f"Could not open refreshed index at {index_path}: {exc}", file=sys.stderr)
            return 2

    try:
        if schema_version(conn) != SCHEMA_VERSION:
            print(f"Index at {index_path} has an old schema. Rebuild it with:\n  "
                  f"{index_command()}", file=sys.stderr)
            return 2
        stats = index_stats(conn)
        if args.no_refresh:
            print(f"Name lookup: {mode}; index last refreshed "
                  f"{format_age(stats[2]) or 'never'}.", file=sys.stderr)
        hits = find_by_name(
            conn, query, sources=sources,
            include_subagents=args.include_subagents,
            days=args.days, project=args.project, limit=None,
        )
        total = len(hits)
        hits = hits[:args.limit]
    finally:
        conn.close()

    if not hits:
        print(f'No session title or agent name matched "{query}". '
              "Try a shorter name or remove filters.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([name_hit_json(h) for h in hits], indent=2, ensure_ascii=False))
    else:
        print_name_results(hits, query, mode, total, stats)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_sources(value: str) -> set[str] | None:
    if value.strip().lower() == "all":
        return set(ALL_SOURCES)
    sources = {s.strip().lower() for s in value.split(",") if s.strip()}
    unknown = sources - set(ALL_SOURCES)
    if unknown:
        print(f"Unknown source(s): {', '.join(sorted(unknown))}. Valid: {', '.join(ALL_SOURCES)}",
              file=sys.stderr)
        return None
    return sources


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "index":
        parser = argparse.ArgumentParser(prog="search_chats.py index",
                                         description="Build or refresh the transcript index.")
        parser.add_argument("--rebuild", action="store_true", help="Discard the index and start over")
        parser.add_argument("--source", default="all",
                            help="Comma-separated harnesses to (re)index: claude, codex, cursor, or all")
        args = parser.parse_args(argv[1:])
        sources = parse_sources(args.source)
        return 2 if sources is None else run_index(sources, args.rebuild, INDEX_PATH)

    if argv and argv[0] == "name":
        parser = argparse.ArgumentParser(
            prog="search_chats.py name",
            description="Find chats by title or agent name (metadata only).",
        )
        parser.add_argument("name", nargs="+", help="Title or agent-name text to match")
        parser.add_argument("--source", default="all",
                            help="Comma-separated harnesses: claude, codex, cursor, or all")
        parser.add_argument("--project", "-p",
                            help="Only sessions whose project, cwd, or path contains this substring")
        parser.add_argument("--days", "-d", type=int, default=None,
                            help="Only sessions active in the last N days")
        parser.add_argument("--limit", "-n", type=int, default=10,
                            help="Max results (default: 10)")
        parser.add_argument("--include-subagents", action="store_true",
                            help="Include Codex/Cursor subagent threads (skipped by default)")
        parser.add_argument("--no-refresh", action="store_true",
                            help="Use the cached index read-only; default refreshes selected sources")
        parser.add_argument("--json", action="store_true", help="Output as JSON")
        args = parser.parse_args(argv[1:])
        sources = parse_sources(args.source)
        return 2 if sources is None else run_name(args, sources, INDEX_PATH)

    if argv and argv[0] == "search":
        argv = argv[1:]
    parser = argparse.ArgumentParser(
        prog="search_chats.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", nargs="+",
                        help='Words that must all appear (stemmed); "quoted phrases" match exactly')
    parser.add_argument("--source", default="all",
                        help="Comma-separated harnesses: claude, codex, cursor, or all (default: all)")
    parser.add_argument("--project", "-p",
                        help="Only sessions whose project, cwd, or path contains this substring")
    parser.add_argument("--days", "-d", type=int, default=None,
                        help="Only sessions active in the last N days")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--snippets", "-s", type=int, default=5,
                        help="Max snippets per session (default: 5)")
    parser.add_argument("--recent", action="store_true",
                        help="Order by last activity instead of relevance")
    parser.add_argument("--include-subagents", action="store_true",
                        help="Include Codex/Cursor subagent threads (skipped by default)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args(argv)
    sources = parse_sources(args.source)
    return 2 if sources is None else run_search(args, sources, INDEX_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
