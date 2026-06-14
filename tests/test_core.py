"""Core invariant tests for ai-session-browser. Stdlib only: `python3 -m unittest`."""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(server.SCHEMA)
    return conn


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("sessions", "messages", "usage", "tool_events", "fts", "tool_fts")}


def make_handler(conn):
    """A Handler bound to `conn` without running BaseHTTPRequestHandler.__init__
    (which expects a live socket). The API methods only touch self.conn / self.lock."""
    h = object.__new__(server.Handler)
    h.conn = conn
    h.lock = threading.Lock()
    return h


class TestPricing(unittest.TestCase):
    def setUp(self):
        server.load_prices()

    def test_input_output_rates(self):
        # Opus 4.8: $5/MTok in, $25/MTok out
        self.assertAlmostEqual(server.cost_for("claude-opus-4-8", 1_000_000, 0), 5.0)
        self.assertAlmostEqual(server.cost_for("claude-opus-4-8", 0, 1_000_000), 25.0)
        self.assertAlmostEqual(server.cost_for("claude-sonnet-4-6", 1_000_000, 1_000_000), 18.0)

    def test_cache_rates(self):
        self.assertAlmostEqual(server.cost_for("claude-opus-4-8", 0, 0, 1_000_000, 0), 0.5)
        self.assertAlmostEqual(server.cost_for("claude-opus-4-8", 0, 0, 0, 1_000_000), 6.25)

    def test_unknown_model_is_zero(self):
        self.assertEqual(server.cost_for("totally-made-up", 1_000_000, 1_000_000), 0.0)

    def test_date_suffix_normalization(self):
        # Claude logs dated IDs like claude-haiku-4-5-20251001
        self.assertIsNotNone(server.price_for("claude-haiku-4-5-20251001"))
        self.assertEqual(server.price_for("claude-haiku-4-5-20251001"),
                         server.price_for("claude-haiku-4-5"))

    def test_prices_json_override(self):
        with tempfile.TemporaryDirectory() as d:
            pj = Path(d) / "prices.json"
            pj.write_text('{"my-model": {"input": 2.0, "output": 4.0}}')
            old = server.PRICES_PATH
            try:
                server.PRICES_PATH = pj
                server.load_prices()
                self.assertAlmostEqual(server.cost_for("my-model", 1_000_000, 0), 2.0)
            finally:
                server.PRICES_PATH = old
                server.load_prices()


class TestTimestampNormalization(unittest.TestCase):
    """norm_ts must canonicalize every timestamp shape to one comparable UTC form,
    so lexical sorting (SQL `ORDER BY ended`, frontend localeCompare) is correct.
    Regression: Hermes logs naive *local* wall-clock times (no offset); comparing
    them lexically against Claude's `...Z` UTC strings misordered the session list."""

    def setUp(self):
        # Pin local tz so naive-timestamp handling is deterministic (UTC+8).
        self._tz = os.environ.get("TZ")
        if hasattr(time, "tzset"):
            os.environ["TZ"] = "Asia/Singapore"
            time.tzset()

    def tearDown(self):
        if hasattr(time, "tzset"):
            if self._tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = self._tz
            time.tzset()

    def test_zulu_normalized_to_offset(self):
        # Claude's `...Z` must become a uniform +00:00 form (not left as Z).
        out = server.norm_ts("2026-06-14T16:27:07.830Z")
        self.assertTrue(out.endswith("+00:00"), out)
        self.assertEqual(out, "2026-06-14T16:27:07.830000+00:00")

    def test_naive_local_converted_to_utc(self):
        # Hermes-style naive local time (UTC+8) -> UTC.
        if not hasattr(time, "tzset"):
            self.skipTest("tzset unavailable")
        self.assertEqual(server.norm_ts("2026-05-22T18:00:00"),
                         "2026-05-22T10:00:00+00:00")

    def test_explicit_offset_preserved_as_utc(self):
        self.assertEqual(server.norm_ts("2026-01-07T06:17:06.501000+00:00"),
                         "2026-01-07T06:17:06.501000+00:00")

    def test_epoch_seconds_and_millis(self):
        self.assertEqual(server.norm_ts(1_700_000_000), "2023-11-14T22:13:20+00:00")
        self.assertEqual(server.norm_ts(1_700_000_000_000), "2023-11-14T22:13:20+00:00")

    def test_empty_and_garbage(self):
        self.assertEqual(server.norm_ts(None), "")
        self.assertEqual(server.norm_ts(""), "")
        # Unparseable strings are returned as-is rather than dropped.
        self.assertEqual(server.norm_ts("not-a-date"), "not-a-date")

    def test_canonical_forms_sort_chronologically(self):
        # The exact bug: hermes-local 18:07 (=10:07 UTC) vs codex 10:54 UTC, same date.
        # Real order (oldest->newest): hermes 10:07 < codex 10:54 < claude next-day.
        if not hasattr(time, "tzset"):
            self.skipTest("tzset unavailable")
        hermes = server.norm_ts("2026-05-22T18:07:59.351218")   # local +8 -> 10:07 UTC
        codex = server.norm_ts("2026-05-22T10:54:18.603Z")
        claude = server.norm_ts("2026-05-23T03:28:40.916Z")
        self.assertEqual(sorted([claude, codex, hermes]), [hermes, codex, claude])


class TestExtractionHelpers(unittest.TestCase):
    def test_parse_apply_patch(self):
        patch = ("*** Begin Patch\n"
                 "*** Update File: /a/b.py\n@@\n-x\n+y\n"
                 "*** Add File: /a/c.py\n+new\n"
                 "*** Delete File: /a/d.py\n"
                 "*** End Patch")
        self.assertEqual(server._parse_apply_patch(patch), ["/a/b.py", "/a/c.py", "/a/d.py"])

    def test_te_file_and_cmd(self):
        self.assertEqual(server._te_file(0, "t", "Read", "/abs/x.py")["path"], "/abs/x.py")
        self.assertEqual(server._te_file(0, "t", "Read", "rel.py", "/base")["path"], "/base/rel.py")
        self.assertIsNone(server._te_file(0, "t", "Read", ""))
        self.assertEqual(server._te_cmd(0, "t", "Bash", " git push ")["command"], "git push")
        self.assertIsNone(server._te_cmd(0, "t", "Bash", "   "))

    def test_usage_list_drops_zero_rows(self):
        usage = {"m1": server._zero_usage(),
                 "m2": {**server._zero_usage(), "in_tokens": 5}}
        out = server._usage_list(usage)
        self.assertEqual([u["model"] for u in out], ["m2"])


class TestDeleteAndIndex(unittest.TestCase):
    def test_delete_clears_all_tables(self):
        conn = fresh_conn()
        conn.execute("INSERT INTO sessions(id,tool,sid,path) VALUES(1,'claude','s','/p')")
        conn.execute("INSERT INTO messages(session_id,idx,role,ts,text) VALUES(1,0,'user','t','hi')")
        conn.execute("INSERT INTO usage(session_id,model,in_tokens) VALUES(1,'m',5)")
        conn.execute("INSERT INTO tool_events(session_id,idx,ts,kind,name,path) VALUES(1,0,'t','file','Read','/x')")
        conn.execute("INSERT INTO fts(text,session_id,idx,role) VALUES('hi',1,0,'user')")
        conn.execute("INSERT INTO tool_fts(path,command,session_id,te_id) VALUES('/x','',1,0)")
        self.assertEqual(sum(counts(conn).values()), 6)
        server._delete_session_rows(conn, 1)
        self.assertEqual(sum(counts(conn).values()), 0)

    def test_index_no_leak_on_reparse(self):
        # Forcing a re-parse must delete+reinsert, not accumulate (the v0.2 invariant).
        with tempfile.TemporaryDirectory() as d:
            fpath = Path(d) / "session.jsonl"
            fpath.write_text("x")  # real file so path.stat() works

            def fake_parser(_p):
                meta = {"tool": "claude", "sid": "abc", "cwd": "/repo", "project": "repo",
                        "started": "2026-06-01T00:00:00Z", "ended": "2026-06-01T01:00:00Z",
                        "model": "claude-opus-4-8", "git_branch": "main"}
                messages = [{"role": "user", "ts": "t", "text": "hello"},
                            {"role": "assistant", "ts": "t", "text": "[tool: Bash] ls"}]
                tool_events = [{"idx": 1, "ts": "t", "kind": "command", "name": "Bash",
                                "path": None, "command": "ls"}]
                usage = [{"model": "claude-opus-4-8", "in_tokens": 100, "out_tokens": 50,
                          "cache_read_tokens": 0, "cache_creation_tokens": 0}]
                return meta, messages, tool_events, usage

            conn = fresh_conn()
            orig = server.discover
            try:
                server.discover = lambda: [("claude", str(fpath), fake_parser, None)]
                server.index(conn)
                first = counts(conn)
                server.index(conn, force=True)   # re-parse same file
                second = counts(conn)
            finally:
                server.discover = orig

            self.assertEqual(first, second, "re-parse leaked rows")
            self.assertEqual(first["sessions"], 1)
            self.assertEqual(first["messages"], 2)
            self.assertEqual(first["usage"], 1)
            self.assertEqual(first["tool_events"], 1)
            # cost rolled onto the session: 100*5/1e6 + 50*25/1e6 = 0.00175
            cost = conn.execute("SELECT cost_usd FROM sessions WHERE id=1").fetchone()[0]
            self.assertAlmostEqual(cost, 0.00175, places=6)


class TestModelNormalization(unittest.TestCase):
    def setUp(self):
        server.load_prices()

    def test_provider_prefix(self):
        self.assertIsNotNone(server.price_for("openai/gpt-5.5"))
        self.assertEqual(server.price_for("openai/gpt-5.5"), server.price_for("gpt-5.5"))

    def test_bedrock_id(self):
        self.assertEqual(server.price_for("us.anthropic.claude-opus-4-6-v1"),
                         server.price_for("claude-opus-4-6"))

    def test_free_suffix(self):
        self.assertEqual(server.price_for("deepseek/deepseek-v4-flash:free"),
                         server.price_for("deepseek-v4-flash"))

    def test_bare_models_unchanged(self):  # regression guard
        self.assertIsNotNone(server.price_for("claude-opus-4-8"))
        self.assertIsNotNone(server.price_for("gpt-5.5"))


class TestPiParser(unittest.TestCase):
    def _write(self, d, lines):
        p = Path(d) / "2026-01-01T00-00-00-000Z_sid.jsonl"
        p.write_text("\n".join(json.dumps(o) for o in lines))
        return p

    def test_cost_passthrough(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, [
                {"type": "session", "id": "sid", "cwd": "/repo", "timestamp": "2026-01-01T00:00:00Z"},
                {"type": "model_change", "modelId": "openrouter/owl-alpha"},
                {"type": "message", "timestamp": "2026-01-01T00:01:00Z",
                 "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}},
                {"type": "message", "timestamp": "2026-01-01T00:02:00Z",
                 "message": {"role": "assistant", "model": "openrouter/owl-alpha",
                             "content": [{"type": "text", "text": "done"},
                                         {"type": "toolCall", "name": "bash",
                                          "arguments": {"command": "ls -la"}}],
                             "usage": {"input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0,
                                       "cost": {"total": 0.42}}}},
            ])
            meta, msgs, tes, usage = server.parse_pi(p)
            self.assertEqual(meta["tool"], "pi")
            self.assertEqual(len(usage), 1)
            self.assertAlmostEqual(usage[0]["cost_usd"], 0.42)
            self.assertTrue(any(t["kind"] == "command" and t["command"] == "ls -la" for t in tes))

            # end-to-end through index(): agent cost wins over the (absent) table price
            conn = fresh_conn()
            orig = server.discover
            try:
                server.discover = lambda: [("pi", str(p), server._wrap_file_parser(server.parse_pi), None)]
                server.index(conn)
            finally:
                server.discover = orig
            row = conn.execute("SELECT cost_usd, cost_source FROM usage").fetchone()
            self.assertAlmostEqual(row["cost_usd"], 0.42)
            self.assertEqual(row["cost_source"], "agent")
            scost = conn.execute("SELECT cost_usd FROM sessions").fetchone()[0]
            self.assertAlmostEqual(scost, 0.42)

    def test_no_messages_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, [
                {"type": "session", "id": "sid", "cwd": "/r", "timestamp": "2026-01-01T00:00:00Z"},
                {"type": "model_change", "modelId": "x/y"},
            ])
            self.assertIsNone(server.parse_pi(p))


class TestHermesParser(unittest.TestCase):
    def test_huge_meta_and_no_usage(self):
        with tempfile.TemporaryDirectory() as d:
            big_tools = [{"name": f"tool_{i}", "schema": "x" * 200} for i in range(60)]
            p = Path(d) / "20260101_000000_hash.jsonl"
            p.write_text("\n".join(json.dumps(o) for o in [
                {"role": "session_meta", "model": "gpt-5.5", "session_id": "h1",
                 "timestamp": "2026-01-01T00:00:00Z", "tools": big_tools},
                {"role": "user", "content": "do it", "timestamp": "2026-01-01T00:01:00Z"},
                {"role": "assistant", "content": "", "timestamp": "2026-01-01T00:02:00Z",
                 "tool_calls": [{"function": {"name": "bash", "arguments": '{"command": "ls"}'}}]},
            ]))
            self.assertGreater(p.stat().st_size, 5000)  # meta line really is large
            meta, msgs, tes, usage = server.parse_hermes(p)
            self.assertEqual(meta["model"], "gpt-5.5")
            self.assertEqual(usage, [])
            self.assertEqual(len(msgs), 2)  # session_meta is NOT a message
            self.assertTrue(any(t["kind"] == "command" and t["command"] == "ls" for t in tes))


class TestLMStudioParser(unittest.TestCase):
    def test_versions(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "1700000000000.conversation.json"
            p.write_text(json.dumps({
                "name": "chat", "createdAt": 1700000000000,
                "messages": [
                    {"currentlySelected": 0, "versions": [
                        {"role": "user", "type": "singleStep",
                         "content": [{"type": "text", "text": "hello"}]}]},
                    {"currentlySelected": 0, "versions": [
                        {"role": "assistant", "type": "multiStep",
                         "steps": [{"type": "contentBlock", "content": [{"type": "text", "text": "hi back"}]}]}]},
                ]}))
            meta, msgs, tes, usage = server.parse_lmstudio(p)
            self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
            self.assertEqual(msgs[0]["text"], "hello")
            self.assertEqual(msgs[1]["text"], "hi back")


def _make_opencode_db(path, sessions):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, data TEXT);
    """)
    for s in sessions:
        conn.execute("INSERT INTO session(id,directory,title,time_created,time_updated)"
                     " VALUES(?,?,?,?,?)",
                     (s["id"], s["directory"], s.get("title", ""),
                      s["time_created"], s["time_updated"]))
        for i, m in enumerate(s["messages"]):
            mid = f'{s["id"]}-m{i}'
            conn.execute("INSERT INTO message(id,session_id,time_created,data) VALUES(?,?,?,?)",
                         (mid, s["id"], i, json.dumps(m["data"])))
            for j, pd in enumerate(m.get("parts", [])):
                conn.execute("INSERT INTO part(id,message_id,session_id,time_created,data)"
                             " VALUES(?,?,?,?,?)",
                             (f"{mid}-p{j}", mid, s["id"], j, json.dumps(pd)))
    conn.commit()
    conn.close()


def _oc_discover(db):
    return lambda: [("opencode", f"{db}#{sid}",
                     (lambda _k, _s=sid: server.parse_opencode_session(db, _s)), mt)
                    for sid, mt in server.opencode_sessions(db)]


class TestOpenCodeParser(unittest.TestCase):
    def _sessions(self):
        return [
            {"id": "A", "directory": "/work/proj-a", "time_created": 1700000000000,
             "time_updated": 1700000100000, "messages": [
                {"data": {"role": "user"}, "parts": [{"type": "text", "text": "fix bug"}]},
                {"data": {"role": "assistant", "modelID": "ollama/qwen", "cost": 0.0,
                          "tokens": {"input": 10, "output": 20, "cache": {"read": 0, "write": 0}}},
                 "parts": [{"type": "text", "text": "on it"},
                           {"type": "tool", "tool": "bash",
                            "state": {"input": {"command": "pytest"}}},
                           {"type": "patch", "files": ["/work/proj-a/x.py"]}]}]},
            {"id": "B", "directory": "/work/proj-b", "time_created": 1700000200000,
             "time_updated": 1700000300000, "messages": [
                {"data": {"role": "user"}, "parts": [{"type": "text", "text": "hello B"}]}]},
        ]

    def test_multi_session(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "opencode.db")
            _make_opencode_db(db, self._sessions())
            conn = fresh_conn()
            orig = server.discover
            try:
                server.discover = _oc_discover(db)
                server.index(conn)
            finally:
                server.discover = orig
            self.assertEqual(counts(conn)["sessions"], 2)
            # session A: 1 command (bash) + 1 file (patch) event
            kinds = [r["kind"] for r in conn.execute("SELECT kind FROM tool_events")]
            self.assertIn("command", kinds)
            self.assertIn("file", kinds)
            # cost passthrough: ollama row recorded with agent source (free => 0.0)
            row = conn.execute("SELECT cost_usd, cost_source FROM usage").fetchone()
            self.assertEqual(row["cost_source"], "agent")
            self.assertAlmostEqual(row["cost_usd"], 0.0)

    def test_no_leak_and_incremental(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "opencode.db")
            _make_opencode_db(db, self._sessions())
            conn = fresh_conn()
            orig = server.discover
            try:
                server.discover = _oc_discover(db)
                s1 = server.index(conn)
                first = counts(conn)
                s2 = server.index(conn, force=True)   # re-parse all
                second = counts(conn)
                self.assertEqual(first, second, "DB re-parse leaked rows")
                self.assertEqual(s1["new"], 2)

                # incrementality: untouched run skips both; bumping one reparses only it
                s3 = server.index(conn)
                self.assertEqual(s3["skipped"], 2)
                wconn = sqlite3.connect(db)
                wconn.execute("UPDATE session SET time_updated=? WHERE id='A'", (1700000900000,))
                wconn.commit(); wconn.close()
                s4 = server.index(conn)
                self.assertEqual(s4["updated"], 1)
                self.assertEqual(s4["skipped"], 1)
            finally:
                server.discover = orig


class TestApiSessionsSort(unittest.TestCase):
    """The `sort` whitelist on /api/sessions (server.api_sessions)."""

    def setUp(self):
        self.conn = fresh_conn()
        # (id, project, cost, msg_count, ended) — chosen so every sort key reorders differently.
        rows = [
            (1, "zebra", 1.0, 10, "2026-01-03T00:00:00Z"),
            (2, "alpha", 5.0, 2,  "2026-01-01T00:00:00Z"),
            (3, "",      0.0, 50, "2026-01-02T00:00:00Z"),  # blank project
        ]
        for sid, proj, cost, mc, ended in rows:
            self.conn.execute(
                "INSERT INTO sessions(id,tool,sid,path,project,cost_usd,msg_count,started,ended)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, "claude", f"s{sid}", f"/p{sid}", proj, cost, mc, ended, ended))
        self.h = make_handler(self.conn)

    def ids(self, sort):
        return [s["id"] for s in self.h.api_sessions({"sort": [sort]})["sessions"]]

    def projects(self, sort):
        return [s["project"] for s in self.h.api_sessions({"sort": [sort]})["sessions"]]

    def test_recent_default_is_ended_desc(self):
        self.assertEqual(self.ids("recent"), [1, 3, 2])
        # omitting the param entirely behaves like recent
        self.assertEqual([s["id"] for s in self.h.api_sessions({})["sessions"]], [1, 3, 2])

    def test_project_alpha_then_blank_last(self):
        self.assertEqual(self.projects("project"), ["alpha", "zebra", ""])

    def test_cost_descending(self):
        self.assertEqual(self.ids("cost"), [2, 1, 3])

    def test_messages_descending(self):
        self.assertEqual(self.ids("messages"), [3, 1, 2])

    def test_unknown_sort_falls_back_to_recent(self):
        self.assertEqual(self.ids("garbage"), self.ids("recent"))

    def test_project_filter_composes_with_sort(self):
        out = self.h.api_sessions({"project": ["alpha"], "sort": ["cost"]})["sessions"]
        self.assertEqual([s["id"] for s in out], [2])


class TestApiSearchRole(unittest.TestCase):
    """The `role` filter on /api/search (server.api_search)."""

    def setUp(self):
        self.conn = fresh_conn()
        self.conn.execute(
            "INSERT INTO sessions(id,tool,sid,path,project) VALUES(1,'claude','s1','/p','proj')")
        for idx, role, text in [
            (0, "user",      "the quick brown fox"),
            (1, "assistant", "lazy dog jumps"),
            (2, "assistant", "fox runs fast"),
        ]:
            self.conn.execute("INSERT INTO fts(text,session_id,idx,role) VALUES(?,?,?,?)",
                              (text, 1, idx, role))
        self.h = make_handler(self.conn)

    def hits(self, **q):
        q.setdefault("q", ["fox"])
        res = self.h.api_search(q)["results"]
        return sum(r["hits"] for r in res), len(res)

    def test_all_roles(self):
        # "fox" appears in one user msg and one assistant msg → 2 hits in 1 session
        self.assertEqual(self.hits(), (2, 1))

    def test_user_only(self):
        self.assertEqual(self.hits(role=["user"]), (1, 1))

    def test_assistant_only(self):
        self.assertEqual(self.hits(role=["assistant"]), (1, 1))

    def test_unknown_role_behaves_like_all(self):
        self.assertEqual(self.hits(role=["bogus"]), (2, 1))

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.h.api_search({"q": [""]})["results"], [])


def _seed_session(conn, sid, path, project="proj", ended="2026-01-01T00:00:00Z"):
    conn.execute(
        "INSERT INTO sessions(tool,sid,path,project,started,ended,msg_count)"
        " VALUES('claude',?,?,?,?,?,1)", (sid, path, project, ended, ended))
    return conn.execute("SELECT id FROM sessions WHERE path=?", (path,)).fetchone()[0]


class TestCollections(unittest.TestCase):
    """Named collections (bookmarks). Membership keys on the stable sessions.path,
    so it must survive a reindex that reassigns sessions.id."""

    def setUp(self):
        self.conn = fresh_conn()
        self.h = make_handler(self.conn)

    # -- create / list --

    def test_create_and_list(self):
        a = self.h.api_collection_create({"name": "Refund work"})
        b = self.h.api_collection_create({"name": "Good prompts"})
        self.assertIn("id", a)
        cols = self.h.api_collections()["collections"]
        names = {c["name"]: c["n"] for c in cols}
        self.assertEqual(names, {"Refund work": 0, "Good prompts": 0})
        self.assertNotEqual(a["id"], b["id"])

    def test_create_trims_and_rejects_empty(self):
        ok = self.h.api_collection_create({"name": "  Spaced  "})
        self.assertEqual(ok["name"], "Spaced")
        bad = self.h.api_collection_create({"name": "   "})
        self.assertIn("error", bad)
        self.assertEqual(len(self.h.api_collections()["collections"]), 1)

    def test_create_duplicate_name_errors(self):
        self.h.api_collection_create({"name": "Dup"})
        dup = self.h.api_collection_create({"name": "Dup"})
        self.assertIn("error", dup)
        self.assertEqual(len(self.h.api_collections()["collections"]), 1)

    # -- membership + filter --

    def test_add_item_filters_sessions(self):
        _seed_session(self.conn, "s1", "/p1")
        _seed_session(self.conn, "s2", "/p2")
        cid = self.h.api_collection_create({"name": "C"})["id"]
        self.h.api_collection_item({"collection_id": cid, "path": "/p1", "op": "add"})
        out = self.h.api_sessions({"collection": [str(cid)]})["sessions"]
        self.assertEqual([s["sid"] for s in out], ["s1"])

    def test_add_item_idempotent(self):
        _seed_session(self.conn, "s1", "/p1")
        cid = self.h.api_collection_create({"name": "C"})["id"]
        self.h.api_collection_item({"collection_id": cid, "path": "/p1", "op": "add"})
        self.h.api_collection_item({"collection_id": cid, "path": "/p1", "op": "add"})
        self.assertEqual(self.h.api_collections()["collections"][0]["n"], 1)

    def test_remove_item(self):
        _seed_session(self.conn, "s1", "/p1")
        cid = self.h.api_collection_create({"name": "C"})["id"]
        self.h.api_collection_item({"collection_id": cid, "path": "/p1", "op": "add"})
        self.h.api_collection_item({"collection_id": cid, "path": "/p1", "op": "remove"})
        self.assertEqual(self.h.api_sessions({"collection": [str(cid)]})["sessions"], [])

    def test_session_collections_lists_memberships(self):
        _seed_session(self.conn, "s1", "/p1")
        c1 = self.h.api_collection_create({"name": "C1"})["id"]
        c2 = self.h.api_collection_create({"name": "C2"})["id"]
        self.h.api_collection_create({"name": "C3"})
        self.h.api_collection_item({"collection_id": c1, "path": "/p1", "op": "add"})
        self.h.api_collection_item({"collection_id": c2, "path": "/p1", "op": "add"})
        ids = set(self.h.api_session_collections({"path": ["/p1"]})["ids"])
        self.assertEqual(ids, {c1, c2})

    def test_bookmarked_paths_is_distinct_union(self):
        _seed_session(self.conn, "s1", "/p1")
        c1 = self.h.api_collection_create({"name": "C1"})["id"]
        c2 = self.h.api_collection_create({"name": "C2"})["id"]
        self.h.api_collection_item({"collection_id": c1, "path": "/p1", "op": "add"})
        self.h.api_collection_item({"collection_id": c2, "path": "/p1", "op": "add"})
        self.h.api_collection_item({"collection_id": c1, "path": "/p2", "op": "add"})
        self.assertEqual(set(self.h.api_bookmarked_paths()["paths"]), {"/p1", "/p2"})

    def test_sessions_row_includes_path(self):
        _seed_session(self.conn, "s1", "/p1")
        out = self.h.api_sessions({})["sessions"]
        self.assertEqual(out[0]["path"], "/p1")

    # -- rename / delete --

    def test_rename(self):
        cid = self.h.api_collection_create({"name": "Old"})["id"]
        self.h.api_collection_rename({"id": cid, "name": "New"})
        self.assertEqual(self.h.api_collections()["collections"][0]["name"], "New")

    def test_delete_cascades_items(self):
        _seed_session(self.conn, "s1", "/p1")
        cid = self.h.api_collection_create({"name": "C"})["id"]
        self.h.api_collection_item({"collection_id": cid, "path": "/p1", "op": "add"})
        self.h.api_collection_delete({"id": cid})
        self.assertEqual(self.h.api_collections()["collections"], [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0], 0)

    # -- edge cases --

    def test_orphan_membership_does_not_break_listing(self):
        # A membership whose session file no longer exists must not crash listing.
        cid = self.h.api_collection_create({"name": "C"})["id"]
        self.h.api_collection_item({"collection_id": cid, "path": "/gone", "op": "add"})
        self.assertEqual(self.h.api_sessions({"collection": [str(cid)]})["sessions"], [])
        self.assertEqual(self.h.api_collections()["collections"][0]["n"], 1)

    # -- the headline invariant --

    def test_membership_survives_reindex(self):
        """Force a full reparse (which delete+reinserts sessions, reassigning ids)
        and assert the session is still in its collection — proving membership keys
        on path, not the volatile sessions.id."""
        with tempfile.TemporaryDirectory() as d:
            def make_parser(sid):
                def parse(_p):
                    meta = {"tool": "claude", "sid": sid, "cwd": "/repo", "project": "repo",
                            "started": "2026-06-01T00:00:00Z", "ended": "2026-06-01T01:00:00Z",
                            "model": "claude-opus-4-8", "git_branch": "main"}
                    return meta, [{"role": "user", "ts": "t", "text": "hi"}], [], []
                return parse

            f1 = Path(d) / "one.jsonl"; f1.write_text("x")
            f2 = Path(d) / "two.jsonl"; f2.write_text("x")
            conn = self.conn
            h = self.h
            orig = server.discover
            try:
                server.discover = lambda: [("claude", str(f1), make_parser("s1"), None),
                                           ("claude", str(f2), make_parser("s2"), None)]
                server.index(conn)
                id_before = conn.execute(
                    "SELECT id FROM sessions WHERE path=?", (str(f2),)).fetchone()[0]
                cid = h.api_collection_create({"name": "Keep"})["id"]
                h.api_collection_item({"collection_id": cid, "path": str(f2), "op": "add"})

                server.index(conn, force=True)   # reassigns ids
                id_after = conn.execute(
                    "SELECT id FROM sessions WHERE path=?", (str(f2),)).fetchone()[0]
            finally:
                server.discover = orig

            self.assertNotEqual(id_before, id_after, "test precondition: id must change")
            out = h.api_sessions({"collection": [str(cid)]})["sessions"]
            self.assertEqual([s["sid"] for s in out], ["s2"])
            self.assertEqual(out[0]["id"], id_after)


if __name__ == "__main__":
    unittest.main()
