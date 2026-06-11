#!/usr/bin/env python3
"""AI Session Browser — local web UI to browse & search Claude / Codex / Gemini history.

Zero third-party dependencies (Python stdlib only: sqlite3 + http.server).

Indexes session transcripts from:
  - Claude Code : ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl
  - Codex CLI   : ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
  - Gemini CLI  : ~/.gemini/tmp/<name|hash>/chats/session-*.{json,jsonl}

Into a SQLite DB with an FTS5 full-text index, then serves a single-page app.
Re-runs are incremental: only files whose mtime changed are re-parsed.

Usage:
    python3 server.py                 # index then serve on http://localhost:8765
    python3 server.py --port 9000
    python3 server.py --reindex       # force full rebuild
    python3 server.py --no-open       # don't auto-open the browser
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HOME = Path.home()
APP_DIR = Path(__file__).resolve().parent


def _find_app_html() -> Path:
    """Locate app.html next to this module (source/editable install) or in the
    installed data dir (regular pip install copies it under share/)."""
    candidates = [
        APP_DIR / "app.html",
        Path(sys.prefix) / "share" / "ai-session-browser" / "app.html",
        Path(os.environ.get("SESSION_BROWSER_APP", "")),
    ]
    for c in candidates:
        if c and c.is_file():
            return c
    return APP_DIR / "app.html"  # default; will 404 with a clear error if missing


APP_HTML = _find_app_html()
DB_PATH = Path(os.environ.get("SESSION_BROWSER_DB", HOME / ".cache" / "ai-session-browser" / "index.db"))

# Cap per-message stored text so a single giant tool dump can't bloat the DB.
MAX_MSG_CHARS = 20_000

PRICES_PATH = Path(os.environ.get(
    "SESSION_BROWSER_PRICES", HOME / ".cache" / "ai-session-browser" / "prices.json"))

# USD per MILLION tokens. cache_read ~= 0.1x input, cache_write(5m) ~= 1.25x input.
# Claude prices are current; OpenAI/Codex values are best-effort estimates —
# correct them by editing ~/.cache/ai-session-browser/prices.json (merged over these).
DEFAULT_PRICES = {
    "claude-fable-5":    {"input": 10.0, "output": 50.0, "cache_read": 1.0,  "cache_write": 12.5},
    "claude-opus-4-8":   {"input": 5.0,  "output": 25.0, "cache_read": 0.5,  "cache_write": 6.25},
    "claude-opus-4-7":   {"input": 5.0,  "output": 25.0, "cache_read": 0.5,  "cache_write": 6.25},
    "claude-opus-4-6":   {"input": 5.0,  "output": 25.0, "cache_read": 0.5,  "cache_write": 6.25},
    "claude-opus-4-5":   {"input": 5.0,  "output": 25.0, "cache_read": 0.5,  "cache_write": 6.25},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0, "cache_read": 0.3,  "cache_write": 3.75},
    "claude-sonnet-4-5": {"input": 3.0,  "output": 15.0, "cache_read": 0.3,  "cache_write": 3.75},
    "claude-haiku-4-5":  {"input": 1.0,  "output": 5.0,  "cache_read": 0.1,  "cache_write": 1.25},
    # OpenAI / Codex — ESTIMATES, verify & override via prices.json:
    "gpt-5.3-codex":     {"input": 1.25, "output": 10.0, "cache_read": 0.125, "cache_write": 0.0},
    "gpt-5.4-codex":     {"input": 1.25, "output": 10.0, "cache_read": 0.125, "cache_write": 0.0},
    "gpt-5.4":           {"input": 1.25, "output": 10.0, "cache_read": 0.125, "cache_write": 0.0},
    "gpt-5.4-mini":      {"input": 0.25, "output": 2.0,  "cache_read": 0.025, "cache_write": 0.0},
    "gpt-5.5":           {"input": 1.25, "output": 10.0, "cache_read": 0.125, "cache_write": 0.0},
}

_PRICES = dict(DEFAULT_PRICES)


def load_prices():
    """Merge an optional user prices.json over the embedded defaults."""
    global _PRICES
    merged = {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    try:
        if PRICES_PATH.exists():
            override = json.loads(PRICES_PATH.read_text(encoding="utf-8"))
            for model, p in (override or {}).items():
                if isinstance(p, dict):
                    merged.setdefault(model, {}).update(p)
    except Exception:
        pass
    _PRICES = merged
    return _PRICES


_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def price_for(model: str):
    """Look up a model's price, tolerating a trailing -YYYYMMDD snapshot suffix."""
    if not model:
        return None
    p = _PRICES.get(model)
    if p is None:
        p = _PRICES.get(_DATE_SUFFIX_RE.sub("", model))
    return p


def cost_for(model: str, in_tok: int, out_tok: int,
             cache_read: int = 0, cache_write: int = 0) -> float:
    """USD cost for one usage row. Unknown model -> 0.0 (surfaced as 'unpriced')."""
    p = price_for(model)
    if not p:
        return 0.0
    return (in_tok * p.get("input", 0) + out_tok * p.get("output", 0)
            + cache_read * p.get("cache_read", 0)
            + cache_write * p.get("cache_write", 0)) / 1_000_000


# ---------------------------------------------------------------------------
# Tool-event extraction helpers (provenance) — shared across all 3 parsers.
# ---------------------------------------------------------------------------

_FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit", "MultiEdit"}
_APPLY_PATCH_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)


def _norm_path(raw: str, base: str = "") -> str:
    if not raw:
        return ""
    raw = str(raw)
    p = Path(raw).expanduser()
    if not p.is_absolute() and base:
        p = Path(base).expanduser() / raw
    try:
        return str(p)
    except Exception:
        return raw


def _te_file(idx, ts, name, raw_path, base=""):
    path = _norm_path(raw_path, base)
    if not path:
        return None
    return {"idx": idx, "ts": ts, "kind": "file", "name": name, "path": path, "command": None}


def _te_cmd(idx, ts, name, command):
    command = (command or "").strip()
    if not command:
        return None
    return {"idx": idx, "ts": ts, "kind": "command", "name": name, "path": None, "command": command}


def _parse_apply_patch(text: str):
    """Return file paths touched by a Codex apply_patch payload."""
    if not isinstance(text, str):
        return []
    return [m.strip() for m in _APPLY_PATCH_RE.findall(text) if m.strip()]


# ---------------------------------------------------------------------------
# Text extraction helpers — normalize every tool's content into plain text.
# ---------------------------------------------------------------------------

NOISE_PREFIXES = (
    "<local-command-caveat>", "<command-name>", "<command-message>",
    "<command-args>", "<local-command-stdout>", "<bridge", "Caveat:",
    "<system-reminder>", "<user-prompt-submit-hook>",
)


def _clip(s: str) -> str:
    if s and len(s) > MAX_MSG_CHARS:
        return s[:MAX_MSG_CHARS] + f"\n…[truncated {len(s) - MAX_MSG_CHARS} chars]"
    return s


def claude_text(content) -> str:
    """Claude content is a string OR a list of typed blocks."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "thinking":
            parts.append("[thinking] " + b.get("thinking", ""))
        elif t == "tool_use":
            inp = json.dumps(b.get("input", {}), ensure_ascii=False)
            parts.append(f"[tool: {b.get('name', '?')}] {inp[:800]}")
        elif t == "tool_result":
            c = b.get("content")
            if isinstance(c, str):
                parts.append("[tool_result] " + c)
            elif isinstance(c, list):
                for x in c:
                    if isinstance(x, dict) and x.get("type") == "text":
                        parts.append("[tool_result] " + x.get("text", ""))
    return "\n".join(p for p in parts if p)


def codex_text(content) -> str:
    """Codex content is a list of {type: input_text|output_text|text, text}."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text"):
            parts.append(b.get("text", ""))
    return "\n".join(p for p in parts if p)


def is_noise(text: str) -> bool:
    t = (text or "").lstrip()
    return any(t.startswith(p) for p in NOISE_PREFIXES)


def derive_title(messages) -> str:
    """First substantive user message becomes the session title."""
    for m in messages:
        if m["role"] == "user":
            txt = (m["text"] or "").strip()
            if txt and not is_noise(txt):
                first_line = txt.splitlines()[0].strip()
                return first_line[:120] if first_line else txt[:120]
    return "(no user prompt)"


def norm_ts(ts) -> str:
    """Return an ISO-8601 string for any timestamp shape we encounter."""
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts / (1000 if ts > 1e12 else 1), timezone.utc).isoformat()
        except Exception:
            return ""
    return str(ts)


# ---------------------------------------------------------------------------
# Per-tool parsers. Each returns (meta: dict, messages: list[{role,ts,text}]).
# Returns None if the file has no usable conversation.
# ---------------------------------------------------------------------------

def parse_claude(path: Path):
    cwd = model = sid = start = end = git_branch = ""
    messages = []
    tool_events = []
    usage = {}  # model -> token accumulator
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue
                cwd = o.get("cwd") or cwd
                sid = o.get("sessionId") or sid
                git_branch = o.get("gitBranch") or git_branch
                t = o.get("type")
                if t in ("user", "assistant"):
                    msg = o.get("message") or {}
                    msg_model = msg.get("model") or ""
                    if msg_model and msg_model != "<synthetic>":
                        model = msg_model
                    text = _clip(claude_text(msg.get("content")))
                    ts = norm_ts(o.get("timestamp"))
                    if not text.strip():
                        continue
                    if not start:
                        start = ts
                    end = ts or end
                    midx = len(messages)
                    messages.append({"role": t, "ts": ts, "text": text})
                    # token usage (assistant only), keyed by the message's own model
                    u = msg.get("usage") if t == "assistant" else None
                    if isinstance(u, dict) and msg_model and msg_model != "<synthetic>":
                        acc = usage.setdefault(msg_model, _zero_usage())
                        acc["in_tokens"] += u.get("input_tokens", 0) or 0
                        acc["out_tokens"] += u.get("output_tokens", 0) or 0
                        acc["cache_read_tokens"] += u.get("cache_read_input_tokens", 0) or 0
                        acc["cache_creation_tokens"] += u.get("cache_creation_input_tokens", 0) or 0
                    # provenance: file/command events from assistant tool_use blocks
                    content = msg.get("content")
                    if isinstance(content, list):
                        for b in content:
                            if not isinstance(b, dict) or b.get("type") != "tool_use":
                                continue
                            name = b.get("name", "")
                            inp = b.get("input") or {}
                            if name in _FILE_TOOLS:
                                fp = inp.get("file_path") or inp.get("notebook_path")
                                ev = _te_file(midx, ts, name, fp, cwd)
                                if ev:
                                    tool_events.append(ev)
                            elif name == "Bash":
                                ev = _te_cmd(midx, ts, name, inp.get("command"))
                                if ev:
                                    tool_events.append(ev)
    except Exception:
        return None
    if not messages:
        return None
    project = Path(cwd).name if cwd else _decode_claude_dir(path.parent.name)
    meta = {"tool": "claude", "sid": sid or path.stem, "cwd": cwd,
            "project": project, "started": start, "ended": end,
            "model": model, "git_branch": git_branch}
    return meta, messages, tool_events, _usage_list(usage)


def _zero_usage():
    return {"in_tokens": 0, "out_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}


def _usage_list(usage_by_model: dict):
    """Convert {model: acc} into a list of usage rows, dropping all-zero rows."""
    out = []
    for model, acc in usage_by_model.items():
        if any(acc.values()):
            out.append({"model": model, **acc})
    return out


def _decode_claude_dir(name: str) -> str:
    # Claude encodes cwd as the path with '/' -> '-'. Best-effort last segment.
    return name.rsplit("-", 1)[-1] if name else ""


def parse_codex(path: Path):
    cwd = model = sid = start = end = ""
    messages = []
    tool_events = []
    last_total = None  # last non-null cumulative token_count
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue
                t = o.get("type")
                p = o.get("payload") or {}
                ts = norm_ts(o.get("timestamp"))
                if t == "session_meta":
                    sid = p.get("id") or sid
                    cwd = p.get("cwd") or cwd
                    start = norm_ts(p.get("timestamp")) or start
                elif t == "turn_context":
                    model = p.get("model") or model
                elif t == "event_msg" and p.get("type") == "token_count":
                    info = p.get("info")
                    if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                        last_total = info["total_token_usage"]  # cumulative; keep last
                elif t == "response_item" and p.get("type") == "message":
                    role = p.get("role")
                    if role not in ("user", "assistant"):
                        continue  # skip developer/system scaffolding
                    text = _clip(codex_text(p.get("content")))
                    if not text.strip() or is_noise(text):
                        continue
                    if not start:
                        start = ts
                    end = ts or end
                    messages.append({"role": role, "ts": ts, "text": text})
                elif t == "response_item" and p.get("type") == "function_call":
                    if p.get("name") == "exec_command":
                        try:
                            args = json.loads(p.get("arguments") or "{}")
                        except Exception:
                            args = {}
                        ev = _te_cmd(len(messages), ts, "exec_command", args.get("cmd"))
                        if ev:
                            tool_events.append(ev)
                elif t == "response_item" and p.get("type") == "custom_tool_call":
                    if p.get("name") == "apply_patch":
                        for fp in _parse_apply_patch(p.get("input") or ""):
                            ev = _te_file(len(messages), ts, "apply_patch", fp, cwd)
                            if ev:
                                tool_events.append(ev)
    except Exception:
        return None
    if not messages:
        return None
    project = Path(cwd).name if cwd else ""
    usage = {}
    if last_total:
        acc = usage.setdefault(model or "gpt-5-codex", _zero_usage())
        acc["in_tokens"] = last_total.get("input_tokens", 0) or 0
        acc["out_tokens"] = last_total.get("output_tokens", 0) or 0
        acc["cache_read_tokens"] = last_total.get("cached_input_tokens", 0) or 0
        # Codex counts cached_input_tokens inside input_tokens — subtract to avoid
        # double-charging cache reads at the full input rate.
        acc["in_tokens"] = max(0, acc["in_tokens"] - acc["cache_read_tokens"])
    meta = {"tool": "codex", "sid": sid or path.stem, "cwd": cwd,
            "project": project, "started": start, "ended": end,
            "model": model, "git_branch": ""}
    return meta, messages, tool_events, _usage_list(usage)


def _gemini_project_root(path: Path) -> str:
    # chats/<file> -> parent.parent is the project dir holding .project_root
    proj_dir = path.parent.parent
    root_file = proj_dir / ".project_root"
    if root_file.exists():
        try:
            return root_file.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            pass
    return proj_dir.name


def parse_gemini(path: Path):
    sid = start = end = ""
    messages = []
    records = []
    try:
        if path.suffix == ".jsonl":
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        continue
            # first record may be session metadata
            if records and isinstance(records[0], dict) and records[0].get("sessionId"):
                meta0 = records[0]
                sid = meta0.get("sessionId", "")
                start = norm_ts(meta0.get("startTime"))
                end = norm_ts(meta0.get("lastUpdated"))
                records = records[1:]
        else:
            obj = json.load(open(path, "r", encoding="utf-8", errors="replace"))
            sid = obj.get("sessionId", "")
            start = norm_ts(obj.get("startTime"))
            end = norm_ts(obj.get("lastUpdated"))
            records = obj.get("messages", [])
    except Exception:
        return None

    tool_events = []
    for r in records:
        if not isinstance(r, dict):
            continue
        if "$set" in r:  # JSONL update record (e.g. lastUpdated bumps)
            su = r.get("$set") or {}
            if su.get("lastUpdated"):
                end = norm_ts(su["lastUpdated"]) or end
            continue
        t = r.get("type")
        if t not in ("user", "gemini"):
            continue  # skip info/error/auth noise
        content = r.get("content") or ""
        tool_calls = r.get("toolCalls") or []
        text = content if isinstance(content, str) else codex_text(content)
        if tool_calls and not text.strip():
            names = ", ".join(tc.get("name", "?") for tc in tool_calls if isinstance(tc, dict))
            text = f"[tool: {names}]"
        text = _clip(text)
        if not text.strip():
            continue
        role = "assistant" if t == "gemini" else "user"
        ts = norm_ts(r.get("timestamp"))
        if not start:
            start = ts
        end = ts or end
        midx = len(messages)
        messages.append({"role": role, "ts": ts, "text": text})
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name", "?")
            args = tc.get("args") or {}
            fp = args.get("file_path") or args.get("path") or args.get("absolute_path")
            cmd = args.get("command") or args.get("cmd")
            if fp:
                ev = _te_file(midx, ts, name, fp)
                if ev:
                    tool_events.append(ev)
            elif cmd:
                ev = _te_cmd(midx, ts, name, cmd)
                if ev:
                    tool_events.append(ev)

    if not messages:
        return None
    meta = {"tool": "gemini", "sid": sid or path.stem,
            "cwd": _gemini_project_root(path),
            "project": Path(_gemini_project_root(path)).name or _gemini_project_root(path),
            "started": start, "ended": end, "model": "", "git_branch": ""}
    return meta, messages, tool_events, []


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover():
    """Yield (tool, path, parser) for every candidate session file."""
    claude_root = HOME / ".claude" / "projects"
    if claude_root.is_dir():
        for p in claude_root.rglob("*.jsonl"):
            yield ("claude", p, parse_claude)

    codex_root = HOME / ".codex" / "sessions"
    if codex_root.is_dir():
        for p in codex_root.rglob("rollout-*.jsonl"):
            yield ("codex", p, parse_codex)

    gemini_root = HOME / ".gemini" / "tmp"
    if gemini_root.is_dir():
        for p in gemini_root.glob("*/chats/*"):
            if p.suffix in (".json", ".jsonl"):
                yield ("gemini", p, parse_gemini)


# ---------------------------------------------------------------------------
# SQLite index
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    tool TEXT, sid TEXT, path TEXT UNIQUE, project TEXT, cwd TEXT,
    started TEXT, ended TEXT, mtime REAL, msg_count INTEGER, title TEXT, model TEXT,
    git_branch TEXT,
    in_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_creation_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_tool   ON sessions(tool);
CREATE INDEX IF NOT EXISTS idx_sessions_ended  ON sessions(ended);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id INTEGER, idx INTEGER, role TEXT, ts TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    session_id INTEGER, model TEXT,
    in_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_creation_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_model   ON usage(model);
CREATE TABLE IF NOT EXISTS tool_events (
    id INTEGER PRIMARY KEY,
    session_id INTEGER, idx INTEGER, ts TEXT,
    kind TEXT, name TEXT, path TEXT, command TEXT
);
CREATE INDEX IF NOT EXISTS idx_te_session ON tool_events(session_id);
CREATE INDEX IF NOT EXISTS idx_te_kind    ON tool_events(kind);
CREATE INDEX IF NOT EXISTS idx_te_path    ON tool_events(path);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    text, session_id UNINDEXED, idx UNINDEXED, role UNINDEXED, tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS tool_fts USING fts5(
    path, command, session_id UNINDEXED, te_id UNINDEXED, tokenize='unicode61'
);
"""

# Columns added to `sessions` after v0.1 — ALTERed in on existing DBs.
_SESSION_MIGRATIONS = {
    "git_branch": "TEXT", "in_tokens": "INTEGER DEFAULT 0",
    "out_tokens": "INTEGER DEFAULT 0", "cache_read_tokens": "INTEGER DEFAULT 0",
    "cache_creation_tokens": "INTEGER DEFAULT 0", "cost_usd": "REAL DEFAULT 0",
}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Idempotent column migration for DBs created before v0.2.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    for col, decl in _SESSION_MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


def _delete_session_rows(conn, session_id):
    conn.execute("DELETE FROM tool_fts WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM tool_events WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM usage WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM fts WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def index(conn, force=False, progress=lambda *_: None):
    existing = {row["path"]: (row["id"], row["mtime"])
                for row in conn.execute("SELECT id, path, mtime FROM sessions")}
    seen_paths = set()
    n_new = n_upd = n_skip = n_total = 0
    by_tool = {"claude": 0, "codex": 0, "gemini": 0}

    for tool, path, parser in discover():
        n_total += 1
        spath = str(path)
        seen_paths.add(spath)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        prev = existing.get(spath)
        if prev and not force and abs(prev[1] - mtime) < 1e-6:
            n_skip += 1
            continue

        parsed = parser(path)
        if parsed is None:
            if prev:
                _delete_session_rows(conn, prev[0])
            continue
        meta = parsed[0]
        messages = parsed[1]
        tool_events = parsed[2] if len(parsed) > 2 else []
        usage = parsed[3] if len(parsed) > 3 else []

        if prev:
            _delete_session_rows(conn, prev[0])
            n_upd += 1
        else:
            n_new += 1
        by_tool[tool] = by_tool.get(tool, 0) + 1

        # Roll up per-model usage into session totals + cost.
        s_in = s_out = s_cr = s_cc = 0
        s_cost = 0.0
        for u in usage:
            c = cost_for(u["model"], u["in_tokens"], u["out_tokens"],
                         u["cache_read_tokens"], u["cache_creation_tokens"])
            u["cost_usd"] = c
            s_in += u["in_tokens"]; s_out += u["out_tokens"]
            s_cr += u["cache_read_tokens"]; s_cc += u["cache_creation_tokens"]
            s_cost += c

        cur = conn.execute(
            "INSERT INTO sessions(tool,sid,path,project,cwd,started,ended,mtime,msg_count,title,"
            "model,git_branch,in_tokens,out_tokens,cache_read_tokens,cache_creation_tokens,cost_usd)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (meta["tool"], meta["sid"], spath, meta["project"], meta["cwd"],
             meta["started"], meta["ended"], mtime, len(messages),
             derive_title(messages), meta["model"], meta.get("git_branch", ""),
             s_in, s_out, s_cr, s_cc, s_cost))
        sess_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO messages(session_id,idx,role,ts,text) VALUES(?,?,?,?,?)",
            [(sess_id, i, m["role"], m["ts"], m["text"]) for i, m in enumerate(messages)])
        conn.executemany(
            "INSERT INTO fts(text,session_id,idx,role) VALUES(?,?,?,?)",
            [(m["text"], sess_id, i, m["role"]) for i, m in enumerate(messages)])
        if usage:
            conn.executemany(
                "INSERT INTO usage(session_id,model,in_tokens,out_tokens,cache_read_tokens,"
                "cache_creation_tokens,cost_usd) VALUES(?,?,?,?,?,?,?)",
                [(sess_id, u["model"], u["in_tokens"], u["out_tokens"], u["cache_read_tokens"],
                  u["cache_creation_tokens"], u["cost_usd"]) for u in usage])
        if tool_events:
            te_rows = [(sess_id, e["idx"], e["ts"], e["kind"], e["name"], e["path"], e["command"])
                       for e in tool_events]
            conn.executemany(
                "INSERT INTO tool_events(session_id,idx,ts,kind,name,path,command)"
                " VALUES(?,?,?,?,?,?,?)", te_rows)
            conn.executemany(
                "INSERT INTO tool_fts(path,command,session_id,te_id) VALUES(?,?,?,?)",
                [(e["path"] or "", e["command"] or "", sess_id, 0) for e in tool_events])

        if n_total % 200 == 0:
            progress(n_total, n_new, n_upd, n_skip)
            conn.commit()

    # Purge sessions whose files disappeared.
    for spath, (sid, _) in existing.items():
        if spath not in seen_paths:
            _delete_session_rows(conn, sid)

    conn.commit()
    return {"total": n_total, "new": n_new, "updated": n_upd,
            "skipped": n_skip, "by_tool": by_tool}


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    conn: sqlite3.Connection = None
    lock = threading.Lock()

    def log_message(self, *_):  # silence default access logging
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, APP_HTML.read_text(encoding="utf-8"), "text/html")
            if u.path == "/api/stats":
                return self._send(200, self.api_stats())
            if u.path == "/api/sessions":
                return self._send(200, self.api_sessions(q))
            if u.path == "/api/session":
                return self._send(200, self.api_session(q))
            if u.path == "/api/search":
                return self._send(200, self.api_search(q))
            if u.path == "/api/analytics":
                return self._send(200, self.api_analytics(q))
            if u.path == "/api/provenance":
                return self._send(200, self.api_provenance(q))
            if u.path == "/api/history":
                return self._send(200, self.api_history(q))
            if u.path == "/api/projects":
                return self._send(200, self.api_projects(q))
            if u.path == "/api/reindex":
                return self._send(200, self.api_reindex(q))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    # -- endpoints --

    def api_stats(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT tool, COUNT(*) n, SUM(msg_count) msgs, MIN(started) mn, MAX(ended) mx"
                " FROM sessions GROUP BY tool").fetchall()
        total = sum(r["n"] for r in rows)
        return {"total": total,
                "tools": [{"tool": r["tool"], "sessions": r["n"], "messages": r["msgs"] or 0,
                           "earliest": r["mn"], "latest": r["mx"]} for r in rows]}

    def api_sessions(self, q):
        tool = (q.get("tool", [""])[0] or "").strip()
        project = (q.get("project", [""])[0] or "").strip()
        model = (q.get("model", [""])[0] or "").strip()
        branch = (q.get("branch", [""])[0] or "").strip()
        dfrom = (q.get("from", [""])[0] or "").strip()
        dto = (q.get("to", [""])[0] or "").strip()
        limit = min(int(q.get("limit", ["300"])[0]), 2000)
        where, args = [], []
        if tool:
            where.append("tool=?"); args.append(tool)
        if project:
            where.append("project=?"); args.append(project)
        if model:
            where.append("model=?"); args.append(model)
        if branch:
            where.append("git_branch=?"); args.append(branch)
        if dfrom:
            where.append("substr(ended,1,10)>=?"); args.append(dfrom)
        if dto:
            where.append("substr(ended,1,10)<=?"); args.append(dto)
        sql = ("SELECT id,tool,sid,project,cwd,started,ended,msg_count,title,model,"
               "git_branch,cost_usd,in_tokens,out_tokens FROM sessions")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY (ended IS NULL OR ended=''), ended DESC, started DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return {"sessions": [dict(r) for r in rows]}

    def api_projects(self, q):
        with self.lock:
            rows = self.conn.execute(
                "SELECT project, tool, COUNT(*) n, MAX(ended) last, SUM(cost_usd) cost"
                " FROM sessions WHERE project!='' GROUP BY project"
                " ORDER BY (MAX(ended) IS NULL OR MAX(ended)=''), MAX(ended) DESC").fetchall()
        return {"projects": [dict(r) for r in rows]}

    def api_session(self, q):
        sid = int(q.get("id", ["0"])[0])
        with self.lock:
            s = self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if not s:
                return {"error": "not found"}
            msgs = self.conn.execute(
                "SELECT idx,role,ts,text FROM messages WHERE session_id=? ORDER BY idx", (sid,)).fetchall()
            events = self.conn.execute(
                "SELECT idx,ts,kind,name,path,command FROM tool_events WHERE session_id=? ORDER BY idx",
                (sid,)).fetchall()
            usage = self.conn.execute(
                "SELECT model,in_tokens,out_tokens,cache_read_tokens,cache_creation_tokens,cost_usd"
                " FROM usage WHERE session_id=?", (sid,)).fetchall()
        d = dict(s)
        d.pop("mtime", None)
        d["messages"] = [dict(m) for m in msgs]
        d["tool_events"] = [dict(e) for e in events]
        d["usage"] = [dict(u) for u in usage]
        return d

    def api_search(self, q):
        raw = (q.get("q", [""])[0] or "").strip()
        tool = (q.get("tool", [""])[0] or "").strip()
        limit = min(int(q.get("limit", ["80"])[0]), 300)
        if not raw:
            return {"results": [], "query": raw}
        match = self._fts_query(raw)
        # snippet() cannot be combined with GROUP BY in FTS5, so we fetch
        # individual ranked message hits then group into sessions in Python.
        # bm25() returns more-negative scores for better matches.
        sql = """
        SELECT f.session_id AS sid, bm25(fts) AS rank,
               snippet(fts, 0, '«', '»', ' … ', 12) AS snip,
               s.tool, s.project, s.cwd, s.title, s.started, s.ended, s.msg_count, s.model
        FROM fts f JOIN sessions s ON s.id = f.session_id
        WHERE fts MATCH ?
        """
        args = [match]
        if tool:
            sql += " AND s.tool=?"; args.append(tool)
        sql += " ORDER BY rank LIMIT ?"
        args.append(max(limit * 8, 400))
        with self.lock:
            try:
                rows = self.conn.execute(sql, args).fetchall()
            except sqlite3.OperationalError as e:
                return {"error": f"bad query: {e}", "query": raw}
        grouped = {}
        for r in rows:
            sid = r["sid"]
            g = grouped.get(sid)
            if g is None:
                g = dict(r); g["hits"] = 0; g.pop("rank", None)
                grouped[sid] = g
            g["hits"] += 1
        results = list(grouped.values())[:limit]
        return {"query": raw, "results": results}

    @staticmethod
    def _fts_query(raw: str) -> str:
        """Turn a user phrase into a safe FTS5 MATCH expression.

        If the user already uses FTS operators (quotes, AND/OR/NEAR, *), pass through.
        Otherwise treat each bare term as a prefix-AND search.
        """
        if any(c in raw for c in '"*()') or re.search(r"\b(AND|OR|NOT|NEAR)\b", raw):
            return raw
        terms = re.findall(r"[^\s]+", raw)
        safe = []
        for t in terms:
            t = re.sub(r'["]', "", t)
            if t:
                safe.append(f'"{t}"*')
        return " ".join(safe) if safe else raw

    def api_analytics(self, q):
        tool = (q.get("tool", [""])[0] or "").strip()
        project = (q.get("project", [""])[0] or "").strip()
        dfrom = (q.get("from", [""])[0] or "").strip()
        dto = (q.get("to", [""])[0] or "").strip()
        where, args = ["1=1"], []
        if tool:
            where.append("tool=?"); args.append(tool)
        if project:
            where.append("project=?"); args.append(project)
        if dfrom:
            where.append("substr(ended,1,10)>=?"); args.append(dfrom)
        if dto:
            where.append("substr(ended,1,10)<=?"); args.append(dto)
        w = " AND ".join(where)

        def rows(sql, extra=()):
            with self.lock:
                return [dict(r) for r in self.conn.execute(sql, list(args) + list(extra)).fetchall()]

        totals = rows(
            f"SELECT COUNT(*) sessions, COALESCE(SUM(cost_usd),0) cost_usd,"
            f" COALESCE(SUM(in_tokens),0) in_tokens, COALESCE(SUM(out_tokens),0) out_tokens,"
            f" COALESCE(SUM(cache_read_tokens),0) cache_read_tokens,"
            f" SUM(CASE WHEN cost_usd>0 THEN 1 ELSE 0 END) costed_sessions FROM sessions WHERE {w}")[0]
        by_day = rows(
            f"SELECT substr(ended,1,10) day, COUNT(*) sessions, COALESCE(SUM(cost_usd),0) cost_usd,"
            f" COALESCE(SUM(in_tokens),0) in_tokens, COALESCE(SUM(out_tokens),0) out_tokens"
            f" FROM sessions WHERE {w} AND ended!='' GROUP BY day ORDER BY day")
        by_project = rows(
            f"SELECT project, COUNT(*) sessions, COALESCE(SUM(cost_usd),0) cost_usd FROM sessions"
            f" WHERE {w} AND project!='' GROUP BY project ORDER BY cost_usd DESC, sessions DESC LIMIT 25")
        by_tool = rows(
            f"SELECT tool, COUNT(*) sessions, COALESCE(SUM(cost_usd),0) cost_usd,"
            f" COALESCE(SUM(in_tokens),0) in_tokens, COALESCE(SUM(out_tokens),0) out_tokens"
            f" FROM sessions WHERE {w} GROUP BY tool")
        # model split + cache savings comes from the usage table joined to sessions
        by_model = rows(
            f"SELECT u.model, COALESCE(SUM(u.in_tokens),0) in_tokens,"
            f" COALESCE(SUM(u.out_tokens),0) out_tokens,"
            f" COALESCE(SUM(u.cache_read_tokens),0) cache_read_tokens,"
            f" COALESCE(SUM(u.cost_usd),0) cost_usd, COUNT(*) sessions"
            f" FROM usage u JOIN sessions s ON s.id=u.session_id WHERE {w}"
            f" GROUP BY u.model ORDER BY cost_usd DESC")
        # cache savings: cache_read tokens × (input_price − cache_read_price) per model
        cache_savings = 0.0
        unpriced = []
        for m in by_model:
            p = price_for(m["model"] or "")
            if not p:
                if m["model"]:
                    unpriced.append(m["model"])
                continue
            cache_savings += m["cache_read_tokens"] * max(0, p.get("input", 0) - p.get("cache_read", 0)) / 1_000_000
        totals["cache_savings_usd"] = round(cache_savings, 4)
        heatmap = rows(
            f"SELECT CAST(strftime('%w', ended) AS INTEGER) dow,"
            f" CAST(substr(ended,12,2) AS INTEGER) hour, COUNT(*) sessions"
            f" FROM sessions WHERE {w} AND ended!='' GROUP BY dow, hour")
        return {"totals": totals, "by_day": by_day, "by_project": by_project,
                "by_tool": by_tool, "by_model": by_model, "heatmap": heatmap,
                "unpriced": sorted(set(unpriced))}

    def api_provenance(self, q):
        raw = (q.get("q", [""])[0] or "").strip()
        kind = (q.get("kind", ["file"])[0] or "file").strip()
        tool = (q.get("tool", [""])[0] or "").strip()
        limit = min(int(q.get("limit", ["100"])[0]), 400)
        if not raw:
            return {"results": [], "query": raw, "kind": kind}
        col = "command" if kind == "command" else "path"
        match = self._fts_query(raw)
        sql = (f"SELECT f.session_id sid, f.{col} hit, s.tool, s.sid sessionsid, s.project, s.cwd,"
               f" s.title, s.started, s.ended, s.msg_count FROM tool_fts f"
               f" JOIN sessions s ON s.id=f.session_id WHERE tool_fts MATCH ?")
        args = [f"{col}:{match}"]
        if tool:
            sql += " AND s.tool=?"; args.append(tool)
        sql += " LIMIT ?"; args.append(max(limit * 8, 400))
        with self.lock:
            try:
                rows = self.conn.execute(sql, args).fetchall()
            except sqlite3.OperationalError as e:
                return {"error": f"bad query: {e}", "query": raw}
        grouped = {}
        for r in rows:
            g = grouped.get(r["sid"])
            if g is None:
                g = {"session_id": r["sid"], "tool": r["tool"], "sessionsid": r["sessionsid"],
                     "project": r["project"], "cwd": r["cwd"], "title": r["title"],
                     "started": r["started"], "ended": r["ended"], "count": 0, "sample": r["hit"]}
                g["resume"] = self._resume_cmd(r["tool"], r["sessionsid"])
                grouped[r["sid"]] = g
            g["count"] += 1
        results = sorted(grouped.values(), key=lambda x: x["count"], reverse=True)[:limit]
        return {"query": raw, "kind": kind, "results": results}

    @staticmethod
    def _resume_cmd(tool, sessionsid):
        if not sessionsid:
            return ""
        if tool == "claude":
            return f"claude --resume {sessionsid}"
        if tool == "codex":
            return f"codex resume {sessionsid}"
        return ""

    def api_history(self, q):
        raw = (q.get("q", [""])[0] or "").strip().lower()
        limit = min(int(q.get("limit", ["200"])[0]), 1000)
        path = HOME / ".codex" / "history.jsonl"
        prompts = []
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    text = o.get("text", "")
                    if raw and raw not in text.lower():
                        continue
                    prompts.append({"session_id": o.get("session_id", ""),
                                    "ts": norm_ts(o.get("ts")), "text": text[:2000]})
            except Exception:
                pass
        prompts.reverse()
        return {"prompts": prompts[:limit], "total": len(prompts)}

    def api_reindex(self, q=None):
        q = q or {}
        reprice = (q.get("reprice", [""])[0] or "") in ("1", "true", "yes")
        with self.lock:
            if reprice:
                load_prices()
                n = self._reprice()
                return {"ok": True, "repriced": n}
            stats = index(self.conn, force=False)
        return {"ok": True, "stats": stats}

    def _reprice(self):
        """Recompute cost_usd from stored token columns without re-parsing files."""
        rows = self.conn.execute(
            "SELECT id, model, in_tokens, out_tokens, cache_read_tokens, cache_creation_tokens"
            " FROM usage").fetchall()
        for r in rows:
            c = cost_for(r["model"], r["in_tokens"], r["out_tokens"],
                         r["cache_read_tokens"], r["cache_creation_tokens"])
            self.conn.execute("UPDATE usage SET cost_usd=? WHERE id=?", (c, r["id"]))
        # roll session totals back up
        self.conn.execute(
            "UPDATE sessions SET cost_usd=COALESCE("
            "(SELECT SUM(cost_usd) FROM usage WHERE usage.session_id=sessions.id),0)")
        self.conn.commit()
        return len(rows)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="AI Session Browser")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SESSION_BROWSER_PORT", "8765")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reindex", action="store_true", help="force a full rebuild")
    ap.add_argument("--no-open", action="store_true", help="do not auto-open the browser")
    args = ap.parse_args()

    conn = connect()
    load_prices()

    def prog(total, new, upd, skip):
        print(f"  …{total} files ({new} new, {upd} updated, {skip} unchanged)", flush=True)

    print("Indexing sessions (Claude / Codex / Gemini)…", flush=True)
    stats = index(conn, force=args.reindex, progress=prog)
    bt = stats["by_tool"]
    print(f"Indexed {stats['total']} files: {stats['new']} new, {stats['updated']} updated, "
          f"{stats['skipped']} unchanged.", flush=True)
    print(f"  by tool this run -> claude:{bt.get('claude',0)} codex:{bt.get('codex',0)} gemini:{bt.get('gemini',0)}", flush=True)

    Handler.conn = conn
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"\nAI Session Browser → {url}\nPress Ctrl+C to stop.", flush=True)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
        conn.close()


if __name__ == "__main__":
    main()
