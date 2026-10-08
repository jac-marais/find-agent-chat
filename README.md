# find-agent-chat

An agent skill that finds local Claude Code, Codex, and Cursor chats and reads their recent messages.

https://github.com/user-attachments/assets/3f5175a5-6e0c-4bbe-8fe9-851ba423062f

It has two commands:

- `search_chats.py` finds a session by its title, agent name, or topic. It uses a local SQLite FTS5 index at `~/.cache/find-agent-chat/index.sqlite`.
- `read_chat.py` reads a Codex or Claude Code transcript and returns the last N user and assistant messages, without tool noise or image bytes.

[`skill/SKILL.md`](skill/SKILL.md) is the full reference for agents.

## Layout

```
LICENSE
README.md
skill/
  SKILL.md                 # skill entry point (name, description, usage)
  scripts/
    search_chats.py        # index + name lookup + full-text search
    read_chat.py           # transcript reader
    test_search_chats.py
    test_read_chat.py
```

## Install

Copy `skill/` into your agent's skills directory as `find-agent-chat`. For Claude Code:

```bash
cp -R skill ~/.claude/skills/find-agent-chat
```

It needs Python 3 and nothing else. The scripts use only the standard library. Cursor title lookup reads Cursor's macOS settings database; the other features have no OS-specific paths.

## Quick use

Run these from the installed skill directory:

```bash
python3 scripts/search_chats.py name "Some chat title"
python3 scripts/search_chats.py index
python3 scripts/search_chats.py helm deploy staging --days 14
python3 scripts/read_chat.py /path/to/session.jsonl --last 20
```

## Test

```bash
cd skill/scripts && python3 -m unittest test_search_chats test_read_chat
```

## License

MIT. See [`LICENSE`](LICENSE).
