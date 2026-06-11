---
name: session-browser
description: "Launch a local web UI to browse, full-text search, cost-analyze, and trace provenance across past AI coding session history (Claude Code, Codex CLI, Gemini CLI). Use when the user wants to find/revisit/search old Claude/Codex/Gemini conversations, see how much they spent on tokens, or find which sessions touched a file or ran a command. Trigger phrases: browse my sessions, search my session history, find that conversation where I, open my chat history, session browser, where did I discuss X with Claude/Codex/Gemini, how much did I spend on Claude/tokens, my AI cost dashboard, which sessions edited <file>, every time I ran <command>, look through my past sessions, my old AI chats."
---

# AI Session Browser

A zero-dependency local tool that indexes every Claude Code, Codex CLI, and Gemini CLI session
transcript into a SQLite FTS5 full-text index and serves a single-page web UI to browse and search
them.

## When to use

Use this when the user wants to look back through their AI coding history — e.g. "find the session
where I set up the BigQuery query", "search my Claude history for the refund report", "browse my
old Codex sessions", or "open my session browser".

## How to run

Start the server (background so it keeps serving):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/server.py"
```

Then give the user the URL it prints (default `http://localhost:8765/`). It auto-opens the browser.

Flags: `--port N`, `--reindex` (force full rebuild), `--no-open`. Env: `SESSION_BROWSER_PORT`,
`SESSION_BROWSER_DB`.

## What it indexes

| Tool   | Location                                          | Format            |
|--------|---------------------------------------------------|-------------------|
| Claude | `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`   | JSONL per record  |
| Codex  | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`    | JSONL per record  |
| Gemini | `~/.gemini/tmp/<name|hash>/chats/session-*.{json,jsonl}` | JSON / JSONL |

Each transcript is normalized to `{role, ts, text}` messages. User + assistant turns are indexed;
tool calls and results are kept inline as markers (`[tool: ...]`, `[tool_result] ...`) so they're
searchable but visually distinct. Pure auth/info/error noise and command scaffolding are skipped.

## How it works

- **Indexing** walks each tool's directory and inserts messages into SQLite. It is *incremental*:
  a file is re-parsed only when its `mtime` changes, and sessions whose files were deleted are
  purged. The index DB lives at `~/.cache/ai-session-browser/index.db` (never in the repo).
- **Search** uses FTS5 with `bm25()` ranking and `snippet()` highlighting. Bare terms become a
  prefix-AND query; advanced users can pass FTS operators (`"exact phrase"`, `term*`, `a OR b`,
  `NEAR(...)`).
- **UI** is a master/detail SPA: recency-ordered session list with tool filters on the left, full
  transcript on the right, a global search box, and a theme/accent/font tweaks panel persisted via
  `localStorage`.

## API (for scripting / debugging)

- `GET /api/stats` — totals and per-tool counts.
- `GET /api/sessions?tool=&project=&model=&branch=&from=&to=&limit=` — filtered session list (includes `cost_usd`, `git_branch`).
- `GET /api/session?id=` — a full transcript plus `tool_events` (files/commands) and per-model `usage`.
- `GET /api/search?q=&tool=&limit=` — full-text search, grouped by session with snippets.
- `GET /api/projects` — projects with counts/last-activity (powers the sidebar).
- `GET /api/analytics?from=&to=&tool=&project=` — totals, `by_day`, `by_project`, `by_tool`, `by_model`, `heatmap`, `cache_savings_usd`, `unpriced`.
- `GET /api/provenance?q=&kind=file|command&tool=` — sessions that touched a path or ran a command, with resume links.
- `GET /api/history?q=&limit=` — flat reverse-chron feed of Codex prompts (`~/.codex/history.jsonl`).
- `GET /api/reindex` — incremental re-index; `?reprice=1` recomputes costs from `prices.json` without re-parsing.

## Pricing

Costs come from `DEFAULT_PRICES` in `server.py` (USD per MTok: input/output/cache_read/cache_write).
Override or add models by writing `~/.cache/ai-session-browser/prices.json` (merged over defaults),
then `GET /api/reindex?reprice=1`. Unknown models cost $0 and are surfaced in the Analytics
`unpriced` list. The OpenAI/Codex defaults are estimates — verify against the current price sheet.

## Extending to a new tool

Add a `parse_<tool>(path)` function returning `(meta, messages)` and register it in `discover()`.
The normalized message shape keeps the UI and search untouched.
