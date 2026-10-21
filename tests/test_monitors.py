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
                    "message": {
                        "usage": {
                            "input_tokens": 1000,
                            "output_tokens": 100,
                            "cache_read_input_tokens": 200,
                        }
                    },
                    "providerData": {"reasoning": "x" * 1200},
                },
            ]
            path.write_text(
                "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
            )
            monitor = m.WorkBuddyMonitor()
            monitor._parse(path)
            self.assertEqual(monitor.session_tokens, 100.0)
            self.assertEqual(monitor.session_input, 800.0)
            self.assertEqual(monitor.session_cache, 200.0)

    def test_qoder_ide_tokens_and_title(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "local.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "create table chat_message(session_id text, role text, "
                "token_info text, gmt_create integer)"
            )
            conn.execute(
                "create table chat_session(session_id text, session_title text)"
            )
            conn.execute(
                "insert into chat_session values('s1','IDE session title')"
            )
            conn.execute(
                "insert into chat_message values('s1','assistant',?,1000)",
                (
                    json.dumps(
                        {
                            "prompt_tokens": 500,
                            "completion_tokens": 100,
                            "cached_tokens": 100,
                        }
                    ),
                ),
            )
            conn.execute(
                "insert into chat_message values('s1','assistant',?,2000)",
                (json.dumps({"completion_tokens": 50}),),
            )
            conn.commit()
            conn.close()

            old = m.QODER_DBS
            m.QODER_DBS = [db]
            try:
                stats = m.QoderMonitor()._ide_stats(0)
            finally:
                m.QODER_DBS = old

            self.assertEqual(stats["s1"].tokens, 150.0)
            self.assertEqual(stats["s1"].input_tokens, 400.0)
            self.assertEqual(stats["s1"].cache_tokens, 100.0)
            self.assertEqual(stats["s1"].calls, 2)
            self.assertEqual(stats["s1"].title, "IDE session title")

    def test_qoder_ide_tokens_override_transcript_estimate(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            db = folder / "local.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "create table chat_message(session_id text, role text, "
                "token_info text, gmt_create integer)"
            )
            conn.execute(
                "create table chat_session(session_id text, session_title text)"
            )
            conn.execute("insert into chat_session values('s1','IDE exact title')")
            conn.execute(
                "insert into chat_message values('s1','assistant',?,1000)",
                (json.dumps({"completion_tokens": 150}),),
            )
            conn.commit()
            conn.close()

            root = folder / "projects" / "p"
            root.mkdir(parents=True)
            transcript = root / "s1.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "type": "user",
                            "timestamp": 1000,
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "prompt"}],
                            },
                        },
                        {
                            "type": "assistant",
                            "timestamp": 2000,
                            "message": {
                                "id": "m1",
                                "stop_reason": "end_turn",
                                "content": [{"type": "text", "text": "x" * 100}],
                            },
                        },
                    ]
                ),
                encoding="utf-8",
            )

            old_roots, old_dbs = m.QODER_ROOTS, m.QODER_DBS
            m.QODER_ROOTS = [folder / "projects"]
            m.QODER_DBS = [db]
            m.QODER_FILE_CACHE.clear()
            try:
                stats = m.QoderMonitor().session_stats(0)
            finally:
                m.QODER_ROOTS = old_roots
                m.QODER_DBS = old_dbs
                m.QODER_FILE_CACHE.clear()

            stat = next(item for item in stats if item.session_id == "s1")
            self.assertEqual(stat.tokens, 150.0)
            self.assertEqual(stat.title, "IDE exact title")
            self.assertFalse(stat.approximate)
            self.assertIsNotNone(stat.tps)

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
