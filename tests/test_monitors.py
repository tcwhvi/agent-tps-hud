import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tps_overlay as m


class MonitorTests(unittest.TestCase):
    def test_zcode_uses_message_output_without_extra_reasoning(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "zcode.sqlite"
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
            conn.execute("create table message(id text, data text)")
            conn.execute(
                "insert into turn_usage values('t1','s1','completed',1000,2000,1000,100,0,1)"
            )
            conn.execute(
                "insert into model_usage values('t1','m1',1000,100,'completed')"
            )
            conn.execute(
                "insert into message values('m1',?)",
                (json.dumps({"tokens": {"output": 80}}),),
            )
            conn.commit()
            conn.close()

            old = m.ZCODE_DB
            m.ZCODE_DB = db
            try:
                reading = m.ZCodeMonitor().poll()
            finally:
                m.ZCODE_DB = old

            self.assertEqual(reading.history, (80.0,))

    def test_workbuddy_output_already_contains_reasoning(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.jsonl"
            rows = [
                {"type": "message", "role": "user", "timestamp": 1000},
                {
                    "type": "message",
                    "role": "assistant",
                    "timestamp": 2000,
                    "message": {"usage": {"output_tokens": 100}},
                    "providerData": {"reasoning": "x" * 1200},
                },
            ]
            path.write_text(
                "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
            )
            monitor = m.WorkBuddyMonitor()
            monitor._parse(path)
            self.assertEqual(monitor.session_tokens, 100.0)

    def test_qoder_turn_closes_at_next_prompt_and_derives_title(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.jsonl"
            records = [
                {
                    "type": "user",
                    "timestamp": 1000,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Fix cache warmup"}],
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": 2000,
                    "message": {
                        "id": "m1",
                        "stop_reason": "tool_use",
                        "content": [{"type": "text", "text": "working"}],
                    },
                },
                {
                    "type": "user",
                    "timestamp": 3000,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Interject"}],
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": 4000,
                    "message": {
                        "id": "m2",
                        "stop_reason": "tool_use",
                        "content": [{"type": "text", "text": "still working"}],
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(row) for row in records), encoding="utf-8"
            )
            monitor = m.QoderMonitor()
            monitor._parse(path)
            self.assertEqual(monitor.session_title, "Fix cache warmup")
            self.assertEqual(len(monitor.history_values), 1)


if __name__ == "__main__":
    unittest.main()
