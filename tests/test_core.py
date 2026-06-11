"""Core invariant tests for ai-session-browser. Stdlib only: `python3 -m unittest`."""
import os
import sqlite3
import sys
import tempfile
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
                server.discover = lambda: [("claude", fpath, fake_parser)]
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


if __name__ == "__main__":
    unittest.main()
