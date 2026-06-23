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
import mimetypes
import os
import re
import sqlite3
import subprocess
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

_DEFAULT_REPO_URL = "https://github.com/ryantss/ai-session-browser"


def _read_pyproject():
    """Best-effort (version, homepage) from pyproject.toml next to this module.
    Returns (None, None) when the file is absent (e.g. a pip install)."""
    pp = APP_DIR / "pyproject.toml"
    try:
        text = pp.read_text(encoding="utf-8")
    except OSError:
        return None, None
    ver = re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', text)
    home = re.search(r'(?m)^\s*Homepage\s*=\s*["\']([^"\']+)["\']', text)
    return (ver.group(1) if ver else None), (home.group(1) if home else None)


def _detect_version(pyproject_version):
    try:
        from importlib.metadata import PackageNotFoundError, version as _v
        try:
            return _v("ai-session-browser")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    return pyproject_version or "0.2.0"


def _git_sha():
    """Full HEAD SHA of the checkout this module lives in, or None if git is
    unavailable / this is not a git tree."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=APP_DIR,
            capture_output=True, text=True, timeout=3, check=True)
        sha = out.stdout.strip()
        return sha or None
    except (OSError, subprocess.SubprocessError):
        return None


def _build_info():
    pyproject_version, homepage = _read_pyproject()
    version = _detect_version(pyproject_version)
    repo_url = (homepage or _DEFAULT_REPO_URL).rstrip("/")
    sha_full = _git_sha()
    sha = sha_full[:7] if sha_full else None
    return {
        "version": version,
        "sha": sha,
        "sha_full": sha_full,
        "repo_url": repo_url,
        "commit_url": f"{repo_url}/commit/{sha_full}" if sha_full else None,
    }


BUILD = _build_info()  # computed once at startup
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
    # Other providers (Pi & OpenCode supply authoritative cost; these are fallbacks):
    "deepseek-v4-pro":   {"input": 0.6,  "output": 1.7,  "cache_read": 0.07,  "cache_write": 0.0},
    "deepseek-v4-flash": {"input": 0.07, "output": 0.30, "cache_read": 0.01,  "cache_write": 0.0},
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
_BEDROCK_RE = re.compile(r"^(?:us|eu|apac)\.anthropic\.(.+?)(?:-v\d+)?$")
_PROVIDER_PREFIX_RE = re.compile(r"^(?:[A-Za-z0-9._-]+/)+")


def _normalize_model(model: str) -> str:
    """Collapse provider-prefixed / bedrock / ':free' model ids to a bare key.

    'openai/gpt-5.5' -> 'gpt-5.5', 'deepseek/deepseek-v4-flash:free' ->
    'deepseek-v4-flash', 'us.anthropic.claude-opus-4-6-v1' -> 'claude-opus-4-6'.
    """
    if not model:
        return model
    m = model.split(":", 1)[0]            # drop ':free' / ':<n>' suffix
    bd = _BEDROCK_RE.match(m)
    if bd:
        m = bd.group(1)
    m = _PROVIDER_PREFIX_RE.sub("", m)    # strip leading 'provider/' segments
    return m


def price_for(model: str):
    """Look up a model's price, tolerating provider prefixes, bedrock ids, a ':free'
    suffix, and a trailing -YYYYMMDD snapshot suffix."""
    if not model:
        return None
    for key in (model, _normalize_model(model)):
        if not key:
            continue
        p = _PRICES.get(key) or _PRICES.get(_DATE_SUFFIX_RE.sub("", key))
        if p:
            return p
    return None


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


_INLINE_BINARY_EXT = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "pdf"}

# Served artifacts are untrusted (LLM-generated, may have processed web content).
# An opaque-origin sandbox lets them render and run their own scripts while denying
# access to this dashboard's first-party origin — so a malicious <script> cannot
# read /api/* and exfiltrate session data. NO allow-same-origin (it would re-grant
# the real origin and defeat the isolation). nosniff pins the declared type.
_FILE_SECURITY_HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts",
    "X-Content-Type-Options": "nosniff",
}


def _serve_ctype(path: str) -> str:
    """Content-type for serving a generated artifact inline. HTML renders; images
    and PDFs use their real type; everything else (md, txt, code, json, csv) is
    served as UTF-8 text/plain so it displays in the browser rather than downloads."""
    _, ext = _path_name_ext(path)
    if ext in ("html", "htm"):
        return "text/html"   # _send appends charset for html
    if ext in _INLINE_BINARY_EXT:
        return mimetypes.guess_type(path)[0] or "application/octet-stream"
    return "text/plain; charset=utf-8"


def _path_name_ext(path: str):
    """(basename, lowercased extension-without-dot) for a file path.
    '/a/page.html' -> ('page.html', 'html'); '/a/Makefile' -> ('Makefile', '')."""
    if not path:
        return "", ""
    p = Path(path)
    suf = p.suffix
    return p.name, (suf[1:].lower() if suf else "")


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


def now_iso() -> str:
    """Canonical UTC ISO-8601 timestamp for newly recorded rows."""
    return datetime.now(timezone.utc).isoformat()


def norm_ts(ts) -> str:
    """Return a canonical UTC ISO-8601 string (``...+00:00``) for any timestamp
    shape we encounter, so every stored value is directly comparable.

    Sorting (SQL ``ORDER BY ended``, the sidebar/list ``localeCompare``) is purely
    lexical, which is only correct when all values share one canonical form. Tools
    disagree: Claude logs ``...Z``, others log explicit offsets, and Hermes logs
    naive *local* wall-clock times with no offset. We coerce them all to UTC.
    Naive timestamps are interpreted as local time (Hermes' convention); aware
    ones are converted. Unparseable strings are returned unchanged rather than
    dropped."""
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts / (1000 if ts > 1e12 else 1), timezone.utc).isoformat()
        except Exception:
            return ""
    s = str(ts).strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s  # unknown shape; keep the raw value rather than lose data
    # Naive .astimezone(utc) treats the value as local time, matching Hermes.
    return dt.astimezone(timezone.utc).isoformat()


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
# Pi — ~/.pi/agent/sessions/<encoded-cwd>/<ts>_<uuid>.jsonl
# JSONL event stream; messages carry typed content blocks AND embedded USD cost.
# ---------------------------------------------------------------------------

_PI_FILE_TOOLS = {"read", "edit", "write", "str_replace", "str_replace_editor",
                  "create", "apply_patch", "multiedit"}


def pi_text(content) -> str:
    """Pi message content is a string OR a list of typed blocks."""
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
            parts.append("[thinking] " + (b.get("text") or b.get("thinking", "")))
        elif t == "toolCall":
            inp = json.dumps(b.get("arguments", {}), ensure_ascii=False)
            parts.append(f"[tool: {b.get('name', '?')}] {inp[:800]}")
        elif t == "image":
            parts.append("[image]")
    return "\n".join(p for p in parts if p)


def parse_pi(path: Path):
    cwd = model = sid = start = end = ""
    messages = []
    tool_events = []
    usage = {}          # model -> token accumulator
    cost_by_model = {}  # model -> summed authoritative USD cost
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
                if t == "session":
                    sid = o.get("id") or sid
                    cwd = o.get("cwd") or cwd
                    start = norm_ts(o.get("timestamp")) or start
                elif t == "model_change":
                    model = o.get("modelId") or model
                elif t == "message":
                    msg = o.get("message") or {}
                    role = msg.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    msg_model = msg.get("model") or model
                    if msg_model:
                        model = msg_model
                    ts = norm_ts(o.get("timestamp"))
                    text = _clip(pi_text(msg.get("content")))
                    if text.strip() and not is_noise(text):
                        if not start:
                            start = ts
                        end = ts or end
                        midx = len(messages)
                        messages.append({"role": role, "ts": ts, "text": text})
                        content = msg.get("content")
                        if isinstance(content, list):
                            for b in content:
                                if not isinstance(b, dict) or b.get("type") != "toolCall":
                                    continue
                                name = b.get("name", "")
                                args = b.get("arguments")
                                if not isinstance(args, dict):
                                    args = {}
                                low = name.lower()
                                if low in ("bash", "shell", "exec", "run"):
                                    ev = _te_cmd(midx, ts, name, args.get("command") or args.get("cmd"))
                                elif low in _PI_FILE_TOOLS:
                                    ev = _te_file(midx, ts, name,
                                                  args.get("path") or args.get("file_path") or args.get("filePath"), cwd)
                                else:
                                    ev = None
                                if ev:
                                    tool_events.append(ev)
                    u = msg.get("usage")
                    if isinstance(u, dict) and msg_model:
                        acc = usage.setdefault(msg_model, _zero_usage())
                        acc["in_tokens"] += u.get("input", 0) or 0
                        acc["out_tokens"] += u.get("output", 0) or 0
                        acc["cache_read_tokens"] += u.get("cacheRead", 0) or 0
                        acc["cache_creation_tokens"] += u.get("cacheWrite", 0) or 0
                        c = u.get("cost")
                        if isinstance(c, dict) and isinstance(c.get("total"), (int, float)):
                            cost_by_model[msg_model] = cost_by_model.get(msg_model, 0.0) + c["total"]
    except Exception:
        return None
    if not messages:
        return None
    rows = _usage_list(usage)
    for r in rows:
        if r["model"] in cost_by_model:
            r["cost_usd"] = cost_by_model[r["model"]]
    project = Path(cwd).name if cwd else ""
    meta = {"tool": "pi", "sid": sid or path.stem, "cwd": cwd,
            "project": project, "started": start, "ended": end,
            "model": model, "git_branch": ""}
    return meta, messages, tool_events, rows


# ---------------------------------------------------------------------------
# Hermes — ~/.hermes/sessions/<ts>_<hash>.jsonl (ignore the parallel session_*.json)
# JSONL records; ChatGPT/codex backend => no token usage in logs (cost = $0).
# ---------------------------------------------------------------------------

_HERMES_FILE_TOOLS = {"read", "write", "edit", "str_replace", "str_replace_editor",
                      "apply_patch", "create_file", "multiedit"}


def parse_hermes(path: Path):
    model = sid = start = end = ""
    messages = []
    tool_events = []
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
                role = o.get("role")
                ts = norm_ts(o.get("timestamp"))
                if role == "session_meta":
                    model = o.get("model") or model
                    sid = o.get("session_id") or sid
                    start = norm_ts(o.get("timestamp")) or start
                    continue
                if role not in ("user", "assistant"):
                    continue  # skip 'tool' outputs and other roles
                content = o.get("content")
                text = content if isinstance(content, str) else codex_text(content)
                tool_calls = o.get("tool_calls") or []
                if tool_calls and not (text or "").strip():
                    names = ", ".join((tc.get("function") or {}).get("name", "?")
                                      for tc in tool_calls if isinstance(tc, dict))
                    text = f"[tool: {names}]"
                text = _clip(text or "")
                if not text.strip() or is_noise(text):
                    continue
                if not start:
                    start = ts
                end = ts or end
                midx = len(messages)
                messages.append({"role": role, "ts": ts, "text": text})
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    name = fn.get("name", "?")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    low = name.lower()
                    if low in ("bash", "shell", "exec", "run", "terminal"):
                        ev = _te_cmd(midx, ts, name, args.get("command") or args.get("cmd"))
                    elif low in _HERMES_FILE_TOOLS:
                        ev = _te_file(midx, ts, name,
                                      args.get("path") or args.get("file_path") or args.get("filePath"))
                    else:
                        ev = None
                    if ev:
                        tool_events.append(ev)
    except Exception:
        return None
    if not messages:
        return None
    meta = {"tool": "hermes", "sid": sid or path.stem, "cwd": "",
            "project": "", "started": start, "ended": end,
            "model": model, "git_branch": ""}
    return meta, messages, tool_events, []


# ---------------------------------------------------------------------------
# LM Studio — ~/.lmstudio/conversations/<epoch>.conversation.json (single JSON doc)
# Local chat; no tool events / usage. Each message carries selectable versions.
# ---------------------------------------------------------------------------

def _lmstudio_text(version) -> str:
    if not isinstance(version, dict):
        return ""
    parts = []

    def _blocks(blocks):
        for b in blocks or []:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))

    _blocks(version.get("content"))
    for step in version.get("steps") or []:
        if isinstance(step, dict):
            _blocks(step.get("content"))
    return "\n".join(p for p in parts if p)


def parse_lmstudio(path: Path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            obj = json.load(fh)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    created = norm_ts(obj.get("createdAt"))
    model = ""
    lum = obj.get("lastUsedModel")
    if isinstance(lum, dict):
        model = lum.get("identifier") or lum.get("modelKey") or ""
    messages = []
    for m in obj.get("messages") or []:
        if not isinstance(m, dict):
            continue
        versions = m.get("versions") or []
        if not versions:
            continue
        sel = m.get("currentlySelected") or 0
        if not isinstance(sel, int) or sel < 0 or sel >= len(versions):
            sel = 0
        ver = versions[sel]
        role = (ver or {}).get("role")
        if role not in ("user", "assistant"):
            continue
        text = _clip(_lmstudio_text(ver))
        if not text.strip():
            continue
        messages.append({"role": role, "ts": created, "text": text})
    if not messages:
        return None
    meta = {"tool": "lmstudio", "sid": path.stem, "cwd": "",
            "project": "", "started": created, "ended": created,
            "model": model, "git_branch": ""}
    return meta, messages, [], []


# ---------------------------------------------------------------------------
# OpenCode — ~/.local/share/opencode/opencode.db (one SQLite DB, MANY sessions).
# session/message/part rows; message & part content is JSON in a `data` column.
# ---------------------------------------------------------------------------

_OPENCODE_FILE_TOOLS = {"read", "write", "edit", "patch", "multiedit"}


def opencode_sessions(db_path):
    """Return [(session_id, mtime_secs)] for every OpenCode session (cheap, no parse)."""
    out = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for r in conn.execute("SELECT id, time_updated FROM session"):
                tu = r[1] or 0
                out.append((r[0], (tu / 1000.0) if tu else 0.0))
        finally:
            conn.close()
    except Exception:
        return []
    return out


def parse_opencode_session(db_path, sid):
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            srow = conn.execute(
                "SELECT directory, title, time_created, time_updated FROM session WHERE id=?",
                (sid,)).fetchone()
            if not srow:
                return None
            mrows = conn.execute(
                "SELECT id, data FROM message WHERE session_id=? ORDER BY time_created, id",
                (sid,)).fetchall()
            prows = conn.execute(
                "SELECT message_id, data FROM part WHERE session_id=? ORDER BY time_created, id",
                (sid,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return None

    parts_by_msg = {}
    for pr in prows:
        try:
            pd = json.loads(pr["data"])
        except Exception:
            continue
        parts_by_msg.setdefault(pr["message_id"], []).append(pd)

    cwd = srow["directory"] or ""
    start = norm_ts(srow["time_created"])
    end = norm_ts(srow["time_updated"])
    messages = []
    tool_events = []
    usage = {}
    cost_by_model = {}
    model_counts = {}

    for mr in mrows:
        try:
            md = json.loads(mr["data"])
        except Exception:
            continue
        role = md.get("role")
        if role not in ("user", "assistant"):
            continue
        m_model = md.get("modelID") or ""
        tinfo = md.get("time")
        ts = norm_ts(tinfo.get("created")) if isinstance(tinfo, dict) else ""
        midx = len(messages)
        text_parts = []
        for pd in parts_by_msg.get(mr["id"], []):
            pt = pd.get("type")
            if pt == "text":
                text_parts.append(pd.get("text", ""))
            elif pt == "reasoning":
                text_parts.append("[thinking] " + pd.get("text", ""))
            elif pt == "tool":
                inp = (pd.get("state") or {}).get("input") or {}
                if not isinstance(inp, dict):
                    inp = {}
                tname = pd.get("tool", "?")
                text_parts.append(f"[tool: {tname}] {json.dumps(inp, ensure_ascii=False)[:800]}")
                low = str(tname).lower()
                if low in ("bash", "shell"):
                    ev = _te_cmd(midx, ts, tname, inp.get("command") or inp.get("cmd"))
                    if ev:
                        tool_events.append(ev)
                elif low in _OPENCODE_FILE_TOOLS:
                    ev = _te_file(midx, ts, tname,
                                  inp.get("filePath") or inp.get("path") or inp.get("file_path"), cwd)
                    if ev:
                        tool_events.append(ev)
            elif pt == "patch":
                files = pd.get("files") or []
                if isinstance(files, dict):
                    files = list(files.keys())
                text_parts.append("[patch] " + ", ".join(str(f) for f in files))
                for f in files:
                    ev = _te_file(midx, ts, "patch", f, cwd)
                    if ev:
                        tool_events.append(ev)
        text = _clip("\n".join(p for p in text_parts if p))
        if not text.strip():
            continue
        if m_model:
            model_counts[m_model] = model_counts.get(m_model, 0) + 1
        if not start:
            start = ts
        end = ts or end
        messages.append({"role": role, "ts": ts, "text": text})
        tok = md.get("tokens")
        if isinstance(tok, dict) and m_model:
            acc = usage.setdefault(m_model, _zero_usage())
            acc["in_tokens"] += tok.get("input", 0) or 0
            acc["out_tokens"] += (tok.get("output", 0) or 0) + (tok.get("reasoning", 0) or 0)
            cache = tok.get("cache")
            if isinstance(cache, dict):
                acc["cache_read_tokens"] += cache.get("read", 0) or 0
                acc["cache_creation_tokens"] += cache.get("write", 0) or 0
        c = md.get("cost")
        if isinstance(c, (int, float)) and m_model:
            cost_by_model[m_model] = cost_by_model.get(m_model, 0.0) + c

    if not messages:
        return None
    model = max(model_counts, key=model_counts.get) if model_counts else ""
    rows = _usage_list(usage)
    for r in rows:
        if r["model"] in cost_by_model:
            r["cost_usd"] = cost_by_model[r["model"]]
    project = Path(cwd).name if cwd else (srow["title"] or "")
    meta = {"tool": "opencode", "sid": str(sid), "cwd": cwd,
            "project": project, "started": start, "ended": end,
            "model": model, "git_branch": ""}
    return meta, messages, tool_events, rows


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

# File-glob sources: (tool, root, mode, pattern, parser). One file == one session.
_FILE_SOURCES = [
    ("claude",   HOME / ".claude" / "projects",       "rglob", "*.jsonl",             parse_claude),
    ("codex",    HOME / ".codex" / "sessions",         "rglob", "rollout-*.jsonl",     parse_codex),
    ("gemini",   HOME / ".gemini" / "tmp",             "glob",  "*/chats/*",           parse_gemini),
    ("pi",       HOME / ".pi" / "agent" / "sessions",  "rglob", "*.jsonl",             parse_pi),
    ("hermes",   HOME / ".hermes" / "sessions",        "glob",  "*.jsonl",             parse_hermes),
    ("lmstudio", HOME / ".lmstudio" / "conversations", "glob",  "*.conversation.json", parse_lmstudio),
]


def _wrap_file_parser(fn):
    return lambda key: fn(Path(key))


def discover():
    """Yield (tool, key, parser, mtime) for every candidate session.

    File sources: ``key`` is the file path and ``mtime`` is None (the indexer stats
    the file). DB sources (OpenCode): ``key`` is ``'<db>#<session_id>'`` and ``mtime``
    is supplied from the session row so each session re-indexes on its own.
    """
    for tool, root, mode, pattern, parser in _FILE_SOURCES:
        if not root.is_dir():
            continue
        it = root.rglob(pattern) if mode == "rglob" else root.glob(pattern)
        wrapped = _wrap_file_parser(parser)
        for p in it:
            if tool == "gemini" and p.suffix not in (".json", ".jsonl"):
                continue
            yield (tool, str(p), wrapped, None)

    # Paperclip wraps Codex sessions — reuse parse_codex, label them as codex.
    pc_root = HOME / ".paperclip" / "instances"
    if pc_root.is_dir():
        wrapped = _wrap_file_parser(parse_codex)
        for p in pc_root.glob("*/codex-home/sessions/**/rollout-*.jsonl"):
            yield ("codex", str(p), wrapped, None)

    # OpenCode — one SQLite DB holds many sessions; yield a virtual key per session.
    oc_db = HOME / ".local" / "share" / "opencode" / "opencode.db"
    if oc_db.is_file():
        for sid, mtime in opencode_sessions(oc_db):
            yield ("opencode", f"{oc_db}#{sid}",
                   (lambda _k, _s=sid: parse_opencode_session(oc_db, _s)), mtime)


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
    cost_usd REAL DEFAULT 0, cost_source TEXT DEFAULT 'table'
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
-- User-curated collections (bookmarks). Membership keys on the stable
-- sessions.path, NOT sessions.id (which reindex reassigns), and these tables
-- are never touched by _delete_session_rows() / the reindex path.
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    sort_order INTEGER DEFAULT 0,
    created TEXT
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL,
    session_path  TEXT NOT NULL,
    added TEXT,
    PRIMARY KEY (collection_id, session_path)
);
CREATE INDEX IF NOT EXISTS idx_ci_path ON collection_items(session_path);
"""

# Columns added to `sessions` after v0.1 — ALTERed in on existing DBs.
_SESSION_MIGRATIONS = {
    "git_branch": "TEXT", "in_tokens": "INTEGER DEFAULT 0",
    "out_tokens": "INTEGER DEFAULT 0", "cache_read_tokens": "INTEGER DEFAULT 0",
    "cache_creation_tokens": "INTEGER DEFAULT 0", "cost_usd": "REAL DEFAULT 0",
}

# Columns added to `usage` after v0.2 — ALTERed in on existing DBs.
_USAGE_MIGRATIONS = {"cost_source": "TEXT DEFAULT 'table'"}


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
    have_u = {r["name"] for r in conn.execute("PRAGMA table_info(usage)")}
    for col, decl in _USAGE_MIGRATIONS.items():
        if col not in have_u:
            conn.execute(f"ALTER TABLE usage ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


def _delete_session_rows(conn, session_id):
    conn.execute("DELETE FROM tool_fts WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM tool_events WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM usage WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM fts WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


# UTC ISO timestamp of the most recent index() run (in-memory; reset each process start).
LAST_INDEXED = None


def index(conn, force=False, progress=lambda *_: None):
    existing = {row["path"]: (row["id"], row["mtime"])
                for row in conn.execute("SELECT id, path, mtime FROM sessions")}
    seen_paths = set()
    n_new = n_upd = n_skip = n_total = 0
    by_tool = {}

    for tool, key, parser, src_mtime in discover():
        n_total += 1
        spath = key
        seen_paths.add(spath)
        if src_mtime is not None:
            mtime = src_mtime
        else:
            try:
                mtime = Path(key).stat().st_mtime
            except OSError:
                continue
        prev = existing.get(spath)
        if prev and not force and abs(prev[1] - mtime) < 1e-6:
            n_skip += 1
            continue

        parsed = parser(key)
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
            if u.get("cost_usd") is not None:
                c = u["cost_usd"]            # authoritative cost reported by the agent
                u["cost_source"] = "agent"
            else:
                c = cost_for(u["model"], u["in_tokens"], u["out_tokens"],
                             u["cache_read_tokens"], u["cache_creation_tokens"])
                u["cost_source"] = "table"
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
                "cache_creation_tokens,cost_usd,cost_source) VALUES(?,?,?,?,?,?,?,?)",
                [(sess_id, u["model"], u["in_tokens"], u["out_tokens"], u["cache_read_tokens"],
                  u["cache_creation_tokens"], u["cost_usd"], u.get("cost_source", "table")) for u in usage])
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
    global LAST_INDEXED
    LAST_INDEXED = datetime.now(timezone.utc).isoformat()
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

    def _send(self, code, body, ctype="application/json", extra_headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
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
            if u.path == "/api/files":
                return self._send(200, self.api_files(q))
            if u.path == "/file":
                code, body, ctype, headers = self.file_payload((q.get("path", [""])[0] or "").strip())
                return self._send(code, body, ctype, headers)
            if u.path == "/api/history":
                return self._send(200, self.api_history(q))
            if u.path == "/api/projects":
                return self._send(200, self.api_projects(q))
            if u.path == "/api/collections":
                return self._send(200, self.api_collections())
            if u.path == "/api/session-collections":
                return self._send(200, self.api_session_collections(q))
            if u.path == "/api/bookmarked-paths":
                return self._send(200, self.api_bookmarked_paths())
            if u.path == "/api/reindex":
                return self._send(200, self.api_reindex(q))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                return self._send(400, {"error": "expected a JSON object"})
            routes = {
                "/api/collections": self.api_collection_create,
                "/api/collections/rename": self.api_collection_rename,
                "/api/collections/delete": self.api_collection_delete,
                "/api/collection_items": self.api_collection_item,
            }
            handler = routes.get(u.path)
            if handler is None:
                return self._send(404, {"error": "not found"})
            result = handler(body)
            return self._send(400 if "error" in result else 200, result)
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON body"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    # -- endpoints --

    def api_stats(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT tool, COUNT(*) n, SUM(msg_count) msgs, MIN(started) mn, MAX(ended) mx"
                " FROM sessions GROUP BY tool").fetchall()
        total = sum(r["n"] for r in rows)
        return {"total": total, "last_indexed": LAST_INDEXED, "build": BUILD,
                "tools": [{"tool": r["tool"], "sessions": r["n"], "messages": r["msgs"] or 0,
                           "earliest": r["mn"], "latest": r["mx"]} for r in rows]}

    def api_sessions(self, q):
        tool = (q.get("tool", [""])[0] or "").strip()
        project = (q.get("project", [""])[0] or "").strip()
        model = (q.get("model", [""])[0] or "").strip()
        branch = (q.get("branch", [""])[0] or "").strip()
        dfrom = (q.get("from", [""])[0] or "").strip()
        dto = (q.get("to", [""])[0] or "").strip()
        collection = (q.get("collection", [""])[0] or "").strip()
        sort = (q.get("sort", ["recent"])[0] or "recent").strip()
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
        if collection:
            where.append("path IN (SELECT session_path FROM collection_items WHERE collection_id=?)")
            args.append(int(collection))
        sql = ("SELECT id,tool,sid,path,project,cwd,started,ended,msg_count,title,model,"
               "git_branch,cost_usd,in_tokens,out_tokens FROM sessions")
        if where:
            sql += " WHERE " + " AND ".join(where)
        order_by = {
            "recent":   "(ended IS NULL OR ended=''), ended DESC, started DESC",
            "project":  "project='', project COLLATE NOCASE ASC, ended DESC",
            "cost":     "cost_usd DESC, ended DESC",
            "messages": "msg_count DESC, ended DESC",
        }
        sql += " ORDER BY " + order_by.get(sort, order_by["recent"]) + " LIMIT ?"
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

    # -- collections (bookmarks) --

    def api_collections(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT c.id, c.name, COUNT(ci.session_path) n FROM collections c"
                " LEFT JOIN collection_items ci ON ci.collection_id=c.id"
                " GROUP BY c.id ORDER BY c.sort_order, c.name COLLATE NOCASE").fetchall()
        return {"collections": [dict(r) for r in rows]}

    def api_session_collections(self, q):
        path = (q.get("path", [""])[0] or "").strip()
        with self.lock:
            rows = self.conn.execute(
                "SELECT collection_id FROM collection_items WHERE session_path=?",
                (path,)).fetchall()
        return {"ids": [r["collection_id"] for r in rows]}

    def api_bookmarked_paths(self):
        """Every session_path that belongs to at least one collection — lets the UI
        flag already-bookmarked rows in one request instead of N lookups."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT session_path FROM collection_items").fetchall()
        return {"paths": [r["session_path"] for r in rows]}

    def api_collection_create(self, body):
        name = (body.get("name") or "").strip()
        if not name:
            return {"error": "name required"}
        with self.lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO collections(name, created) VALUES(?, ?)",
                    (name, now_iso()))
                self.conn.commit()
            except sqlite3.IntegrityError:
                return {"error": "name already exists"}
        return {"id": cur.lastrowid, "name": name}

    def api_collection_rename(self, body):
        cid = int(body.get("id") or 0)
        name = (body.get("name") or "").strip()
        if not name:
            return {"error": "name required"}
        with self.lock:
            try:
                self.conn.execute("UPDATE collections SET name=? WHERE id=?", (name, cid))
                self.conn.commit()
            except sqlite3.IntegrityError:
                return {"error": "name already exists"}
        return {"ok": True}

    def api_collection_delete(self, body):
        cid = int(body.get("id") or 0)
        with self.lock:
            self.conn.execute("DELETE FROM collection_items WHERE collection_id=?", (cid,))
            self.conn.execute("DELETE FROM collections WHERE id=?", (cid,))
            self.conn.commit()
        return {"ok": True}

    def api_collection_item(self, body):
        cid = int(body.get("collection_id") or 0)
        path = (body.get("path") or "").strip()
        op = (body.get("op") or "").strip()
        if not (cid and path and op in ("add", "remove")):
            return {"error": "collection_id, path and op(add|remove) required"}
        with self.lock:
            if op == "add":
                self.conn.execute(
                    "INSERT OR IGNORE INTO collection_items(collection_id, session_path, added)"
                    " VALUES(?, ?, ?)", (cid, path, now_iso()))
            else:
                self.conn.execute(
                    "DELETE FROM collection_items WHERE collection_id=? AND session_path=?",
                    (cid, path))
            self.conn.commit()
        return {"ok": True}

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
        role = (q.get("role", [""])[0] or "").strip()
        sort = (q.get("sort", ["relevance"])[0] or "relevance").strip()
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
               s.tool, s.project, s.cwd, s.title, s.started, s.ended, s.msg_count, s.model, s.path
        FROM fts f JOIN sessions s ON s.id = f.session_id
        WHERE fts MATCH ?
        """
        args = [match]
        if tool:
            sql += " AND s.tool=?"; args.append(tool)
        if role in ("user", "assistant"):
            sql += " AND f.role=?"; args.append(role)
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
        # Messages are fetched in bm25 rank order, so grouped.values() is already
        # in relevance order (best snippet per session kept). Re-sort the sessions
        # to honor the `sort` selector; an unknown sort keeps relevance order.
        results = list(grouped.values())
        if sort == "recent":
            results.sort(key=lambda r: (r.get("ended") or "", r.get("started") or ""), reverse=True)
        elif sort == "messages":
            results.sort(key=lambda r: (r.get("msg_count") or 0), reverse=True)
        elif sort == "project":
            results.sort(key=lambda r: ((r.get("project") or "") == "", (r.get("project") or "").lower()))
        results = results[:limit]
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
                if m["model"] and not m["cost_usd"]:  # agent-costed models aren't "unpriced"
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
                g["resume"] = self._resume_cmd(r["tool"], r["sessionsid"], r.get("cwd", ""))
                grouped[r["sid"]] = g
            g["count"] += 1
        results = sorted(grouped.values(), key=lambda x: x["count"], reverse=True)[:limit]
        return {"query": raw, "kind": kind, "results": results}

    @staticmethod
    def _resume_cmd(tool, sessionsid, cwd=""):
        if not sessionsid:
            return ""
        prefix = f"cd {cwd} && " if cwd else ""
        if tool == "claude":
            return f"{prefix}claude --resume {sessionsid}"
        if tool == "codex":
            return f"{prefix}codex resume {sessionsid}"
        return ""

    def api_files(self, q):
        """Generated Files view. Without ``path``: a list of distinct files that
        sessions wrote/modified (pure Reads excluded), grouped by path, each linked
        to its most-recent originating session, with ext/tool/substring filters and
        a type facet. With ``path``: every session that touched that file, recent
        first (full provenance history for one artifact)."""
        path = (q.get("path", [""])[0] or "").strip()
        if path:
            return self._api_file_sessions(path)
        tool = (q.get("tool", [""])[0] or "").strip()
        ext = (q.get("ext", [""])[0] or "").strip().lstrip(".").lower()
        raw = (q.get("q", [""])[0] or "").strip()
        sort = (q.get("sort", ["recent"])[0] or "recent").strip()
        limit = min(int(q.get("limit", ["500"])[0]), 2000)

        # Base predicate: file events that are writes/edits (not reads). Read tools
        # across every parser are named 'Read'/'read'/'read_file' — exclude 'read*'.
        base_where = ["te.kind='file'", "te.path!=''", "lower(te.name) NOT LIKE 'read%'"]
        base_args = []
        if tool:
            base_where.append("s.tool=?"); base_args.append(tool)
        if raw:
            base_where.append("lower(te.path) LIKE ?"); base_args.append(f"%{raw.lower()}%")
        main_where = list(base_where)
        main_args = list(base_args)
        if ext:
            main_where.append("lower(te.path) LIKE ?"); main_args.append(f"%.{ext}")

        order_by = {
            "recent":   "last_ts DESC, path COLLATE NOCASE",
            "name":     "path COLLATE NOCASE ASC",
            "sessions": "session_count DESC, last_ts DESC",
            "ops":      "ops DESC, last_ts DESC",
        }.get(sort, "last_ts DESC, path COLLATE NOCASE")
        # Bare columns (s.*, last_session_id) come from the MAX(te.ts) input row —
        # SQLite's documented min/max bare-column rule — i.e. the most-recent toucher.
        sql = (
            "SELECT te.path AS path, MAX(te.ts) AS last_ts,"
            " COUNT(DISTINCT te.session_id) AS session_count, COUNT(*) AS ops,"
            " s.id AS last_session_id, s.tool AS tool, s.project AS project,"
            " s.title AS title, s.sid AS sessionsid, s.cwd AS cwd"
            " FROM tool_events te JOIN sessions s ON s.id=te.session_id"
            f" WHERE {' AND '.join(main_where)}"
            f" GROUP BY te.path ORDER BY {order_by} LIMIT ?")
        with self.lock:
            rows = self.conn.execute(sql, main_args + [limit]).fetchall()
            # Type facet over the base set (ignores the active ext filter so the UI
            # dropdown stays stable), counting distinct files per extension.
            type_paths = self.conn.execute(
                "SELECT DISTINCT te.path FROM tool_events te JOIN sessions s ON s.id=te.session_id"
                f" WHERE {' AND '.join(base_where)}", base_args).fetchall()
        files = []
        for r in rows:
            d = dict(r)
            d["name"], d["ext"] = _path_name_ext(d["path"])
            files.append(d)
        type_counts = {}
        for r in type_paths:
            _, e = _path_name_ext(r["path"])
            type_counts[e] = type_counts.get(e, 0) + 1
        types = sorted(({"ext": e, "n": n} for e, n in type_counts.items()),
                       key=lambda t: (-t["n"], t["ext"]))
        return {"files": files, "types": types}

    def _api_file_sessions(self, path):
        sql = (
            "SELECT s.id AS session_id, s.tool, s.project, s.title, s.sid AS sessionsid,"
            " s.cwd, s.started, s.ended, COUNT(*) AS ops, MAX(te.ts) AS last_ts"
            " FROM tool_events te JOIN sessions s ON s.id=te.session_id"
            " WHERE te.kind='file' AND te.path=? AND lower(te.name) NOT LIKE 'read%'"
            " GROUP BY s.id ORDER BY last_ts DESC, s.ended DESC")
        with self.lock:
            rows = self.conn.execute(sql, (path,)).fetchall()
        sessions = []
        for r in rows:
            d = dict(r)
            d["resume"] = self._resume_cmd(d["tool"], d["sessionsid"], d.get("cwd", ""))
            sessions.append(d)
        name, ext = _path_name_ext(path)
        return {"path": path, "name": name, "ext": ext, "sessions": sessions}

    def file_payload(self, path):
        """Serve a generated artifact for inline viewing. Returns (code, body, ctype,
        headers): raw bytes on success, an error dict otherwise. Guarded so only files
        the index already references can be read — never an arbitrary local-file proxy.
        Successful responses carry a sandbox CSP so untrusted artifacts are isolated
        from this dashboard's origin (see _FILE_SECURITY_HEADERS)."""
        if not path:
            return 404, {"error": "path required"}, "application/json", {}
        with self.lock:
            known = self.conn.execute(
                "SELECT 1 FROM tool_events WHERE path=? AND kind='file' LIMIT 1", (path,)).fetchone()
        if not known:
            return 404, {"error": "not an indexed generated file"}, "application/json", {}
        try:
            body = Path(path).read_bytes()
        except OSError:
            return 404, {"error": "file not found on disk"}, "application/json", {}
        return 200, body, _serve_ctype(path), dict(_FILE_SECURITY_HEADERS)

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
            "SELECT id, model, in_tokens, out_tokens, cache_read_tokens, cache_creation_tokens,"
            " cost_source FROM usage").fetchall()
        for r in rows:
            if r["cost_source"] == "agent":
                continue  # preserve authoritative agent-reported cost
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

    print("Indexing sessions (Claude / Codex / Gemini / Pi / Hermes / OpenCode / LM Studio)…", flush=True)
    stats = index(conn, force=args.reindex, progress=prog)
    bt = stats["by_tool"]
    print(f"Indexed {stats['total']} files: {stats['new']} new, {stats['updated']} updated, "
          f"{stats['skipped']} unchanged.", flush=True)
    if bt:
        print("  by tool this run -> " + "  ".join(f"{k}:{v}" for k, v in sorted(bt.items())), flush=True)

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
