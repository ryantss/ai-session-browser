# session-browser

Local web UI to **browse, full-text search, cost-analyze, and trace** your AI coding session
history across **Claude Code**, **Codex CLI**, **Gemini CLI**, **Pi**, **Hermes**, **OpenCode**,
and **LM Studio** — in one place.

Zero third-party dependencies: Python 3 stdlib only (`sqlite3` + `http.server`). Works offline
(the Markdown renderer is hand-rolled — no CDN).

Three views:
- **Browse** — project sidebar, full-text search, transcripts with rendered Markdown + collapsible
  tool blocks, per-session cost badges, keyboard nav (`j`/`k`/`/`), date filters.
- **Analytics** — token/cost dashboard ($ per day / model / project / tool), cache savings, and a
  day×hour activity heatmap. Built from usage data already in the logs.
- **Provenance** — "which sessions touched `ingester.py`", "every `git push` I ran", with
  `claude --resume` / `codex resume` links and `cursor://` / `vscode://` jump-to-file. Plus a flat
  feed of your Codex prompt history.

## Quick start

```bash
git clone https://github.com/ryan-wego/ai-session-browser
cd ai-session-browser
python3 server.py
```

Opens `http://localhost:8765/` automatically. No install, no dependencies — Python 3.9+ is all you need.

### As a Claude Code plugin

This repo is also a Claude Code plugin marketplace, so you can install it and get the
`/browse-sessions` command:

```bash
claude plugin marketplace add /path/to/ai-session-browser   # or the GitHub URL
claude plugin install session-browser
```

### Optional: install as a CLI

```bash
pipx install .        # then run:  ai-session-browser
```

## Tests

```bash
python3 -m unittest discover tests
```

Stdlib `unittest` (no test deps). Covers the pricing math, the date-suffix model normalization, the
provenance/patch parsing, and — most importantly — the index "no-leak on re-parse" invariant.

## What it does

- **Indexes** every session transcript from:
  - Claude Code — `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`
  - Codex CLI — `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
  - Gemini CLI — `~/.gemini/tmp/<name|hash>/chats/session-*.{json,jsonl}`
  - Pi — `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl` (carries its own per-message USD cost)
  - Hermes — `~/.hermes/sessions/*.jsonl`
  - OpenCode — `~/.local/share/opencode/opencode.db` (one SQLite DB, many sessions)
  - LM Studio — `~/.lmstudio/conversations/*.conversation.json`
  - Paperclip-wrapped Codex — `~/.paperclip/instances/*/codex-home/sessions/**/rollout-*.jsonl`
- **Normalizes** each tool's distinct schema into a common `{role, ts, text}` message shape. Tool
  calls/results are inlined as searchable markers; auth/info/error noise is skipped.
- **Extracts** token usage (per model) and tool events (files touched, commands run) — Claude
  `message.usage`, Codex `token_count`, and `tool_use` / `exec_command` / `apply_patch` payloads.
- **Stores** everything in a SQLite **FTS5** index at `~/.cache/ai-session-browser/index.db`
  (`sessions` + `messages` + `usage` + `tool_events`, with `fts` and `tool_fts` virtual tables).
- **Prices** usage from an embedded model→cost table, overridable via
  `~/.cache/ai-session-browser/prices.json`; unknown models surface as "unpriced" and re-pricing is a
  cheap `GET /api/reindex?reprice=1` (no re-parse).
- **Serves** a single-page app with the three views above.

## Incremental by design

Re-running only re-parses files whose `mtime` changed and purges sessions whose files were deleted,
so day-to-day startups are sub-second even with thousands of sessions.

## Flags & env

| Flag / env | Effect |
|---|---|
| `--port N` / `SESSION_BROWSER_PORT` | Serve on a different port (default `8765`). |
| `--reindex` | Force a full rebuild (use if a tool changes its on-disk format). |
| `--no-open` | Don't auto-open the browser. |
| `SESSION_BROWSER_DB` | Override the index DB path. |

## Search syntax

Bare words are treated as a prefix-AND search (`refund bigquery` → sessions containing both). FTS5
operators pass through: `"exact phrase"`, `term*`, `a OR b`, `NEAR(a b, 5)`.

## HTTP API

`GET /api/stats` · `GET /api/sessions` (filters: `tool`, `project`, `model`, `branch`, `from`, `to`) ·
`GET /api/session?id=` (returns transcript + `tool_events` + `usage`) · `GET /api/search?q=` ·
`GET /api/projects` · `GET /api/analytics` · `GET /api/provenance?q=&kind=file|command` ·
`GET /api/history?q=` · `GET /api/reindex[?reprice=1]`. See the skill doc for response shapes.

## Privacy

Everything runs locally and binds to `127.0.0.1`. No data leaves the machine. The index DB lives
under `~/.cache`, never inside this repo.

## Adding another tool

For a **file-per-session** tool, add a row to `_FILE_SOURCES` in `server.py` (tool name, root,
glob mode, pattern, parser) and write `parse_<tool>(path) -> (meta, messages, tool_events, usage)`.
`discover()` yields `(tool, key, parser, mtime)`; for file sources `mtime` is `None` (the file is
stat-ed). The normalized message shape means the UI and search need no changes.

For a **multi-session store** (e.g. a single SQLite DB like OpenCode), enumerate sessions in
`discover()` and yield a virtual `key="<db>#<session_id>"` plus that session's own `mtime`, with a
parser closure bound to the id — so each session re-indexes incrementally through the same path.

If an agent reports its own spend, set `cost_usd` on its `usage` rows: the indexer stores it with
`cost_source="agent"`, honors it over the embedded price table, and preserves it across
`GET /api/reindex?reprice=1`. Model ids are normalized (provider prefixes like `openai/`, bedrock
ids, and `:free` suffixes) before price lookup.
