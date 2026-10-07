"""Conversation-level regression tests for the two supported transcript formats."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from read_chat import read_chat


def claude(role, content, *, message_id=None, stop=None, **metadata):
    message = {"role": role, "content": content}
    if message_id is not None:
        message["id"] = message_id
    if stop is not None:
        message["stop_reason"] = stop
    return {"type": role, "sessionId": "claude-session", "message": message,
            "timestamp": "2026-09-11T22:00:00Z", **metadata}


def codex(role, text, channel=None):
    return {"type": "response_item", "payload": {
        "type": "message", "role": role, "channel": channel,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
    }}


def event(kind, **payload):
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


def block(text):
    return {"type": "text", "text": text}


class ReadChatTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "session.jsonl"

    def write(self, *records):
        self.path.write_text("\n".join(json.dumps(record) for record in records) + "\n",
                             encoding="utf-8")

    def texts(self, **options):
        return [message["text"] for message in read_chat(self.path, **options)["messages"]]

    def test_claude_counts_roles_and_repeated_human_messages(self):
        self.write(claude("user", "again"), claude("assistant", [block("one")]),
                   claude("user", "again"), claude("assistant", [block("two")]),
                   claude("user", "latest"))
        result = read_chat(self.path, last=3)
        self.assertEqual(result["source"], "claude")
        self.assertEqual(result["session_id"], "claude-session")
        self.assertEqual(result["available"], {"user": 3, "assistant": 2})
        self.assertEqual([m["text"] for m in result["messages"]], ["again", "two", "latest"])
        self.assertEqual(self.texts(last=2, role="user"), ["again", "latest"])
        self.assertEqual(self.texts(last_user=3, last_assistant=1), ["again", "again", "two", "latest"])
        self.assertEqual(self.texts(last_assistant=1), ["two"])

    def test_claude_excludes_tools_reasoning_metadata_and_images(self):
        records = [claude("user", "visible question")]
        for flag in ("isMeta", "isCompactSummary", "isVisibleInTranscriptOnly", "turnCompanion"):
            records.append(claude("user", "injected", **{flag: True}))
        records.extend([
            claude("user", "peer notification", origin={"kind": "peer"}),
            claude("assistant", "API failure", isApiErrorMessage=True, stop="stop_sequence"),
            claude("user", [{"type": "tool_result", "content": "tool output"}]),
            claude("assistant", [{"type": "thinking", "thinking": "private reasoning"},
                                 {"type": "tool_use", "input": {"text": "tool argument"}}]),
            claude("user", [block("image question"), {"type": "image", "source": {"data": "image bytes"}}]),
            claude("assistant", [block("visible answer")], stop="end_turn"),
        ])
        self.write(*records)
        self.assertEqual(self.texts(), ["visible question", "image question", "visible answer"])

    def test_claude_commands_interruptions_and_injected_envelopes(self):
        self.write(*[claude("user", value) for value in (
            "<command-name>/model</command-name>",
            "<local-command-caveat>local only</local-command-caveat>",
            "<local-command-stdout>Copied</local-command-stdout>",
            "<local-command-stderr>Failed</local-command-stderr>",
            "<task-notification>finished</task-notification>",
            "[Request interrupted by user]",
            "[Request interrupted by user for tool use]",
            "<system-reminder>injected</system-reminder>Real request",
        )])
        self.assertEqual(self.texts(), ["Real request"])

    def test_claude_queued_human_prompts_count_once_per_identity(self):
        def queued(text, uuid, mode="prompt", origin="human"):
            return {"type": "attachment", "sessionId": "claude-session", "attachment": {
                "type": "queued_command", "commandMode": mode, "prompt": text,
                "source_uuid": uuid, "origin": {"kind": origin},
                "timestamp": "2026-09-11T22:01:00Z",
            }}

        self.write(
            claude("user", "start"),
            {"type": "queue-operation", "operation": "enqueue", "content": "continue"},
            queued("continue", "q1"),
            claude("user", "continue", uuid="q1", promptSource="queued"),
            queued("continue", "q2"),
            queued("notification", "q3", mode="task-notification", origin="task-notification"),
            queued("peer message", "q4", origin="peer"),
            queued("command", "q5", mode="bash"),
        )
        result = read_chat(self.path)
        self.assertEqual(self.texts(), ["start", "continue", "continue"])
        self.assertEqual(result["available"]["user"], 3)
        self.assertEqual(result["messages"][1]["line"], 3)
        self.assertEqual(result["messages"][1]["timestamp"], "2026-09-11T22:01:00Z")
        self.assertEqual(self.texts(last=2, before_line=5), ["start", "continue"])

    def test_claude_split_text_and_later_completion_keep_first_text_line(self):
        self.write(
            claude("user", "question"),
            claude("assistant", [{"type": "thinking"}], message_id="reply"),
            claude("assistant", [block("first")], message_id="reply", uuid="text-1"),
            claude("assistant", [block("second")], message_id="reply", uuid="text-2"),
            claude("assistant", [block("second")], message_id="reply", uuid="text-2", stop="end_turn"),
            claude("user", "unanswered"),
        )
        result = read_chat(self.path, last_user=1, last_assistant=1, final_only=True)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["available"], {"user": 2, "assistant": 1})
        self.assertEqual(result["messages"][0]["text"], "first\nsecond")
        self.assertEqual(result["messages"][0]["line"], 3)
        self.assertEqual(result["messages"][0]["channel"], "final")
        self.assertEqual(result["messages"][1]["text"], "unanswered")
        self.assertEqual(self.texts(last_assistant=1, final_only=True, before_line=5), [])

    def test_claude_completion_is_explicit_and_progress_remains_readable(self):
        self.write(
            claude("assistant", [block("working")], message_id="1", stop="tool_use"),
            claude("assistant", [block("done")], message_id="2", stop="end_turn"),
            claude("assistant", [block("still streaming")], message_id="3"),
            claude("assistant", [block("cut off")], message_id="4", stop="max_tokens"),
        )
        self.assertEqual(self.texts(), ["working", "done", "still streaming", "cut off"])
        self.assertEqual(self.texts(final_only=True), ["done"])

    def test_claude_pagination_has_no_overlap_and_reconstructs_full_text(self):
        self.write(
            claude("user", "first"),
            claude("assistant", [block("α"), block("β")], message_id="a", stop="end_turn"),
            claude("user", "second"),
            claude("assistant", [block("γ")], message_id="b"),
            claude("assistant", [block("δ")], message_id="b", stop="end_turn"),
            claude("user", "third"),
        )
        page = read_chat(self.path, last=2)
        older = read_chat(self.path, last=2, before_line=page["oldest_returned_line"])
        self.assertEqual([m["text"] for m in page["messages"]], ["γ\nδ", "third"])
        self.assertEqual([m["text"] for m in older["messages"]], ["α\nβ", "second"])

    def test_codex_canonical_messages_suppress_mirrors_and_reasoning(self):
        self.write(
            {"type": "session_meta", "payload": {"id": "codex-session"}},
            event("user_message", message="question"),
            codex("user", "question"),
            codex("assistant", "hidden reasoning", "analysis"),
            codex("assistant", "working", "commentary"),
            event("agent_message", message="done"),
            codex("assistant", "done", "final"),
            event("task_complete", last_agent_message="done"),
        )
        result = read_chat(self.path)
        self.assertEqual(result["source"], "codex")
        self.assertEqual(result["session_id"], "codex-session")
        self.assertEqual(result["available"], {"user": 1, "assistant": 2})
        self.assertEqual(self.texts(), ["question", "working", "done"])
        self.assertEqual(self.texts(final_only=True), ["question", "done"])

    def test_codex_legacy_completed_reply_can_precede_latest_user(self):
        self.write(
            event("user_message", message="first"),
            event("agent_message", message="done"),
            event("task_complete", last_agent_message="done"),
            event("user_message", message="latest"),
        )
        result = read_chat(self.path, last_user=1, last_assistant=1, final_only=True)
        self.assertEqual([m["text"] for m in result["messages"]], ["done", "latest"])
        self.assertEqual(result["messages"][0]["line"], 2)
        self.assertEqual(self.texts(last_assistant=1, final_only=True, before_line=3), [])

    def test_incomplete_jsonl_tail_does_not_lose_messages(self):
        self.write(claude("user", "complete message"))
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write('{"type": "assistant",')
        self.assertEqual(self.texts(), ["complete message"])

    def test_unsupported_format_has_actionable_error(self):
        self.write({"role": "user", "message": "unsupported format"})
        with self.assertRaisesRegex(ValueError, "Codex or Claude Code JSONL"):
            read_chat(self.path)

    def test_cli_json_preserves_long_text_and_reports_source(self):
        text = "No truncation. " * 2000
        self.write(claude("user", "question"), claude("assistant", [block(text)], stop="end_turn"))
        command = [sys.executable, str(Path(__file__).with_name("read_chat.py")), str(self.path)]
        output = subprocess.run(command + ["--last", "1", "--json"], capture_output=True, text=True, check=True)
        result = json.loads(output.stdout)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["source"], "claude")
        self.assertEqual(result["messages"][0]["text"], text.strip())
        invalid = subprocess.run(command + ["--last", "2", "--last-user", "1"], capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("Use either --last/--role", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
