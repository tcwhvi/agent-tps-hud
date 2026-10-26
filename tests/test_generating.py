import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tps_overlay as m

NOW = 1_793_012_400_000


class FreshnessTests(unittest.TestCase):
    def test_is_fresh_boundary(self):
        self.assertTrue(m.is_fresh(NOW - m.GENERATING_STALE_MS + 1, NOW))
        self.assertFalse(m.is_fresh(NOW - m.GENERATING_STALE_MS - 1, NOW))
        self.assertFalse(m.is_fresh(0, NOW))


def build_db(sessions, turns, messages, parts=(), model_rows=()):
    """sessions: [(id, parent_id)]  turns: [(id, session, status, started, completed)]
    messages: [(id, session, time_updated, role, completed)]  parts: [(id, msg, session, t)]
    model_rows: [(turn_id, message_id, duration_ms, output_tokens)]"""
    holder = tempfile.TemporaryDirectory()
    db = Path(holder.name) / "zcode.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "create table session(id text primary key, parent_id text, title text)"
    )
    conn.execute(
        "create table turn_usage(turn_id text, session_id text, status text, "
        "started_at integer, completed_at integer, duration_ms integer, "
        "output_tokens integer, reasoning_tokens integer, model_request_count integer)"
    )
    conn.execute(
        "create table model_usage(turn_id text, assistant_message_id text, "
        "duration_ms integer, output_tokens integer, status text)"
    )
    conn.execute(
        "create table message(id text, session_id text, time_created integer, "
        "time_updated integer, data text)"
    )
    conn.execute(
        "create table part(id text, message_id text, session_id text, "
        "time_created integer, time_updated integer, data text)"
    )
    for sid, parent in sessions:
        conn.execute("insert into session values(?,?,?)", (sid, parent, sid))
    for turn_id, session, status, started, completed in turns:
        conn.execute(
            "insert into turn_usage values(?,?,?,?,?,100,0,0,1)",
            (turn_id, session, status, started, completed),
        )
    for mid, session, updated, role, completed in messages:
        data = {"role": role, "time": {"created": updated - 1000}}
        if completed:
            data["time"]["completed"] = completed
        conn.execute(
            "insert into message values(?,?,?,?,?)",
            (mid, session, updated - 1000, updated, json.dumps(data)),
        )
    for pid, mid, session, updated in parts:
        conn.execute(
            "insert into part values(?,?,?,?,?,?)",
            (pid, mid, session, updated - 500, updated, json.dumps({"type": "text"})),
        )
    for turn_id, message_id, duration, output in model_rows:
        conn.execute(
            "insert into model_usage values(?,?,?,?, 'completed')",
            (turn_id, message_id, duration, output),
        )
    conn.commit()
    conn.close()
    return holder, db


class ZcodeGeneratingTests(unittest.TestCase):
    def poll(self, sessions, turns, messages, parts=(), model_rows=()):
        holder, db = build_db(sessions, turns, messages, parts, model_rows)
        old = m.ZCODE_DB
        m.ZCODE_DB = db
        try:
            return m.ZCodeMonitor().poll()
        finally:
            m.ZCODE_DB = old
            holder.cleanup()

    def test_open_turn_on_main_session_blinks(self):
        now = m.now_ms()
        reading = self.poll(
            sessions=[("main", None)],
            turns=[("t1", "main", "completed", now - 9000, now - 8000)],
            messages=[("m1", "main", now - 1000, "assistant", None)],
        )
        self.assertTrue(reading.generating)
        self.assertEqual(reading.session_key, "main")

    def test_closed_turn_with_nothing_newer_does_not_blink(self):
        now = m.now_ms()
        reading = self.poll(
            sessions=[("main", None)],
            turns=[("t1", "main", "completed", now - 9000, now - 1000)],
            messages=[("m1", "main", now - 1000, "assistant", now - 1000)],
        )
        self.assertFalse(reading.generating)

    def test_subagent_activity_keeps_main_session_blinking(self):
        # 主会话 300 秒没写，子代理刚写 —— 主代理在派活等待，仍算生成中
        now = m.now_ms()
        reading = self.poll(
            sessions=[("main", None), ("sub1", "main")],
            turns=[("t1", "main", "completed", now - 400_000, now - 300_000)],
            messages=[
                ("m1", "main", now - 300_000, "assistant", None),
                ("m2", "sub1", now - 3000, "assistant", None),
            ],
        )
        self.assertTrue(reading.generating)
        self.assertEqual(reading.session_key, "main")

    def test_row_follows_main_session_not_the_newest_subagent(self):
        # 子代理不停收尾，turn_usage 最新行属于子代理；读数必须跟主会话
        now = m.now_ms()
        reading = self.poll(
            sessions=[("main", None), ("sub1", "main")],
            turns=[
                ("t1", "main", "completed", now - 50_000, now - 40_000),
                ("t2", "sub1", "completed", now - 5000, now - 2000),
            ],
            messages=[
                ("m1", "main", now - 40_000, "assistant", now - 40_000),
                ("m2", "sub1", now - 2000, "assistant", now - 2000),
            ],
            model_rows=[("t1", "m1", 2000, 200), ("t2", "m2", 500, 5000)],
        )
        self.assertEqual(reading.session_key, "main")
        self.assertIn("1 turns", reading.note)
        # 主会话这一轮 200 token / 2s = 100 TPS；子代理的 5000 token 不算进来
        self.assertAlmostEqual(reading.tps, 100.0, places=3)

    def test_first_turn_in_flight_still_blinks(self):
        # 主会话还没有任何已完成轮次，走的是"暂无完成的轮次"分支
        now = m.now_ms()
        reading = self.poll(
            sessions=[("main", None)],
            turns=[],
            messages=[("m1", "main", now - 1000, "assistant", None)],
        )
        self.assertTrue(reading.generating)
        self.assertEqual(reading.session_key, "main")
        self.assertIsNone(reading.tps)

    def test_stalled_everything_stops_blinking(self):
        now = m.now_ms()
        stale = now - m.GENERATING_STALE_MS - 5000
        reading = self.poll(
            sessions=[("main", None), ("sub1", "main")],
            turns=[("t1", "main", "completed", stale - 1000, stale)],
            messages=[("m1", "main", stale, "assistant", None)],
        )
        self.assertFalse(reading.generating)

    def test_part_write_keeps_long_model_request_blinking(self):
        # 单次模型请求很久：消息行时间不动，靠 part 推进
        now = m.now_ms()
        reading = self.poll(
            sessions=[("main", None)],
            turns=[("t1", "main", "completed", now - 400_000, now - 390_000)],
            messages=[("m1", "main", now - 200_000, "assistant", None)],
            parts=[("p1", "m1", "main", now - 5000)],
        )
        self.assertTrue(reading.generating)


if __name__ == "__main__":
    unittest.main()
