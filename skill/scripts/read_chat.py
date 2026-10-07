#!/usr/bin/env python3
"""Read recent human/assistant text from Codex or Claude Code JSONL (stdlib only).

Examples:
  read_chat.py /path/to/rollout.jsonl --last 20
  read_chat.py /path/to/rollout.jsonl --last 10 --role user
  read_chat.py /path/to/rollout.jsonl --last-user 30 --last-assistant 1 --final-only
  read_chat.py /path/to/claude-session.jsonl --last 50
"""

import argparse
from collections import deque
import json
from pathlib import Path
import re
import sys


WRAPPERS = re.compile(
    r"\A\s*<(environment_context|recommended_plugins|skill|"
    r"codex_internal_context|turn_aborted|system-reminder)\b[^>]*>.*?</\1>\s*",
    re.DOTALL,
)
CLAUDE_COMMAND = re.compile(
    r"\A\s*<(command-name|local-command-caveat|local-command-stdout|"
    r"local-command-stderr|task-notification)\b"
)


def text_blocks(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"] for block in content
        if isinstance(block, dict)
        and block.get("type") in ("input_text", "output_text", "text", "Text")
        and isinstance(block.get("text"), str)
    )


def user_text(text):
    """Remove known leading injected envelopes, preserving ordinary user prose."""
    while WRAPPERS.match(text):
        text = WRAPPERS.sub("", text, count=1)
    # Desktop attachment preamble; leave the user's actual request intact.
    if text.lstrip().startswith("# Files") and "## My request:" in text:
        text = text.split("## My request:", 1)[1]
    # Images themselves are excluded by text_blocks; retain their local references.
    text = re.sub(r'<image\b([^>]*)>\s*</image>', r'[Image reference:\1]', text)
    return text.strip()


def claude_metadata(record):
    origin = record.get("origin")
    return any(record.get(flag) for flag in (
        "isMeta", "isCompactSummary", "isVisibleInTranscriptOnly",
        "turnCompanion", "isApiErrorMessage",
    )) or (record["type"] == "user" and isinstance(origin, dict)
           and origin.get("kind") in ("peer", "task-notification"))


def claude_text(record):
    text = text_blocks(record["message"].get("content"))
    if record["type"] == "user":
        text = user_text(text)
        if CLAUDE_COMMAND.match(text) or text in (
            "[Request interrupted by user]",
            "[Request interrupted by user for tool use]",
        ):
            return ""
    return text.strip()


def records(path, before_line=None):
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line, raw in enumerate(stream, 1):
            if before_line is not None and line >= before_line:
                break
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                # Running sessions may end with an incomplete line.
                continue
            if isinstance(record, dict):
                yield line, record


def read_chat(path, *, last=20, role="both", last_user=None,
              last_assistant=None, final_only=False, before_line=None):
    per_role = last_user is not None or last_assistant is not None
    capacity = max(last, last_user or 0, last_assistant or 0, 1)
    canonical = {r: deque(maxlen=capacity) for r in ("user", "assistant")}
    legacy = {r: deque(maxlen=capacity) for r in canonical}
    finals = deque(maxlen=capacity)
    counts = {r: 0 for r in canonical}
    legacy_counts = counts.copy()
    final_count = 0
    latest_assistant = None
    latest_final_line = None
    session_id = None
    source = None
    claude_id = None
    claude_assistant = None
    claude_complete = False
    claude_uuids = set()
    claude_user_uuids = set()

    def make(line, record, r, text, channel=None):
        return {"line": line, "timestamp": record.get("timestamp"),
                "role": r, "channel": channel, "text": text}

    for line, record in records(path, before_line):
        kind = record.get("type")
        if kind == "attachment":
            attachment = record.get("attachment")
            if not isinstance(attachment, dict):
                continue
            origin = attachment.get("origin")
            if (attachment.get("type") != "queued_command"
                    or attachment.get("commandMode") != "prompt"
                    or not isinstance(origin, dict) or origin.get("kind") != "human"):
                continue
            record = dict(record, type="user", origin=origin,
                          uuid=attachment.get("source_uuid"),
                          timestamp=attachment.get("timestamp") or record.get("timestamp"),
                          message={"content": attachment.get("prompt")})
            kind = "user"
        message = record.get("message")
        if source != "codex" and kind in ("user", "assistant") and isinstance(message, dict):
            source = "claude"
            session_id = session_id or record.get("sessionId")
            if claude_metadata(record):
                continue
            text = claude_text(record)
            if kind == "user":
                if text:
                    uuid = record.get("uuid")
                    if uuid and uuid in claude_user_uuids:
                        continue
                    if uuid:
                        claude_user_uuids.add(uuid)
                    canonical[kind].append(make(line, record, kind, text))
                    counts[kind] += 1
                    claude_id = None
                    claude_assistant = None
                continue
            message_id = message.get("id")
            if not message_id or message_id != claude_id:
                claude_id = message_id
                claude_assistant = None
                claude_complete = False
                claude_uuids.clear()
            uuid = record.get("uuid")
            repeated = uuid and uuid in claude_uuids
            if uuid:
                claude_uuids.add(uuid)
            if text and not repeated:
                if claude_assistant is None:
                    claude_assistant = make(line, record, kind, text)
                    canonical[kind].append(claude_assistant)
                    counts[kind] += 1
                else:
                    # Claude may save separate text blocks with the same API message ID.
                    claude_assistant["text"] += "\n" + text
            claude_complete |= message.get("stop_reason") == "end_turn"
            if claude_complete and claude_assistant and claude_assistant["channel"] != "final":
                claude_assistant["channel"] = "final"
                finals.append(claude_assistant)
                final_count += 1
            continue
        if source == "claude":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind in ("session_meta", "response_item", "event_msg", "turn_context"):
            source = "codex"
        if kind == "session_meta":
            session_id = payload.get("id") or payload.get("session_id")
        if kind == "response_item" and payload.get("type") == "message":
            r = payload.get("role")
            if r not in canonical or payload.get("channel") == "analysis":
                continue
            text = text_blocks(payload.get("content"))
            text = user_text(text) if r == "user" else text.strip()
            if not text:
                continue
            msg = make(line, record, r, text, payload.get("channel"))
            counts[r] += 1
            canonical[r].append(msg)
            if r == "assistant":
                latest_assistant = msg
                if msg["channel"] == "final":
                    finals.append(msg)
                    final_count += 1
                    latest_final_line = line
        elif kind == "event_msg":
            event = payload.get("type")
            # Older CLI transcripts use these events; response_item is preferred
            # per role when present, avoiding the usual mirrored log duplicates.
            if event in ("user_message", "agent_message"):
                r = "user" if event == "user_message" else "assistant"
                text = payload.get("message", "")
                if not isinstance(text, str):
                    continue
                text = user_text(text) if r == "user" else text.strip()
                if text:
                    msg = make(line, record, r, text)
                    legacy[r].append(msg)
                    legacy_counts[r] += 1
                    if r == "assistant" and counts[r] == 0:
                        latest_assistant = msg
            elif event == "task_complete":
                text = payload.get("last_agent_message")
                if isinstance(text, str) and text.strip():
                    if latest_assistant and latest_assistant["text"] == text.strip():
                        msg = dict(latest_assistant, channel="final")
                    else:
                        msg = make(line, record, "assistant", text.strip(), "final")
                    if msg["line"] != latest_final_line:
                        finals.append(msg)
                        final_count += 1
                        latest_final_line = msg["line"]

    if source is None:
        raise ValueError("No supported records found. Expected Codex or Claude Code JSONL.")
    available = {}
    selected = []
    for r in canonical:
        messages = canonical[r] if counts[r] else legacy[r]
        available[r] = counts[r] or legacy_counts[r]
        if r == "assistant" and final_only:
            messages = finals
            available[r] = final_count
        n = ((last_user or 0) if r == "user" else (last_assistant or 0)) if per_role else last
        if (per_role or role in ("both", r)) and n:
            selected.extend(list(messages)[-n:])
    selected.sort(key=lambda msg: msg["line"])
    if not per_role:
        selected = selected[-last:]
    return {"path": str(path), "source": source, "session_id": session_id,
            "available": available, "count": len(selected),
            "oldest_returned_line": selected[0]["line"] if selected else None,
            "messages": selected}


def positive(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="Codex or Claude Code transcript path from search_chats.py")
    parser.add_argument("--last", "-n", type=positive, help="Last N matching messages total (default 20)")
    parser.add_argument("--role", choices=("user", "assistant", "both"), default="both")
    parser.add_argument("--last-user", type=positive, help="Separate count of user messages")
    parser.add_argument("--last-assistant", type=positive, help="Separate count of assistant messages")
    parser.add_argument("--final-only", action="store_true",
                        help="Require Codex final/task_complete or Claude end_turn evidence")
    parser.add_argument("--before-line", type=positive, help="Read only records before this line (pagination)")
    parser.add_argument("--json", action="store_true", help="Structured output including full message text")
    args = parser.parse_args()
    per_role = args.last_user is not None or args.last_assistant is not None
    if per_role and (args.last is not None or args.role != "both"):
        parser.error("Use either --last/--role or --last-user/--last-assistant.")
    try:
        result = read_chat(args.path.expanduser().resolve(), last=args.last or 20,
                           role=args.role, last_user=args.last_user,
                           last_assistant=args.last_assistant, final_only=args.final_only,
                           before_line=args.before_line)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Transcript: {result['path']}\nSource: {result['source']}\nMessages returned: {result['count']}")
        for msg in result["messages"]:
            channel = f" ({msg['channel']})" if msg['channel'] else ""
            print(f"\n--- {msg['role']}{channel} | {msg['timestamp']} | L{msg['line']} ---\n{msg['text']}")
        if not result["messages"]:
            print("No matching messages; final-only requires explicit completion evidence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
