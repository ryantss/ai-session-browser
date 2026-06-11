---
description: Launch the local AI Session Browser web UI to browse & search Claude / Codex / Gemini history.
---

# Browse AI Sessions

Launch the local **AI Session Browser** — a web UI that indexes and full-text searches the user's
Claude Code, Codex CLI, and Gemini CLI session transcripts.

## What to do

1. Start the server **in the background** so it keeps running while the session continues:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/server.py"
   ```

   - It indexes `~/.claude/projects`, `~/.codex/sessions`, and `~/.gemini/tmp/*/chats` into a
     SQLite FTS5 index at `~/.cache/ai-session-browser/index.db`.
   - First run parses every file (~10-30s for a couple thousand sessions). Subsequent runs are
     incremental (only changed files re-parse) and start in well under a second.
   - It auto-opens `http://localhost:8765/` in the default browser.

2. Report the URL to the user and the index summary the script prints (counts per tool).

3. Tell the user how to stop it (Ctrl+C, or kill the background job) and that re-running picks up
   new sessions automatically.

## Useful flags

- `--port 9000` — serve on a different port (or set `SESSION_BROWSER_PORT`).
- `--reindex` — force a full rebuild (use if a tool changed its on-disk format).
- `--no-open` — don't auto-open the browser.

## Notes

- Zero third-party dependencies — Python 3 stdlib only (`sqlite3` + `http.server`).
- The index DB lives under `~/.cache`, never inside the repo.
- If the user only wants a fresh re-index without serving, the running server also exposes
  `GET /api/reindex`.
