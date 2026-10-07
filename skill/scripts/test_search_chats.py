"""Index and query tests over synthetic transcripts for all three harnesses."""

import contextlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

import search_chats as sc


def iso(days_ago=0):
    return (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def claude(role, text, days_ago=0, **extra):
    session_id = extra.pop("session_id", "claude-1")
    return {"type": role, "sessionId": session_id, "cwd": f"/Users/me/proj/{'webshop' if session_id == 'claude-1' else 'other'}",
            "gitBranch": "main", "timestamp": iso(days_ago),
            "message": {"role": role, "content": [{"type": "text", "text": text}]}, **extra}


def codex_meta(session_id, thread_source="user"):
    return {"type": "session_meta", "timestamp": iso(), "payload": {
        "id": session_id, "cwd": "/Users/me/proj/contracts", "thread_source": thread_source,
        "git": {"branch": "feat"}, "instructions": "system prompt with zebra"}}


def codex_message(role, text):
    return {"type": "response_item", "timestamp": iso(), "payload": {
        "type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}}


def codex_event(kind, text):
    return {"type": "event_msg", "timestamp": iso(), "payload": {"type": kind, "message": text}}


def cursor(role, text):
    return {"role": role, "message": {"role": role, "content": text}}


def claude_title(title, session_id):
    return {"type": "custom-title", "sessionId": session_id, "customTitle": title,
            "timestamp": iso()}


class SearchChatsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        overrides = {
            "CLAUDE_PROJECTS_ROOT": self.root / "claude",
            "CODEX_SESSIONS_ROOT": self.root / "codex",
            "CODEX_INDEX": self.root / "codex_index.jsonl",
            "CURSOR_PROJECTS_ROOT": self.root / "cursor",
            "CURSOR_DB": self.root / "missing.vscdb",
            "INDEX_PATH": self.root / "index.sqlite",
        }
        for name, value in overrides.items():
            self.addCleanup(setattr, sc, name, getattr(sc, name))
            setattr(sc, name, value)
        self.claude_path = self.write("claude/-Users-me-proj-webshop/claude-1.jsonl",
                                      claude("user", "please run the helm deploy for staging"),
                                      claude("assistant", "Deploying the chart now."))
        self.write("claude/-Users-me-proj-other/claude-2.jsonl",
                   claude("user", "the helm chart values look wrong", days_ago=400, session_id="claude-2"),
                   claude("assistant", "Old conversation.", days_ago=400, session_id="claude-2"))
        self.write("codex/2026/09/10/rollout-a.jsonl",
                   codex_meta("codex-aaaa-1111"),
                   {"type": "turn_context", "timestamp": iso(), "payload": {"model": "zebra"}},
                   codex_message("user", "draft the vendor contract"),
                   codex_event("user_message", "draft the vendor contract"))
        self.write("codex/2026/09/11/rollout-b.jsonl",
                   codex_meta("codex-bbbb-2222", thread_source="subagent"),
                   codex_message("assistant", "subagent summary about quokka"))
        self.write("cursor/-Users-me-proj-dashboard/agent-transcripts/cur-1/cur-1.jsonl",
                   cursor("user", "fix the auth middleware"),
                   cursor("assistant", "Done."))
        (self.root / "codex_index.jsonl").write_text(
            json.dumps({"id": "codex-aaaa-1111", "thread_name": "Vendor contract draft"}) + "\n")

    def write(self, relative, *records):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return path

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sc.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def index(self, *argv):
        code, out, _ = self.run_cli("index", *argv)
        self.assertEqual(code, 0, out)
        return out

    def search(self, *argv):
        code, out, err = self.run_cli(*argv, "--json")
        return code, (json.loads(out) if code == 0 else []), err

    def name(self, *argv):
        code, out, err = self.run_cli("name", *argv, "--json")
        return code, (json.loads(out) if code == 0 else []), err

    def ids(self, hits):
        return [h["session_id"] for h in hits]

    def test_search_without_index_explains_how_to_build(self):
        code, _, err = self.search("helm")
        self.assertEqual(code, 2)
        self.assertIn("search_chats.py index", err)

    def test_all_words_required_then_or_fallback(self):
        self.index()
        _, hits, _ = self.search("helm", "deploy")
        self.assertEqual(self.ids(hits), ["claude-1"])
        _, hits, _ = self.search("helm", "nosuchwordanywhere")
        self.assertEqual(sorted(self.ids(hits)), ["claude-1", "claude-2"])

    def test_words_may_be_in_different_messages_of_one_session(self):
        self.index()
        # "chart" is in the assistant reply, "staging" in the user's ask; claude-2 has only "chart".
        self.assertEqual(self.ids(self.search("chart", "staging")[1]), ["claude-1"])

    def test_stemming_prefix_and_phrases(self):
        self.index()
        self.assertEqual(self.ids(self.search("deploying")[1]), ["claude-1"])
        self.assertEqual(self.ids(self.search("stag")[1]), ["claude-1"])
        self.assertEqual(self.ids(self.search('"deploy for staging"')[1]), ["claude-1"])
        self.assertEqual(self.search('"staging deploy"')[0], 1)

    def test_titles_and_session_ids_match(self):
        self.index()
        _, hits, _ = self.search("vendor", "draft")
        self.assertEqual(self.ids(hits), ["codex-aaaa-1111"])
        self.assertEqual(hits[0]["title"], "Vendor contract draft")
        self.assertEqual(self.ids(self.search("codex-aaaa-1111")[1]), ["codex-aaaa-1111"])

    def test_codex_mirrors_deduped_and_system_lines_skipped(self):
        self.index()
        _, hits, _ = self.search("contract")
        self.assertEqual(hits[0]["match_count"], 1)
        self.assertEqual(self.search("zebra")[0], 1)

    def test_filters(self):
        self.index()
        self.assertEqual(self.search("quokka")[0], 1)
        self.assertEqual(self.ids(self.search("quokka", "--include-subagents")[1]), ["codex-bbbb-2222"])
        self.assertEqual(self.ids(self.search("helm", "--days", "30")[1]), ["claude-1"])
        self.assertEqual(self.ids(self.search("helm", "--project", "webshop")[1]), ["claude-1"])
        self.assertEqual(self.ids(self.search("helm", "--source", "codex,cursor")[1]), [])
        self.assertEqual(self.ids(self.search("middleware")[1]), ["cur-1"])
        self.assertEqual(self.search("middleware")[1][0]["project"], "dashboard")

    def test_incremental_index_tracks_changes(self):
        first = self.index()
        self.assertIn("+2 added", first.splitlines()[0])
        second = self.index()
        self.assertIn("+0 added", second)
        self.assertIn("2 unchanged", second.splitlines()[0])
        with self.claude_path.open("a") as f:
            f.write(json.dumps(claude("user", "now rollback the pelican release")) + "\n")
        self.claude_path.touch()
        (self.root / "cursor/-Users-me-proj-dashboard/agent-transcripts/cur-1/cur-1.jsonl").unlink()
        third = self.index()
        self.assertIn("~1 updated", third)
        self.assertIn("-1 removed", third)
        self.assertEqual(self.ids(self.search("pelican")[1]), ["claude-1"])
        self.assertEqual(self.search("middleware")[0], 1)

    def test_rebuild_replaces_index_atomically(self):
        self.index()
        self.index("--rebuild")
        self.assertFalse((self.root / "index.sqlite.building").exists())
        self.assertEqual(self.ids(self.search("helm", "deploy")[1]), ["claude-1"])

    def test_conversation_outranks_repetitive_tool_output(self):
        self.write("claude/-Users-me-proj-webshop/claude-4.jsonl",
                   claude("user", "look at the pelican migration", session_id="claude-4"))
        tool_call = claude("assistant", "", session_id="claude-5")
        tool_call["message"]["content"] = [{"type": "tool_use", "input": {"command": "grep pelican " * 20}}]
        tool_result = claude("user", "", session_id="claude-5")
        tool_result["toolUseResult"] = {"content": "pelican.py pelican_test.py " * 30}
        self.write("claude/-Users-me-proj-webshop/claude-5.jsonl", tool_call, tool_result)
        self.index()
        _, hits, _ = self.search("pelican")
        self.assertEqual(self.ids(hits), ["claude-4", "claude-5"])
        self.assertTrue(hits[1]["snippets"][0].startswith(("[tool_use", "[tool_result")), hits[1]["snippets"])

    def test_injected_instructions_are_not_indexed(self):
        self.write("codex/2026/09/12/rollout-c.jsonl",
                   codex_meta("codex-cccc-3333"),
                   codex_message("developer", "AGENTS.md says walrus"),
                   codex_message("user", "<environment_context>\ncwd platypus\n</environment_context>"),
                   codex_message("user", "real question about narwhals"))
        meta_record = claude("user", "caveat about a wombat", isMeta=True)
        self.write("claude/-Users-me-proj-webshop/claude-6.jsonl", meta_record)
        self.index()
        self.assertEqual(self.search("walrus")[0], 1)
        self.assertEqual(self.search("platypus")[0], 1)
        self.assertEqual(self.search("wombat")[0], 1)
        self.assertEqual(self.ids(self.search("narwhal")[1]), ["codex-cccc-3333"])

    def test_search_subcommand_allows_reserved_words(self):
        self.index()
        self.write("claude/-Users-me-proj-webshop/claude-3.jsonl",
                   claude("user", "rebuild the search index tonight", session_id="claude-3"))
        self.index()
        self.assertEqual(self.ids(self.search("search", "index", "tonight")[1]), ["claude-3"])

    def test_name_matches_metadata_only_and_ranks_exact_titles(self):
        self.write(
            "claude/-Users-me-proj-webshop/name-exact.jsonl",
            claude_title("Weekly team sync status", "name-exact"),
            claude("user", "body-only words are deliberately unrelated", session_id="name-exact"),
        )
        self.write(
            "claude/-Users-me-proj-other/name-partial.jsonl",
            claude_title("Weekly team sync status notes", "name-partial"),
            claude("user", "body-only words are deliberately unrelated", session_id="name-partial"),
        )
        self.write(
            "claude/-Users-me-proj-webshop/name-duplicate.jsonl",
            claude_title("Weekly team sync status", "name-duplicate"),
            claude("user", "body-only words are deliberately unrelated", session_id="name-duplicate"),
        )
        self.write(
            "claude/-Users-me-proj-webshop/name-body.jsonl",
            claude("user", "Weekly team sync status", session_id="name-body"),
        )
        self.index("--source", "claude")
        code, hits, _ = self.name(
            "  WEEKLY   team sync STATUS ", "--source", "claude", "--no-refresh"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.ids(hits)[:3], ["name-duplicate", "name-exact", "name-partial"])
        self.assertEqual([hit["match_type"] for hit in hits[:3]],
                         ["exact_title", "exact_title", "partial_title"])
        self.assertNotIn("name-body", self.ids(hits))

    def test_name_normalizes_unicode_and_supports_agent_metadata(self):
        self.write(
            "claude/-Users-me-proj-webshop/name-unicode.jsonl",
            claude_title("Straße  Sync", "name-unicode"),
            {"type": "agent-name", "sessionId": "name-unicode", "agentName": "Night Pilot",
             "timestamp": iso()},
        )
        self.index("--source", "claude")
        self.assertEqual(self.ids(self.name("STRASSE   sync", "--source", "claude", "--no-refresh")[1]),
                         ["name-unicode"])
        code, hits, _ = self.name("night pilot", "--source", "claude", "--no-refresh")
        self.assertEqual(code, 0)
        self.assertEqual(hits[0]["match_type"], "exact_agent_name")

    def test_name_auto_refreshes_missing_index_and_reports_cached_mode(self):
        self.write(
            "claude/-Users-me-proj-webshop/name-fresh.jsonl",
            claude_title("Freshly indexed chat", "name-fresh"),
            claude("user", "hello", session_id="name-fresh"),
        )
        code, hits, err = self.name("freshly indexed chat", "--source", "claude")
        self.assertEqual(code, 0)
        self.assertEqual(self.ids(hits), ["name-fresh"])
        self.assertIn("refreshing", err)

        self.root.joinpath("index.sqlite").unlink()
        code, _, err = self.name("freshly indexed chat", "--source", "claude", "--no-refresh")
        self.assertEqual(code, 2)
        self.assertIn("cached index", err)

    def test_name_refreshes_codex_title_without_touching_transcript_or_old_fts(self):
        self.root.joinpath("codex_index.jsonl").write_text(
            json.dumps({"id": "codex-aaaa-1111", "thread_name": "Legacy Codex label"}) + "\n"
        )
        self.index("--source", "codex")
        self.assertEqual(self.ids(self.name("legacy codex label", "--source", "codex",
                                            "--no-refresh")[1]), ["codex-aaaa-1111"])
        self.root.joinpath("codex_index.jsonl").write_text(
            json.dumps({"id": "codex-aaaa-1111", "thread_name": "Renamed Codex label"}) + "\n"
        )
        code, hits, _ = self.name("renamed codex label", "--source", "codex")
        self.assertEqual(code, 0)
        self.assertEqual(self.ids(hits), ["codex-aaaa-1111"])
        self.assertEqual(self.search("legacy", "--source", "codex")[0], 1)
        self.assertEqual(self.search("renamed", "--source", "codex")[1][0]["title"],
                         "Renamed Codex label")

    def write_cursor_headers(self, name, *, cwd=None, created=0, updated=0):
        db_path = self.root / "cursor_headers.vscdb"
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS composerHeaders (
            composerId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER,
            isSubagent INTEGER, value TEXT)""")
        conn.execute("DELETE FROM composerHeaders")
        value = {"name": name}
        if cwd is not None:
            value["workspaceIdentifier"] = {"uri": {"fsPath": cwd}}
        conn.execute("INSERT INTO composerHeaders VALUES (?,?,?,?,?)",
                     ("cur-1", created, updated, 0, json.dumps(value)))
        conn.commit()
        conn.close()
        sc.CURSOR_DB = db_path

    def test_name_refreshes_cursor_rename_and_preserves_transcript_fallbacks(self):
        self.write_cursor_headers("Cursor Original", cwd="/Users/me/proj/dashboard",
                                  created=1700000000000, updated=1700000010000)
        self.index("--source", "cursor")
        self.write_cursor_headers("Cursor Renamed")
        code, hits, _ = self.name("cursor renamed", "--source", "cursor", "--project", "dashboard")
        self.assertEqual(code, 0)
        self.assertEqual(self.ids(hits), ["cur-1"])
        self.assertEqual(hits[0]["project"], "dashboard")
        self.assertEqual(hits[0]["first_ts"], sc.format_ts(1700000000))
        self.assertEqual(hits[0]["last_ts"], sc.format_ts(1700000010))
        self.assertEqual(self.search("original", "--source", "cursor")[0], 1)
        self.assertEqual(self.search("renamed", "--source", "cursor")[1][0]["title"],
                         "Cursor Renamed")

    def test_name_claude_rename_stays_cached_until_refresh(self):
        with self.claude_path.open("a") as f:
            f.write(json.dumps(claude_title("Original title", "claude-1")) + "\n")
        self.index("--source", "claude")
        with self.claude_path.open("a") as f:
            f.write(json.dumps(claude_title("Replacement title", "claude-1")) + "\n")
        code, hits, err = self.name("original title", "--source", "claude", "--no-refresh")
        self.assertEqual(code, 0)
        self.assertEqual(self.ids(hits), ["claude-1"])
        self.assertIn("cached mode", err)
        self.assertEqual(self.search("replacement", "--source", "claude")[0], 1)
        self.assertEqual(self.ids(self.name("replacement title", "--source", "claude")[1]),
                         ["claude-1"])
        self.assertEqual(self.name("original title", "--source", "claude", "--no-refresh")[0], 1)

    def test_name_filters_and_reserved_word_content_search(self):
        self.root.joinpath("codex_index.jsonl").write_text("\n".join([
            json.dumps({"id": "codex-aaaa-1111", "thread_name": "Named chat"}),
            json.dumps({"id": "codex-bbbb-2222", "thread_name": "Named chat"}),
        ]) + "\n")
        title = claude_title("Named chat", "old-chat")
        title["timestamp"] = iso(400)
        self.write("claude/-Users-me-proj-old/old-chat.jsonl", title,
                   claude("user", "name this sample", days_ago=400, session_id="old-chat"))
        self.index()
        self.assertEqual(set(self.ids(self.name("Named chat", "--no-refresh")[1])),
                         {"old-chat", "codex-aaaa-1111"})
        self.assertEqual(self.ids(self.name("Named chat", "--days", "1", "--no-refresh")[1]),
                         ["codex-aaaa-1111"])
        self.assertEqual(len(self.name("Named chat", "--include-subagents", "--no-refresh")[1]), 3)
        self.assertEqual(self.ids(self.name("Named chat", "--source", "claude", "--no-refresh")[1]),
                         ["old-chat"])
        self.assertEqual(self.ids(self.name("Named chat", "--project", "contracts", "--no-refresh")[1]),
                         ["codex-aaaa-1111"])
        self.assertEqual(len(self.name("Named chat", "--limit", "1", "--no-refresh")[1]), 1)
        self.assertEqual(self.ids(self.search("search", "name", "this", "sample")[1]), ["old-chat"])

    def test_read_only_name_lookup_accepts_uri_characters_and_cannot_write(self):
        sc.INDEX_PATH = self.root / "index #1?.sqlite"
        self.index("--source", "codex")
        before = sc.INDEX_PATH.read_bytes()
        self.assertEqual(self.ids(self.name("Vendor contract draft", "--no-refresh")[1]),
                         ["codex-aaaa-1111"])
        conn = sc.connect_read_only(sc.INDEX_PATH)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM sessions")
            self.assertIsInstance(sc.find_by_name(conn, "Vendor")[0].path, Path)
        finally:
            conn.close()
        self.assertEqual(sc.INDEX_PATH.read_bytes(), before)

    def test_unchanged_external_metadata_does_not_rewrite_index(self):
        self.write_cursor_headers("Cursor stable")
        self.index()
        conn = sc.connect(sc.INDEX_PATH)
        try:
            for source in ("codex", "cursor"):
                before = conn.total_changes
                sc.refresh_source_metadata(conn, source)
                self.assertEqual(conn.total_changes, before)
        finally:
            conn.close()

    def test_invalid_name_lookup_does_not_create_index(self):
        for args in [("   ",), ("title", "--limit", "0"), ("title", "--source", "")]:
            with self.subTest(args=args):
                self.assertEqual(self.name(*args)[0], 2)
                self.assertFalse(sc.INDEX_PATH.exists())


if __name__ == "__main__":
    unittest.main()
