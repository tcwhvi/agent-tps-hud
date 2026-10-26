import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tps_overlay as m

NOW = 1_793_012_400_000


def msg(role, completed=None, created=NOW - 5000, finish=None):
    time = {"created": created}
    if completed:
        time["completed"] = completed
    return json.dumps({"role": role, "time": time, "finish": finish})


class AssistantInFlightTests(unittest.TestCase):
    def test_assistant_without_completed_and_recent_is_generating(self):
        rows = [(msg("assistant"), NOW - 3000)]
        self.assertTrue(m.assistant_in_flight(rows, NOW))

    def test_completed_assistant_is_not_generating(self):
        rows = [(msg("assistant", completed=NOW - 4000), NOW - 4000)]
        self.assertFalse(m.assistant_in_flight(rows, NOW))

    def test_stalled_assistant_stops_blinking(self):
        # 客户端中途退出：assistant 永远不会有 completed，靠新鲜度兜底
        rows = [(msg("assistant"), NOW - m.GENERATING_STALE_MS - 1000)]
        self.assertFalse(m.assistant_in_flight(rows, NOW))

    def test_newest_user_message_looks_at_previous_assistant(self):
        rows = [
            (msg("user"), NOW - 1000),
            (msg("assistant", completed=NOW - 2000), NOW - 2000),
        ]
        self.assertFalse(m.assistant_in_flight(rows, NOW))
        rows = [
            (msg("user"), NOW - 1000),
            (msg("assistant"), NOW - 2000),
        ]
        self.assertTrue(m.assistant_in_flight(rows, NOW))

    def test_no_messages_is_not_generating(self):
        self.assertFalse(m.assistant_in_flight([], NOW))

    def test_malformed_row_is_ignored(self):
        rows = [("not json", NOW - 1000), (msg("assistant"), NOW - 2000)]
        self.assertTrue(m.assistant_in_flight(rows, NOW))


class FreshnessTests(unittest.TestCase):
    def test_is_fresh_boundary(self):
        self.assertTrue(m.is_fresh(NOW - m.GENERATING_STALE_MS + 1, NOW))
        self.assertFalse(m.is_fresh(NOW - m.GENERATING_STALE_MS - 1, NOW))
        self.assertFalse(m.is_fresh(0, NOW))


def zcode_db(message_rows, part_rows=()):
    """message_rows: [(id, time_updated, data_json)] newest turn already closed."""
    import sqlite3

    holder = tempfile.TemporaryDirectory()
    db = Path(holder.name) / "zcode.sqlite"
    conn = sqlite3.connect(db)
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
    conn.execute(
        "insert into turn_usage values('t1','s1','completed',1000,2000,1000,100,0,1)"
    )
    conn.execute("insert into model_usage values('t1','m1',1000,100,'completed')")
    conn.execute(
        "insert into message values('m1','s1',1000,2000,?)",
        (json.dumps({"role": "assistant", "time": {"created": 1000, "completed": 2000},
                     "tokens": {"output": 80}}),),
    )
    for mid, updated, data in message_rows:
        conn.execute(
            "insert into message values(?,'s1',?,?,?)", (mid, updated - 1000, updated, data)
        )
    for pid, message_id, updated in part_rows:
        conn.execute(
            "insert into part values(?,?,'s1',?,?,?)",
            (pid, message_id, updated - 500, updated, json.dumps({"type": "text"})),
        )
    conn.commit()
    conn.close()
    return holder, db


class ZcodeGeneratingWiringTests(unittest.TestCase):
    def poll_with(self, message_rows, part_rows=()):
        holder, db = zcode_db(message_rows, part_rows)
        old = m.ZCODE_DB
        m.ZCODE_DB = db
        try:
            return m.ZCodeMonitor().poll()
        finally:
            m.ZCODE_DB = old
            holder.cleanup()

    def test_open_assistant_message_recently_updated_blinks(self):
        now = m.now_ms()
        data = json.dumps({"role": "assistant", "time": {"created": now - 4000}})
        reading = self.poll_with([("x1", now - 1000, data)])
        self.assertTrue(reading.generating)

    def test_abandoned_open_message_stops_blinking(self):
        now = m.now_ms()
        stale = now - m.GENERATING_STALE_MS - 5000
        data = json.dumps({"role": "assistant", "time": {"created": stale}})
        reading = self.poll_with([("x1", stale, data)])
        self.assertFalse(reading.generating)

    def test_long_turn_keeps_blinking_via_parts(self):
        # 消息行创建后 time_updated 就不动了，长回答靠 part 推进
        now = m.now_ms()
        created = now - m.GENERATING_STALE_MS - 5000
        data = json.dumps({"role": "assistant", "time": {"created": created}})
        reading = self.poll_with(
            [("x1", created, data)], [("p1", "x1", now - 5000)]
        )
        self.assertTrue(reading.generating)

    def test_closed_turn_is_not_generating(self):
        reading = self.poll_with([])
        self.assertFalse(reading.generating)


if __name__ == "__main__":
    unittest.main()
