#!/usr/bin/env python3
"""Local TPS overlay for Qoder / WorkBuddy / MiMo / ZCode.

Read-only: it only reads numeric metadata (token counts, timestamps, text lengths)
from local session stores. It never prints or persists prompt/response content.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    import ctypes
    from ctypes import wintypes
except Exception:
    ctypes = None
    wintypes = None

HOME = Path.home()
APPDATA = Path(os.environ.get("APPDATA", str(HOME / "AppData" / "Roaming")))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
QODER_DBS = [
    APPDATA / "QoderCN" / "SharedClientCache" / "cache" / "db" / "local.db",
    APPDATA / "Qoder" / "SharedClientCache" / "cache" / "db" / "local.db",
]
QODER_ROOTS = [HOME / ".qoder-cn" / "projects", HOME / ".qoder" / "projects"]
ZCODE_DB = HOME / ".zcode" / "cli" / "db" / "db.sqlite"
MIMO_DB = HOME / ".local" / "share" / "mimocode" / "mimocode.db"
WORKBUDDY_ROOT = HOME / ".workbuddy" / "projects"
WORKBUDDY_DB = HOME / ".workbuddy" / "workbuddy.db"
QODER_FILE_CACHE: dict = {}
WORKBUDDY_FILE_CACHE: dict = {}
SESSION_CACHE_LIMIT = 400


def now_ms() -> int:
    return int(time.time() * 1000)


# A turn that has not written anything for this long is a client that went away
# mid-generation, not one that is still streaming.
GENERATING_STALE_MS = 120_000


def is_fresh(timestamp_ms, now: int) -> bool:
    return bool(timestamp_ms) and (now - int(timestamp_ms)) < GENERATING_STALE_MS


SNAPSHOT_DIRS = []


def _cleanup_snapshots() -> None:
    for folder in SNAPSHOT_DIRS:
        shutil.rmtree(folder, ignore_errors=True)


atexit.register(_cleanup_snapshots)


def _snapshot_connection(path: Path) -> sqlite3.Connection:
    folder = tempfile.mkdtemp(prefix="agent-tps-hud-sqlite-")
    SNAPSHOT_DIRS.append(folder)
    target = Path(folder) / path.name
    shutil.copy2(path, target)
    for suffix in ("-wal", "-shm"):
        source = Path(f"{path}{suffix}")
        if source.exists():
            shutil.copy2(source, Path(f"{target}{suffix}"))
    conn = sqlite3.connect(target, timeout=0.5)
    conn.execute("PRAGMA query_only=ON")
    return conn


def open_ro(path: Path) -> sqlite3.Connection:
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=0.5
        )
        conn.execute("select name from sqlite_master limit 1").fetchall()
        return conn
    except sqlite3.OperationalError as exc:
        if conn is not None:
            conn.close()
        if "locked" not in str(exc).lower():
            raise
        # The source app can hold a write lock; read an isolated copy instead.
        return _snapshot_connection(path)


def file_cache_key(path: Path):
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def remember_file(cache: dict, key, values) -> None:
    if len(cache) >= SESSION_CACHE_LIMIT:
        cache.clear()
    cache[key] = values


def jload(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def model_name(value) -> str:
    obj = jload(value)
    if not isinstance(obj, dict):
        return ""
    for key in (
        "requestModelName",
        "modelID",
        "modelId",
        "model",
        "modelName",
        "displayName",
        "name",
        "modelKey",
        "key",
    ):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
        or 0x3040 <= o <= 0x30FF
        or 0xAC00 <= o <= 0xD7AF
        or 0xF900 <= o <= 0xFAFF
    )


def estimate_tokens(text: str) -> float:
    """Rough live estimate: CJK ~1 token/char, other ~3.6 chars/token."""
    if not text:
        return 0.0
    cjk = 0
    other = 0
    for ch in text:
        if ch.isspace():
            continue
        if is_cjk(ch):
            cjk += 1
        else:
            other += 1
    return cjk + other / 3.6


@dataclass
class Reading:
    client: str
    tps: Optional[float]
    mode: str
    model: str = ""
    active: bool = False
    note: str = ""
    generating: bool = False
    sample_key: str = ""
    session_key: str = ""
    history: tuple[float, ...] = ()


@dataclass
class SessionStat:
    client: str
    session_id: str
    title: str
    start_ms: int
    end_ms: int
    tokens: float
    model_ms: int
    calls: int
    approximate: bool = False
    input_tokens: float = 0.0
    cache_tokens: float = 0.0
    subagents: int = 0

    @property
    def tps(self) -> Optional[float]:
        if self.model_ms <= 0 or self.tokens <= 0:
            return None
        return self.tokens / (self.model_ms / 1000.0)


def period_cutoff(period: str) -> int:
    now = datetime.now()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "7d":
        start = now - timedelta(days=7)
    else:
        start = now - timedelta(days=30)
    return int(start.timestamp() * 1000)


def fmt_tokens(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{int(value)}"


def fmt_tps(value: Optional[float], approximate: bool = False) -> str:
    if value is None:
        return "—"
    text = f"{value:.1f}" if value < 100 else f"{value:.0f}"
    return ("≈" if approximate else "") + text


def fmt_time(ms: int) -> str:
    if not ms:
        return "—"
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return "—"


class MimoMonitor:
    def __init__(self) -> None:
        self.stats_cache = {}
        self.history_values = []
        self.generating = False

    def _message_stats(self, conn, mid, msg_tc, msg_tu):
        cache_key = (mid, msg_tu)
        cached = self.stats_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            rows = conn.execute(
                "select time_created,time_updated,data from part "
                "where message_id=? order by time_created asc",
                (mid,),
            ).fetchall()
        except Exception:
            return (0.0, 0, 0, 0)

        parsed = []
        for tc, tu, data in rows:
            d = jload(data) or {}
            parsed.append(
                {
                    "tc": tc,
                    "tu": tu,
                    "type": d.get("type"),
                    "data": d,
                    "text": d.get("text") if isinstance(d.get("text"), str) else "",
                }
            )

        steps = []
        current = None
        for part in parsed:
            if part["type"] == "step-start":
                if current:
                    steps.append(current)
                current = {"start": part["tc"], "texts": [], "finish": None}
            elif part["type"] in ("text", "reasoning"):
                if current is None:
                    current = {"start": msg_tc, "texts": [], "finish": None}
                current["texts"].append(part)
            elif part["type"] == "step-finish" and current is not None:
                current["finish"] = part
        if current:
            steps.append(current)

        total_tokens = 0.0
        total_input = 0.0
        total_cache = 0.0
        total_ms = 0
        last_ts = 0
        calls = 0
        for index, step in enumerate(steps):
            finish = step["finish"]
            tokens = 0
            if finish:
                values = finish["data"].get("tokens") or {}
                tokens = (values.get("output") or 0) + (values.get("reasoning") or 0)
                total_input += values.get("input") or 0
                cache = values.get("cache") or {}
                total_cache += cache.get("read") or 0
            if not tokens and step["texts"]:
                tokens = estimate_tokens("".join(p["text"] for p in step["texts"]))
            if not tokens:
                continue
            start = msg_tc if index == 0 else step["start"]
            if step["texts"]:
                end = max(p["tu"] for p in step["texts"])
            elif finish:
                end = finish["tu"]
            else:
                end = msg_tu
            duration = max(0, end - start)
            total_tokens += tokens
            total_ms += duration
            last_ts = max(last_ts, end)
            calls += 1

        result = (total_tokens, total_ms, last_ts, calls, total_input, total_cache)
        if len(self.stats_cache) > 800:
            self.stats_cache.clear()
        self.stats_cache[cache_key] = result
        return result

    def poll(self) -> Reading:
        if not MIMO_DB.exists():
            return Reading("MiMo", None, "每轮", note="未找到 mimocode.db")
        conn = None
        turns = []
        current = None
        try:
            conn = open_ro(MIMO_DB)
            session = conn.execute(
                "select id from session order by time_updated desc limit 1"
            ).fetchone()
            if not session:
                return Reading("MiMo", None, "每轮", note="暂无会话")
            session_key = str(session[0])
            messages = conn.execute(
                "select id,time_created,time_updated,data from message "
                "where session_id=? order by time_created asc limit 200",
                (session[0],),
            ).fetchall()
            for mid, tc, tu, data in messages:
                parsed = jload(data) or {}
                role = parsed.get("role")
                if role == "user":
                    if current:
                        turns.append(current)
                    current = {
                        "tokens": 0.0,
                        "model_ms": 0,
                        "calls": 0,
                        "last_ts": tc,
                        "complete": False,
                        "has_assistant": False,
                    }
                    continue
                if role != "assistant":
                    continue
                if current is None:
                    current = {
                        "tokens": 0.0,
                        "model_ms": 0,
                        "calls": 0,
                        "last_ts": tc,
                        "complete": False,
                        "has_assistant": False,
                    }
                tokens, model_ms, last_ts, calls, _input, _cache = self._message_stats(
                    conn, mid, tc, tu
                )
                current["tokens"] += tokens
                current["model_ms"] += model_ms
                current["calls"] += calls
                current["last_ts"] = max(current["last_ts"], last_ts or tu)
                current["complete"] = parsed.get("finish") == "stop"
                current["has_assistant"] = True
        except Exception as exc:
            return Reading("MiMo", None, "每轮", note=f"读取失败: {type(exc).__name__}")
        finally:
            if conn is not None:
                conn.close()

        if current:
            turns.append(current)

        closed = turns[:-1] + (
            [turns[-1]] if turns and turns[-1]["complete"] else []
        )
        completed = [
            turn
            for turn in closed
            if turn["model_ms"] > 0 and turn["tokens"] > 0
        ]
        history = []
        for turn in completed:
            tps = turn["tokens"] / (turn["model_ms"] / 1000.0)
            if 0 < tps < 500:
                history.append(tps)
        self.history_values = history
        last_turn = turns[-1] if turns else None
        self.generating = bool(
            last_turn
            and last_turn["has_assistant"]
            and not last_turn["complete"]
            and is_fresh(last_turn["last_ts"], now_ms())
        )

        if completed:
            total_tokens = sum(turn["tokens"] for turn in completed)
            total_ms = sum(turn["model_ms"] for turn in completed)
            total_calls = sum(turn["calls"] for turn in completed)
            session_tps = total_tokens / (total_ms / 1000.0)
            return Reading(
                "MiMo",
                session_tps,
                "每轮",
                "",
                self.generating,
                f"{int(total_tokens)} tok / {total_ms / 1000:.1f}s · "
                f"{len(completed)} turns / {total_calls} calls",
                self.generating,
                sample_key=str(completed[-1]["last_ts"]),
                session_key=session_key,
                history=tuple(history[-24:]),
            )
        return Reading("MiMo", None, "每轮", "", self.generating, "暂无完成的轮次", self.generating)

    def session_stats(self, since_ms: int) -> list[SessionStat]:
        if not MIMO_DB.exists():
            return []
        conn = None
        stats = []
        try:
            conn = open_ro(MIMO_DB)
            sessions = conn.execute(
                "select id,title,time_created,time_updated from session "
                "where time_updated>=? order by time_updated desc limit 120",
                (since_ms,),
            ).fetchall()
            for sid, title, created, updated in sessions:
                messages = conn.execute(
                    "select id,time_created,time_updated,data from message "
                    "where session_id=? order by time_created asc",
                    (sid,),
                ).fetchall()
                tokens = 0.0
                input_tokens = 0.0
                cache_tokens = 0.0
                model_ms = 0
                calls = 0
                start = created or 0
                end = updated or 0
                for mid, tc, tu, data in messages:
                    parsed = jload(data) or {}
                    if parsed.get("role") != "assistant":
                        continue
                    value = self._message_stats(conn, mid, tc, tu)
                    tokens += value[0]
                    model_ms += value[1]
                    calls += value[3]
                    input_tokens += value[4]
                    cache_tokens += value[5]
                    start = min(start or tc, tc)
                    end = max(end, value[2] or tu or tc)
                if tokens > 0 and model_ms > 0:
                    stats.append(
                        SessionStat(
                            "MiMo",
                            str(sid),
                            (title or "MiMo 会话").replace("\n", " "),
                            int(start),
                            int(end),
                            tokens,
                            model_ms,
                            calls,
                            input_tokens=input_tokens,
                            cache_tokens=cache_tokens,
                        )
                    )
        except Exception:
            return []
        finally:
            if conn is not None:
                conn.close()
        return stats


class QoderMonitor:
    def __init__(self) -> None:
        self.cache_path: Optional[Path] = None
        self.cache_mtime: Optional[int] = None
        self.latest_tps: Optional[float] = None
        self.model = ""
        self.note = ""
        self.last_stop = None
        self.last_event_type = None
        self.sample_key = ""
        self.history_values = []
        self.turn_points = []
        self.session_tokens = 0.0
        self.session_ms = 0
        self.session_calls = 0
        self.session_turns = 0
        self.session_start = 0
        self.session_end = 0
        self.session_title = ""

    def _latest_transcript(self) -> Optional[Path]:
        files = []
        for root in QODER_ROOTS:
            if not root.exists():
                continue
            for p in root.rglob("*.jsonl"):
                if "subagents" in p.parts:
                    continue
                files.append(p)
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    @staticmethod
    def _ts(value):
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                return None
        return None

    @staticmethod
    def _prompt_title(content) -> str:
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    value = block.get("text")
                    if isinstance(value, str) and value.strip():
                        text = value
                        break
        text = " ".join(text.split())
        return text[:40]

    def _parse(self, path: Path) -> None:
        self.latest_tps = None
        self.model = ""
        self.note = ""
        self.last_stop = None
        self.last_event_type = None
        self.sample_key = ""
        self.history_values = []
        self.session_tokens = 0.0
        self.session_ms = 0
        self.session_calls = 0
        self.session_start = 0
        self.session_end = 0
        self.session_title = ""

        groups = {}
        anchors = []
        prompts = []
        last_stop = None

        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    typ = obj.get("type")
                    ts = self._ts(obj.get("timestamp"))
                    if typ == "user":
                        self.last_event_type = "user"
                        if ts:
                            anchors.append(ts)
                        content = (obj.get("message") or {}).get("content")
                        is_tool_result = isinstance(content, list) and any(
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                            for block in content
                        )
                        if ts and not is_tool_result:
                            prompts.append(ts)
                            if not self.session_title:
                                self.session_title = self._prompt_title(content)
                        continue
                    if typ in ("tool", "function_call_result"):
                        self.last_event_type = typ
                        if ts:
                            anchors.append(ts)
                        continue
                    if typ != "assistant":
                        continue
                    self.last_event_type = "assistant"
                    message = obj.get("message") or {}
                    mid = message.get("id") or obj.get("uuid") or f"ts{ts}"
                    group = groups.setdefault(
                        mid,
                        {
                            "first_ts": ts,
                            "last_ts": ts,
                            "tokens": 0.0,
                            "usage": 0,
                            "seen": set(),
                            "stop": None,
                        },
                    )
                    if ts:
                        group["first_ts"] = min(group["first_ts"] or ts, ts)
                        group["last_ts"] = max(group["last_ts"] or ts, ts)
                    stop = message.get("stop_reason")
                    if stop:
                        group["stop"] = stop
                    last_stop = stop or last_stop
                    usage = message.get("usage") or {}
                    output = usage.get("output_tokens") or 0
                    if output:
                        group["usage"] = max(group["usage"], int(output))
                    content = message.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            block_type = block.get("type")
                            if block_type == "thinking":
                                text = block.get("thinking")
                            elif block_type == "text":
                                text = block.get("text")
                            else:
                                continue
                            if isinstance(text, str) and text:
                                key = (block_type, text)
                                if key not in group["seen"]:
                                    group["seen"].add(key)
                                    group["tokens"] += estimate_tokens(text)
            self.last_stop = last_stop
        except Exception:
            pass

        prompts = sorted(set(prompts))
        anchors = sorted(set(anchors))
        turns = {}
        for group in groups.values():
            if not group["usage"] and not group["tokens"]:
                continue
            first_ts = group["first_ts"]
            last_ts = group["last_ts"]
            if not first_ts or not last_ts:
                continue
            prompt = None
            for value in prompts:
                if value <= first_ts:
                    prompt = value
                else:
                    break
            if prompt is None:
                continue
            anchor = None
            for value in anchors:
                if value <= first_ts:
                    anchor = value
                else:
                    break
            if anchor is None:
                anchor = prompt
            duration = last_ts - anchor
            if duration <= 0:
                continue
            tokens = group["usage"] or group["tokens"]
            if not tokens:
                continue
            turn = turns.setdefault(
                prompt,
                {
                    "tokens": 0.0,
                    "model_ms": 0,
                    "calls": 0,
                    "last_ts": 0,
                    "last_stop": None,
                },
            )
            turn["tokens"] += tokens
            turn["model_ms"] += duration
            turn["calls"] += 1
            turn["last_ts"] = max(turn["last_ts"], last_ts)
            if group.get("stop"):
                turn["last_stop"] = group["stop"]

        history = []
        latest = None
        session_tokens = 0.0
        session_ms = 0
        session_calls = 0
        session_turns = 0
        session_start = 0
        session_end = 0
        latest_prompt = prompts[-1] if prompts else None
        points = []
        for prompt in sorted(turns):
            turn = turns[prompt]
            closed = turn["last_stop"] == "end_turn" or prompt != latest_prompt
            if not closed or turn["model_ms"] <= 0:
                continue
            tps = turn["tokens"] / (turn["model_ms"] / 1000.0)
            if 0 < tps < 2000:
                history.append(tps)
                points.append((prompt, tps))
                session_tokens += turn["tokens"]
                session_ms += turn["model_ms"]
                session_calls += turn["calls"]
                session_turns += 1
                session_start = prompt if not session_start else min(session_start, prompt)
                session_end = max(session_end, turn["last_ts"])
                latest = (
                    tps,
                    turn["last_ts"],
                    prompt,
                    turn["tokens"],
                    turn["model_ms"],
                    turn["calls"],
                )

        latest_turn = turns.get(latest_prompt)
        self.generating = bool(
            latest_turn
            and latest_turn["calls"] > 0
            and latest_turn["last_stop"] != "end_turn"
        )

        if session_ms > 0:
            self.latest_tps = session_tokens / (session_ms / 1000.0)
            self.note = (
                f"≈{int(session_tokens)} tok / {session_ms / 1000:.1f}s · "
                f"{session_turns} turns / {session_calls} calls"
            )
            self.sample_key = str(path)
            self.history_values = history
            self.turn_points = points
            self.session_tokens = session_tokens
            self.session_ms = session_ms
            self.session_calls = session_calls
            self.session_turns = session_turns
            self.session_start = session_start
            self.session_end = session_end
        self._fold_subagents(path)

    @staticmethod
    def _family_files(path: Path) -> list:
        folder = path.with_suffix("") / "subagents"
        try:
            return [p for p in sorted(folder.glob("*.jsonl")) if p.is_file()]
        except OSError:
            return []

    @classmethod
    def _family_mtime_ms(cls, path: Path) -> int:
        times = []
        for item in [path, *cls._family_files(path)]:
            try:
                times.append(int(item.stat().st_mtime * 1000))
            except OSError:
                continue
        return max(times) if times else 0

    def _fold_subagents(self, path: Path) -> None:
        """One row = one task: merge this session's subagent transcripts in.

        A main session that only orchestrates subagents closes no turn of its
        own, so without this the row reads blank while the task is running.
        """

        points = list(self.turn_points)
        generating = bool(self.generating)
        tokens = self.session_tokens
        model_ms = self.session_ms
        calls = self.session_calls
        turns = self.session_turns
        start = self.session_start
        end = self.session_end
        for child in self._family_files(path):
            extra = QoderMonitor()
            try:
                extra._parse(child)
            except Exception:
                continue
            generating = generating or bool(extra.generating)
            if not extra.session_ms:
                continue
            points += list(extra.turn_points)
            tokens += extra.session_tokens
            model_ms += extra.session_ms
            calls += extra.session_calls
            turns += extra.session_turns
            if extra.session_start:
                start = min(start or extra.session_start, extra.session_start)
            end = max(end, extra.session_end)
        self.turn_points = points
        self.generating = generating
        self.history_values = [tps for _prompt, tps in sorted(points)]
        self.session_tokens = tokens
        self.session_ms = model_ms
        self.session_calls = calls
        self.session_turns = turns
        self.session_start = start
        self.session_end = end
        if model_ms > 0:
            self.latest_tps = tokens / (model_ms / 1000.0)
            self.note = (
                f"≈{int(tokens)} tok / {model_ms / 1000:.1f}s · "
                f"{turns} turns / {calls} calls"
            )

    @staticmethod
    def _ide_usage(value) -> tuple:
        info = jload(value) or {}
        try:
            output = max(0, int(info.get("completion_tokens") or 0))
            prompt = max(0, int(info.get("prompt_tokens") or 0))
            cached = max(0, int(info.get("cached_tokens") or 0))
            return output, max(0, prompt - cached), cached
        except Exception:
            return 0, 0, 0

    def _ide_stats(self, since_ms: int) -> dict:
        groups = {}
        for db in QODER_DBS:
            if not db.exists():
                continue
            conn = None
            try:
                conn = open_ro(db)
                try:
                    rows = conn.execute(
                        "select cm.session_id,cm.token_info,cm.gmt_create,"
                        "cs.session_title from chat_message cm "
                        "left join chat_session cs on cs.session_id=cm.session_id "
                        "where cm.role='assistant' and cm.token_info is not null "
                        "and cm.gmt_create>=?",
                        (since_ms,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = conn.execute(
                        "select session_id,token_info,gmt_create,null "
                        "from chat_message where role='assistant' "
                        "and token_info is not null and gmt_create>=?",
                        (since_ms,),
                    ).fetchall()
                for sid, token_info, created, title in rows:
                    if not sid:
                        continue
                    tokens, input_tokens, cache_tokens = self._ide_usage(token_info)
                    if tokens <= 0:
                        continue
                    entry = groups.setdefault(
                        str(sid),
                        {
                            "tokens": 0.0,
                            "input": 0.0,
                            "cache": 0.0,
                            "calls": 0,
                            "start": 0,
                            "end": 0,
                            "title": "",
                        },
                    )
                    entry["tokens"] += tokens
                    entry["input"] += input_tokens
                    entry["cache"] += cache_tokens
                    entry["calls"] += 1
                    stamp = int(created or 0)
                    entry["start"] = stamp if not entry["start"] else min(entry["start"], stamp)
                    entry["end"] = max(entry["end"], stamp)
                    if not entry["title"] and title:
                        entry["title"] = str(title).replace("\n", " ")[:80]
            except Exception:
                continue
            finally:
                if conn is not None:
                    conn.close()
        return {
            sid: SessionStat(
                "Qoder",
                sid,
                entry["title"] or "Qoder IDE 会话",
                entry["start"],
                entry["end"],
                entry["tokens"],
                0,
                entry["calls"],
                input_tokens=entry["input"],
                cache_tokens=entry["cache"],
            )
            for sid, entry in groups.items()
        }

    def session_stats(self, since_ms: int) -> list[SessionStat]:
        by_id = {}
        files = []
        for root in QODER_ROOTS:
            if not root.exists():
                continue
            for p in root.rglob("*.jsonl"):
                if "subagents" in p.parts:
                    continue
                try:
                    if int(p.stat().st_mtime * 1000) >= since_ms:
                        files.append(p)
                except OSError:
                    continue
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[:60]:
            key = file_cache_key(path)
            cached = QODER_FILE_CACHE.get(key) if key else None
            if cached is not None:
                for item in cached:
                    by_id[item.session_id] = item
                continue
            monitor = QoderMonitor()
            monitor._parse(path)
            values = []
            if monitor.session_tokens > 0 and monitor.session_ms > 0:
                values.append(
                    SessionStat(
                        "Qoder",
                        path.stem,
                        monitor.session_title or "Qoder 会话",
                        monitor.session_start,
                        monitor.session_end,
                        monitor.session_tokens,
                        monitor.session_ms,
                        monitor.session_calls,
                        approximate=True,
                    )
                )
            if key:
                remember_file(QODER_FILE_CACHE, key, values)
            for item in values:
                by_id[item.session_id] = item

        for sid, ide in self._ide_stats(since_ms).items():
            local = by_id.get(sid)
            if local is None:
                by_id[sid] = ide
                continue
            title = (
                ide.title
                if ide.title and ide.title != "Qoder IDE 会话"
                else local.title
            )
            by_id[sid] = SessionStat(
                "Qoder",
                sid,
                title,
                local.start_ms,
                local.end_ms,
                ide.tokens,
                local.model_ms,
                ide.calls,
                approximate=False,
                input_tokens=ide.input_tokens,
                cache_tokens=ide.cache_tokens,
            )
        return list(by_id.values())

    def poll(self) -> Reading:
        path = self._latest_transcript()
        if not path:
            return Reading("Qoder", None, "每次", note="未找到 Qoder 会话")
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return Reading("Qoder", None, "每次", note="读取失败")
        mtime_ms = self._family_mtime_ms(path)
        family_ns = mtime_ms * 1_000_000
        if self.cache_path != path or self.cache_mtime != family_ns:
            self.cache_path = path
            self.cache_mtime = family_ns
            self._parse(path)

        generating = bool(
            getattr(self, "generating", False)
            and (now_ms() - mtime_ms) < GENERATING_STALE_MS
        )

        if self.latest_tps is not None:
            return Reading(
                "Qoder",
                self.latest_tps,
                "每轮",
                "",
                generating,
                self.note,
                generating,
                sample_key=self.sample_key,
                session_key=self.sample_key,
                history=tuple(self.history_values[-24:]),
            )
        return Reading(
            "Qoder",
            None,
            "每轮",
            "",
            generating,
            "Qoder 未返回 usage",
            generating,
        )


class WorkBuddyMonitor:
    def __init__(self) -> None:
        self.cache_path: Optional[Path] = None
        self.cache_mtime: Optional[int] = None
        self.last: Optional[tuple[float, str, int]] = None
        self.active = False
        self.note = ""
        self.in_progress = False
        self.history_values = []
        self.session_tps = None
        self.session_tokens = 0.0
        self.session_input = 0.0
        self.session_cache = 0.0
        self.session_ms = 0
        self.session_calls = 0
        self.session_start = 0
        self.session_end = 0

    def _latest_file(self) -> Optional[Path]:
        if not WORKBUDDY_ROOT.exists():
            return None
        files = [
            p
            for p in WORKBUDDY_ROOT.rglob("*.jsonl")
            if "subagents" not in p.parts
        ]
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    @staticmethod
    def _family_files(path: Path) -> list:
        folder = path.with_suffix("") / "subagents"
        try:
            return [p for p in sorted(folder.glob("*.jsonl")) if p.is_file()]
        except OSError:
            return []

    @classmethod
    def _family_mtime_ms(cls, path: Path) -> int:
        times = []
        for item in [path, *cls._family_files(path)]:
            try:
                times.append(int(item.stat().st_mtime * 1000))
            except OSError:
                continue
        return max(times) if times else 0

    def _fold_subagents(self, path: Path) -> None:
        """One row = one task: merge this session's subagent transcripts in."""

        points = list(self.turn_points)
        tokens = self.session_tokens
        model_ms = self.session_ms
        calls = self.session_calls
        gen_input = self.session_input
        gen_cache = self.session_cache
        end = self.session_end
        in_progress = bool(self.in_progress)
        for child in self._family_files(path):
            extra = WorkBuddyMonitor()
            try:
                extra._parse(child)
            except Exception:
                continue
            points += list(extra.turn_points)
            tokens += extra.session_tokens
            model_ms += extra.session_ms
            calls += extra.session_calls
            gen_input += extra.session_input
            gen_cache += extra.session_cache
            end = max(end, extra.session_end)
            in_progress = in_progress or bool(extra.in_progress)
        self.turn_points = points
        self.session_input = gen_input
        self.session_cache = gen_cache
        self.session_end = end
        self.in_progress = in_progress
        if model_ms > 0:
            self.history_values = [tps for _ts, tps in sorted(points)]
            self.session_tokens = tokens
            self.session_ms = model_ms
            self.session_calls = calls
            self.session_tps = tokens / (model_ms / 1000.0)
            self.note = (
                f"{int(tokens)} tok / {model_ms / 1000:.1f}s · "
                f"{len(points)} turns / {calls} calls"
            )
            self.last = (self.session_tps, "", int(sorted(points)[-1][0]))

    def _parse(self, path: Path) -> None:
        self.last = None
        self.active = False
        self.history_values = []
        self.turn_points = []
        self.in_progress = False
        self.session_tokens = 0.0
        self.session_input = 0.0
        self.session_cache = 0.0
        self.session_ms = 0
        self.session_calls = 0
        self.session_start = 0
        self.session_end = 0
        turns = []
        current = None
        anchor = None
        first_event_ts = None
        last_event_ts = None
        last_user_ts = None
        last_final_ts = None

        def finish(turn):
            if turn and turn["tokens"] > 0 and turn["model_ms"] > 0:
                tps = turn["tokens"] / (turn["model_ms"] / 1000.0)
                if 0 < tps < 500:
                    return tps
            return None

        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    typ = obj.get("type")
                    ts = obj.get("timestamp")
                    ts = int(ts) if isinstance(ts, (int, float)) else None
                    if ts:
                        first_event_ts = ts if first_event_ts is None else first_event_ts
                        last_event_ts = ts
                    if typ == "message" and obj.get("role") == "user":
                        if current:
                            tps = finish(current)
                            if tps is not None:
                                turns.append(
                                    (
                                        tps,
                                        current["last_ts"],
                                        current["tokens"],
                                        current["model_ms"],
                                        current["calls"],
                                        current["input_tokens"],
                                        current["cache_tokens"],
                                    )
                                )
                        current = {
                            "tokens": 0.0,
                            "input_tokens": 0.0,
                            "cache_tokens": 0.0,
                            "model_ms": 0,
                            "calls": 0,
                            "complete": False,
                            "last_ts": ts or 0,
                        }
                        anchor = ts
                        last_user_ts = ts
                        continue
                    if typ == "function_call_result":
                        anchor = ts
                        continue
                    message = obj.get("message") or {}
                    usage = message.get("usage") or {}
                    output = usage.get("output_tokens") or 0
                    if output and ts and anchor and ts > anchor:
                        if current is None:
                            current = {
                                "tokens": 0.0,
                                "input_tokens": 0.0,
                                "cache_tokens": 0.0,
                                "model_ms": 0,
                                "calls": 0,
                                "complete": False,
                                "last_ts": ts,
                            }
                        input_raw = usage.get("input_tokens") or 0
                        cache = usage.get("cache_read_input_tokens") or 0
                        # WorkBuddy's output_tokens already includes reasoning.
                        current["tokens"] += float(output)
                        current["input_tokens"] += max(
                            0, float(input_raw) - float(cache)
                        )
                        current["cache_tokens"] += float(cache)
                        current["model_ms"] += ts - anchor
                        current["calls"] += 1
                        current["last_ts"] = ts
                    if typ == "message" and obj.get("role") == "assistant":
                        # Only final messages carry usage; intermediate ones
                        # written before a tool call must not close the turn.
                        if current and output:
                            current["complete"] = True
                        if ts:
                            last_final_ts = ts
        except Exception:
            pass

        if current and current["complete"]:
            tps = finish(current)
            if tps is not None:
                turns.append(
                    (
                        tps,
                        current["last_ts"],
                        current["tokens"],
                        current["model_ms"],
                        current["calls"],
                        current["input_tokens"],
                        current["cache_tokens"],
                    )
                )

        self.history_values = [turn[0] for turn in turns]
        self.turn_points = [(turn[1], turn[0]) for turn in turns]
        if turns:
            total_tokens = sum(turn[2] for turn in turns)
            total_ms = sum(turn[3] for turn in turns)
            total_calls = sum(turn[4] for turn in turns)
            self.session_input = sum(turn[5] for turn in turns)
            self.session_cache = sum(turn[6] for turn in turns)
            self.session_tps = total_tokens / (total_ms / 1000.0)
            self.note = (
                f"{int(total_tokens)} tok / {total_ms / 1000:.1f}s · "
                f"{len(turns)} turns / {total_calls} calls"
            )
            self.last = (self.session_tps, "", turns[-1][1])
            self.session_tokens = total_tokens
            self.session_ms = total_ms
            self.session_calls = total_calls
        else:
            self.session_tps = None
        self.session_start = first_event_ts or 0
        self.session_end = last_event_ts or int(path.stat().st_mtime * 1000)
        self.in_progress = bool(
            current and not current["complete"] and current["calls"] > 0
        )
        if last_event_ts and (now_ms() - last_event_ts) < 5000:
            self.active = True
        self._fold_subagents(path)


    def session_stats(self, since_ms: int) -> list[SessionStat]:
        if not WORKBUDDY_ROOT.exists():
            return []
        titles = {}
        try:
            conn = open_ro(WORKBUDDY_DB)
            try:
                for sid, title, custom, created, updated in conn.execute(
                    "select id,title,custom_title,created_at,updated_at from sessions"
                ):
                    titles[str(sid)] = (
                        custom or title or "",
                        int(created or 0),
                        int(updated or 0),
                    )
            finally:
                conn.close()
        except Exception:
            pass

        files = []
        for path in WORKBUDDY_ROOT.rglob("*.jsonl"):
            if "subagents" in path.parts:
                continue
            try:
                if int(path.stat().st_mtime * 1000) >= since_ms:
                    files.append(path)
            except OSError:
                continue
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        stats = []
        for path in files[:60]:
            meta = titles.get(path.stem)
            key = (file_cache_key(path), meta)
            cached = WORKBUDDY_FILE_CACHE.get(key) if key[0] else None
            if cached is not None:
                stats.extend(cached)
                continue
            monitor = WorkBuddyMonitor()
            monitor._parse(path)
            values = []
            if monitor.session_tokens > 0 and monitor.session_ms > 0:
                title = (meta[0] if meta else "") or "WorkBuddy 会话"
                start = monitor.session_start or (meta[1] if meta else 0)
                end = monitor.session_end or (meta[2] if meta else 0)
                values.append(
                    SessionStat(
                        "WorkBuddy",
                        path.stem,
                        title.replace("\n", " "),
                        start,
                        end,
                        monitor.session_tokens,
                        monitor.session_ms,
                        monitor.session_calls,
                        input_tokens=monitor.session_input,
                        cache_tokens=monitor.session_cache,
                    )
                )
            if key[0]:
                remember_file(WORKBUDDY_FILE_CACHE, key, values)
            stats.extend(values)
        return stats

    def poll(self) -> Reading:
        path = self._latest_file()
        if not path:
            return Reading("WorkBuddy", None, "每轮", note="未找到会话 jsonl")
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return Reading("WorkBuddy", None, "每轮", note="读取失败")
        if self.cache_path != path or self.cache_mtime != mtime:
            self.cache_path = path
            self.cache_mtime = mtime
            self._parse(path)

        generating = False
        try:
            conn = open_ro(WORKBUDDY_DB)
            try:
                row = conn.execute(
                    "select status,last_activity_at from sessions "
                    "order by last_activity_at desc limit 1"
                ).fetchone()
            finally:
                conn.close()
            # WorkBuddy marks a running session "working", not "active".
            if row and row[0] in ("working", "active") and (
                now_ms() - row[1]
            ) < GENERATING_STALE_MS:
                generating = True
        except Exception:
            pass
        try:
            if (
                self.in_progress
                and (now_ms() - self._family_mtime_ms(path)) < GENERATING_STALE_MS
            ):
                generating = True
        except OSError:
            pass
        self.generating = generating

        if self.last:
            note = f"上一轮 · {self.note}" if generating else self.note
            return Reading(
                "WorkBuddy",
                self.last[0],
                "每轮",
                self.last[1],
                generating,
                note,
                generating,
                sample_key=str(self.last[2]),
                session_key=str(path),
                history=tuple(self.history_values[-24:]),
            )
        note = "生成中…" if generating else "等待生成"
        return Reading("WorkBuddy", None, "每轮", "", generating, note, generating)


class ZCodeMonitor:
    @staticmethod
    def _tokens_by_message(conn, message_ids) -> dict:
        values = {}
        ids = [value for value in message_ids if value]
        for index in range(0, len(ids), 500):
            chunk = ids[index : index + 500]
            placeholders = ",".join("?" for _ in chunk)
            for message_id, data in conn.execute(
                f"select id,data from message where id in ({placeholders})", chunk
            ):
                parsed = jload(data) or {}
                tokens = parsed.get("tokens") or {}
                output = tokens.get("output")
                if not isinstance(output, (int, float)):
                    continue
                prompt = tokens.get("input")
                prompt = float(prompt) if isinstance(prompt, (int, float)) else 0.0
                cache = tokens.get("cache") or {}
                cached = cache.get("read")
                cached = (
                    float(cached) if isinstance(cached, (int, float)) else 0.0
                )
                values[message_id] = (
                    float(output),
                    max(0.0, prompt - cached),
                    cached,
                )
        return values

    @staticmethod
    def _active_root(conn):
        """Newest top-level session, counting its subagents' writes as its own.

        turn_usage only gets a row when a turn ends and subagents close turns
        constantly, so ordering by turn_usage lands on a subagent while the
        main session — quiet while it waits on children — is never examined.
        """

        rows = conn.execute(
            "select coalesce(s.parent_id, s.id) as root, max(m.time_updated) "
            "from message m join session s on s.id = m.session_id "
            "group by root order by 2 desc limit 8"
        ).fetchall()
        for root, _activity in rows:
            if not root:
                continue
            if conn.execute(
                "select 1 from session where id=? and parent_id is null", (root,)
            ).fetchone():
                return root
        return None

    @staticmethod
    def _turn_rows(conn, session_ids) -> list:
        if not session_ids:
            return []
        placeholders = ",".join("?" for _ in session_ids)
        return conn.execute(
            "select turn_id,status,started_at,completed_at,duration_ms,"
            f"output_tokens,reasoning_tokens,model_request_count "
            f"from turn_usage where session_id in ({placeholders}) order by started_at",
            session_ids,
        ).fetchall()

    @staticmethod
    def _descendants(conn, session_id) -> list:
        return [
            row[0]
            for row in conn.execute(
                "select id from session where parent_id=?", (session_id,)
            ).fetchall()
        ]

    @staticmethod
    def _live_activity(conn, session_id) -> int:
        """Newest write across the session and all of its subagent sessions."""

        ids = [
            row[0]
            for row in conn.execute(
                "select id from session where id=? or parent_id=?",
                (session_id, session_id),
            ).fetchall()
        ]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        value = conn.execute(
            f"select max(time_updated) from ("
            f"select time_updated from message where session_id in ({placeholders}) "
            f"union all "
            f"select time_updated from part where session_id in ({placeholders})"
            f")",
            [*ids, *ids],
        ).fetchone()[0]
        return int(value or 0)

    def poll(self) -> Reading:
        if not ZCODE_DB.exists():
            return Reading("ZCode", None, "每轮", note="未找到 db.sqlite")
        try:
            conn = open_ro(ZCODE_DB)
            try:
                session_id = self._active_root(conn)
                if not session_id:
                    row = conn.execute(
                        "select session_id from turn_usage order by started_at desc limit 1"
                    ).fetchone()
                    if not row:
                        return Reading("ZCode", None, "每轮", note="暂无会话")
                    session_id = row[0]
                live_ms = self._live_activity(conn, session_id)
                last_closed = conn.execute(
                    "select max(completed_at) from turn_usage where session_id=?",
                    (session_id,),
                ).fetchone()[0] or 0
                scope = [session_id] + self._descendants(conn, session_id)
                turns = self._turn_rows(conn, scope)
                by_turn = {}
                turn_ids = [turn[0] for turn in turns if turn[0]]
                if turn_ids:
                    placeholders = ",".join("?" for _ in turn_ids)
                    model_rows = conn.execute(
                        "select turn_id,assistant_message_id,duration_ms,output_tokens "
                        f"from model_usage where turn_id in ({placeholders}) "
                        "and status='completed'",
                        turn_ids,
                    ).fetchall()
                    message_tokens = self._tokens_by_message(
                        conn, [row[1] for row in model_rows]
                    )
                    for turn_id, message_id, duration, output in model_rows:
                        entry = by_turn.setdefault(
                            turn_id, {"tokens": 0.0, "model_ms": 0}
                        )
                        value = message_tokens.get(message_id)
                        entry["tokens"] += value[0] if value else (output or 0)
                        entry["model_ms"] += duration or 0
            finally:
                conn.close()
        except Exception as exc:
            return Reading("ZCode", None, "每轮", note=f"读取失败: {type(exc).__name__}")

        history = []
        session_tokens = 0.0
        session_ms = 0
        session_calls = 0
        session_turns = 0
        for turn in turns:
            turn_id, status, started, completed, duration, output, reasoning, calls = turn
            entry = by_turn.get(turn_id)
            if status not in ("completed", "cancelled") or not entry or entry["model_ms"] <= 0:
                continue
            tokens = entry["tokens"] or ((output or 0) + (reasoning or 0))
            tps = tokens / (entry["model_ms"] / 1000.0)
            if 0 < tps < 2000:
                history.append(tps)
                session_tokens += tokens
                session_ms += entry["model_ms"]
                session_calls += calls or 0
                session_turns += 1

        generating = bool(live_ms) and live_ms > last_closed and is_fresh(live_ms, now_ms())
        if session_ms > 0:
            session_tps = session_tokens / (session_ms / 1000.0)
            return Reading(
                "ZCode",
                session_tps,
                "每轮",
                "",
                generating,
                f"{int(session_tokens)} tok / {session_ms / 1000:.1f}s · {session_turns} turns / {session_calls} calls",
                generating,
                sample_key=str(session_id),
                session_key=str(session_id),
                history=tuple(history[-24:]),
            )
        return Reading(
            "ZCode",
            None,
            "每轮",
            note="暂无完成的轮次",
            generating=generating,
            session_key=str(session_id),
        )

    def session_stats(self, since_ms: int) -> list[SessionStat]:
        if not ZCODE_DB.exists():
            return []
        conn = None
        stats = []
        try:
            conn = open_ro(ZCODE_DB)
            sessions = conn.execute(
                "select id,title,time_created,time_updated from session "
                "where time_updated>=? order by time_updated desc limit 120",
                (since_ms,),
            ).fetchall()
            parent_of = {
                child: root
                for child, root in conn.execute(
                    "select id, coalesce(parent_id, id) from session"
                ).fetchall()
                if child != root
            }
            titles = {row[0]: row[1] or "" for row in sessions}
            prepared = []
            message_ids = set()
            for sid, title, created, updated in sessions:
                turns = conn.execute(
                    "select turn_id,status,started_at,completed_at,output_tokens "
                    "from turn_usage where session_id=? "
                    "and status in ('completed','cancelled') "
                    "order by started_at",
                    (sid,),
                ).fetchall()
                terminal = {row[0] for row in turns if row[0]}
                if not terminal:
                    continue
                rows = [
                    row
                    for row in conn.execute(
                        "select turn_id,assistant_message_id,duration_ms,output_tokens "
                        "from model_usage where session_id=? and status='completed'",
                        (sid,),
                    ).fetchall()
                    if row[0] in terminal
                ]
                if not rows:
                    continue
                prepared.append((sid, title, updated, turns, rows))
                message_ids.update(row[1] for row in rows if row[1])

            message_tokens = self._tokens_by_message(conn, message_ids)

            for sid, title, updated, turns, rows in prepared:
                tokens = 0.0
                input_tokens = 0.0
                cache_tokens = 0.0
                model_ms = 0
                calls = 0
                for _turn_id, message_id, duration, output in rows:
                    value = message_tokens.get(message_id)
                    if value:
                        tokens += value[0]
                        input_tokens += value[1]
                        cache_tokens += value[2]
                    else:
                        tokens += output or 0
                    model_ms += duration or 0
                    calls += 1
                if tokens <= 0 or model_ms <= 0:
                    continue
                start = min(row[2] for row in turns if row[2])
                end = max(
                    (row[3] or row[2] or 0) for row in turns
                ) or updated or start
                stats.append(
                    SessionStat(
                        "ZCode",
                        str(sid),
                        (title or "ZCode 会话").replace("\n", " "),
                        int(start),
                        int(end),
                        tokens,
                        model_ms,
                        calls,
                        input_tokens=input_tokens,
                        cache_tokens=cache_tokens,
                    )
                )
        except Exception:
            return []

        finally:
            if conn is not None:
                conn.close()
        stats = fold_task_rows(stats, parent_of, titles)
        return stats


def poll_all() -> list[Reading]:
    return [
        ZCodeMonitor().poll(),
        QoderMonitor().poll(),
        WorkBuddyMonitor().poll(),
        MimoMonitor().poll(),
    ]


def collect_session_stats(since_ms: int) -> list[SessionStat]:
    monitors = (ZCodeMonitor(), QoderMonitor(), WorkBuddyMonitor(), MimoMonitor())

    def run(monitor):
        try:
            return monitor.session_stats(since_ms)
        except Exception:
            return []

    stats = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for values in pool.map(run, monitors):
            stats.extend(values)
    stats.sort(key=lambda item: item.end_ms, reverse=True)
    return stats


def once() -> None:
    for r in poll_all():
        print(json.dumps(r.__dict__, ensure_ascii=False))


APP_VERSION = "0.5.0"
BG = "#0f1117"
BORDER = "#2a2f3a"
TEXT = "#e6e8ee"
SUBTLE = "#8b93a7"
GREEN = "#4ade80"
YELLOW = "#fbbf24"
RED = "#f87171"
BLUE = "#60a5fa"
ACCENT = "#60a5fa"
PEEK = 5
SNAP = 14
HIDE_DELAY = 0.6
CLIENT_COLORS = {
    "ZCode": BLUE,
    "Qoder": "#c084fc",
    "WorkBuddy": YELLOW,
    "MiMo": GREEN,
}


def request_geometry(window, width: int, height: int, x: int, y: int) -> tuple:
    """Ask Tk to move the window and return what must be confirmed later.

    Tk silently drops a geometry request when the window has not been moved
    for a while, so the caller has to re-issue it until the window lands.
    """

    window.geometry(f"{width}x{height}+{x}+{y}")
    return (x, y)


def confirm_geometry(window, target, width: int, height: int):
    """Return None once the window really sits at target, else re-issue it."""

    if not target:
        return None
    if (window.winfo_x(), window.winfo_y()) == (target[0], target[1]):
        return None
    window.geometry(f"{width}x{height}+{target[0]}+{target[1]}")
    return target


def fold_task_rows(stats, parent_of, titles):
    """One row per top-level task; subagent sessions fold into their parent.

    ZCode spawns hundreds of subagent sessions per task, and each one used to
    occupy its own row, burying the task the user actually ran.
    """

    groups = {}
    order = []
    for row in stats:
        root = parent_of.get(row.session_id, row.session_id)
        if root not in groups:
            groups[root] = {
                "client": row.client,
                "own": None,
                "start": row.start_ms,
                "end": row.end_ms,
                "tokens": 0.0,
                "input": 0.0,
                "cache": 0.0,
                "ms": 0,
                "calls": 0,
                "subs": 0,
                "approximate": False,
            }
            order.append(root)
        group = groups[root]
        group["tokens"] += row.tokens
        group["input"] += row.input_tokens
        group["cache"] += row.cache_tokens
        group["ms"] += row.model_ms
        group["calls"] += row.calls
        group["start"] = min(group["start"], row.start_ms)
        group["end"] = max(group["end"], row.end_ms)
        group["approximate"] = group["approximate"] or row.approximate
        if root == row.session_id:
            group["own"] = row
        else:
            group["subs"] += 1
    folded = []
    for root in order:
        group = groups[root]
        own = group["own"]
        if own is not None:
            title = own.title
        else:
            title = " ".join((titles.get(root) or "").split()) or f"{group['client']} 任务"
        folded.append(
            SessionStat(
                group["client"],
                root,
                title,
                int(group["start"]),
                int(group["end"]),
                group["tokens"],
                group["ms"],
                group["calls"],
                approximate=group["approximate"],
                input_tokens=group["input"],
                cache_tokens=group["cache"],
                subagents=group["subs"],
            )
        )
    return folded


class HistoryTrack:
    """Rolling sparkline points for one client, kept across sessions.

    Each monitor only reports the turns of its newest session, so the previous
    session is frozen when the session key changes instead of being dropped.
    """

    def __init__(self, limit: int = 24) -> None:
        self.limit = limit
        self.key = ""
        self.frozen: list[tuple[float, bool]] = []
        self.values: list[float] = []
        self.boundary = False

    def push(self, session_key: str, values: tuple[float, ...]) -> None:
        if session_key and session_key != self.key:
            self._freeze()
            self.key = session_key
            self.boundary = bool(self.frozen)
        self.values = list(values)

    def _freeze(self) -> None:
        if not self.values:
            return
        self.frozen.extend((v, self.boundary and i == 0) for i, v in enumerate(self.values))
        self.frozen = self.frozen[-self.limit :]
        self.values = []
        self.boundary = False

    def points(self) -> list[tuple[float, bool]]:
        points = self.frozen + [
            (v, self.boundary and i == 0) for i, v in enumerate(self.values)
        ]
        return points[-self.limit :]


class Overlay:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(f"TPS Monitor {APP_VERSION}")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.96)
        self.root.configure(bg=BORDER)

        self.width, self.height = 190, 128
        self.work = self._get_work_area()
        left, top, right, _bottom = self.work
        x = max(left + 10, right - self.width - 20)
        y = top + 20
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")

        self.edge = None
        self.hidden = False
        self.dragging = False
        self.hover_armed = False
        self.last_inside = time.time()
        self.hide_time = 0.0
        self.full_geom = (x, y)
        self.pending_geom = None
        self.click_while_hidden = False

        self.q: queue.Queue[list[Reading]] = queue.Queue()
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()
        self.history = {name: HistoryTrack() for name in CLIENT_COLORS}
        self.popup = None
        self.popup_period = "today"
        self.popup_window = "today"
        self.popup_sessions = None
        self.popup_loading = False
        self.popup_pending = False
        self.popup_updated_at = 0.0
        self.popup_token = 0
        self.popup_q = queue.Queue()
        self.history_cache = {}
        self._press_root = (0, 0)
        self._moved = False
        self.blinking = {}

        self._build()
        self.root.after(120, self._drain)
        self.root.after(80, self._hover_check)
        self.root.after(120, self._blink_tick)
        self.root.after(2000, self._preload_history)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _get_work_area(self):
        width = self.root.winfo_screenwidth()
        height = self.root.winfo_screenheight()
        if ctypes is not None:
            try:
                rect = wintypes.RECT()
                if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0
                ):
                    return (rect.left, rect.top, rect.right, rect.bottom)
            except Exception:
                pass
        return (0, 0, width, height)

    def _build(self) -> None:
        outer = tk.Frame(
            self.root,
            bg=BG,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        outer.pack(fill="both", expand=True)
        self.outer = outer

        header = tk.Frame(outer, bg=BG, height=13)
        header.pack(fill="x", padx=3, pady=(1, 0))
        header.pack_propagate(False)
        tk.Label(
            header,
            text=f"TPS {APP_VERSION}",
            bg=BG,
            fg="#6b7280",
            font=("Consolas", 7, "bold"),
        ).pack(side="left")
        close = tk.Label(
            header,
            text="×",
            bg=BG,
            fg="#6b7280",
            font=("Consolas", 9, "bold"),
            cursor="hand2",
        )
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self._close())

        self.rows = {}
        for name in ("ZCode", "Qoder", "WorkBuddy", "MiMo"):
            row = tk.Frame(outer, bg=BG, height=27)
            row.pack(fill="x", padx=(3, 0), pady=0)
            row.pack_propagate(False)

            label = tk.Label(
                row,
                text=name[0],
                bg=BG,
                fg=CLIENT_COLORS[name],
                font=("Consolas", 9, "bold"),
                anchor="w",
                width=1,
            )
            label.pack(side="left", padx=(0, 3))

            spark = tk.Canvas(
                row,
                width=120,
                height=16,
                bg=BG,
                highlightthickness=0,
            )
            spark.pack(side="left", padx=(2, 3))

            tps = tk.Label(
                row,
                text="—",
                bg=BG,
                fg=SUBTLE,
                font=("Consolas", 12, "bold"),
                anchor="e",
                width=5,
            )
            tps.pack(side="right", padx=(0, 0))

            for widget in (row, label, spark, tps):
                widget.bind("<ButtonPress-1>", self._start_drag)
                widget.bind("<B1-Motion>", self._drag)
                widget.bind("<ButtonRelease-1>", self._end_drag)
                widget.bind("<Button-3>", self._toggle_popup)

            self.rows[name] = {
                "label": label,
                "spark": spark,
                "tps": tps,
            }

        for widget in (outer, header):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)
            widget.bind("<Button-3>", self._toggle_popup)

    def _draw_spark(
        self, canvas: tk.Canvas, values, color: str, marks=()
    ) -> None:
        canvas.delete("all")
        width = int(canvas.cget("width"))
        height = int(canvas.cget("height"))
        if not values:
            return
        if len(values) == 1:
            canvas.create_oval(
                width - 4,
                height / 2 - 1.5,
                width - 1,
                height / 2 + 1.5,
                fill=color,
                outline="",
            )
            if 0 in marks:
                self._draw_boundary(canvas, width - 7, height)
            return
        lo = min(values)
        hi = max(values)
        if hi - lo < 1e-9:
            hi = lo + 1
        count = len(values)
        pad = 2
        points = []
        log_lo = math.log1p(max(0.0, lo))
        log_hi = math.log1p(max(0.0, hi))
        if log_hi - log_lo < 1e-9:
            log_hi = log_lo + 1
        for i, value in enumerate(values):
            x = pad + i * (width - 2 * pad) / (count - 1)
            y = height - pad - (
                (math.log1p(max(0.0, value)) - log_lo) / (log_hi - log_lo)
            ) * (height - 2 * pad)
            points.extend((x, y))
        canvas.create_line(*points, fill=color, width=1.6, smooth=True)
        step = (width - 2 * pad) / (count - 1)
        for index in marks:
            if index <= 0:
                continue
            self._draw_boundary(canvas, pad + index * step - step / 2, height)
        canvas.create_oval(
            points[-2] - 1.5,
            points[-1] - 1.5,
            points[-2] + 1.5,
            points[-1] + 1.5,
            fill=color,
            outline="",
        )

    @staticmethod
    def _draw_boundary(canvas: tk.Canvas, x: float, height: int) -> None:
        canvas.create_line(x, 1, x, height - 1, fill="#39404f", width=1)

    def _worker(self) -> None:
        monitors = {
            "ZCode": ZCodeMonitor(),
            "Qoder": QoderMonitor(),
            "WorkBuddy": WorkBuddyMonitor(),
            "MiMo": MimoMonitor(),
        }
        while not self.stop.is_set():
            for name, monitor in monitors.items():
                try:
                    reading = monitor.poll()
                except Exception as exc:
                    reading = Reading(
                        name, None, "", note=f"异常: {type(exc).__name__}"
                    )
                self.q.put([reading])
            self.stop.wait(0.5)

    def _drain(self) -> None:
        try:
            while True:
                readings = self.q.get_nowait()
                self._apply(readings)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _apply(self, readings: list[Reading]) -> None:
        for r in readings:
            row = self.rows.get(r.client)
            if not row:
                continue

            values, marks = [], ()
            track = self.history.get(r.client)
            if track is not None:
                if r.history:
                    track.push(r.session_key, r.history)
                points = track.points()
                values = [point[0] for point in points]
                marks = tuple(
                    index for index, point in enumerate(points) if point[1]
                )
            display_tps = values[-1] if values else r.tps

            if display_tps is None:
                # No number yet (e.g. a first turn still in flight). The rule
                # is that the words never come back: show a dash and let it
                # blink while the client is working.
                row["tps"].config(
                    text="—",
                    fg=SUBTLE if not r.generating else YELLOW,
                    font=("Consolas", 11, "bold"),
                )
                color = YELLOW if r.generating else "#4b5263"
            else:
                if display_tps < 100:
                    value = f"{display_tps:.1f}"
                else:
                    value = f"{display_tps:.0f}"
                if r.client == "Qoder":
                    value = "≈" + value
                color = GREEN if display_tps >= 50 else YELLOW if display_tps >= 25 else RED
                row["tps"].config(text=value, font=("Consolas", 12, "bold"))

            if r.generating:
                # The blink loop owns the foreground color; resetting it here
                # on every 0.5s reading causes visible quick flashes.
                self.blinking[r.client] = color
            else:
                self.blinking.pop(r.client, None)
                if display_tps is not None:
                    row["tps"].config(fg=color)

            self._draw_spark(row["spark"], values, color, marks)

    @staticmethod
    def _blend_color(first: str, second: str, amount: float) -> str:
        amount = max(0.0, min(1.0, amount))
        a = tuple(int(first[i : i + 2], 16) for i in (1, 3, 5))
        b = tuple(int(second[i : i + 2], 16) for i in (1, 3, 5))
        return "#" + "".join(
            f"{int(a[i] + (b[i] - a[i]) * amount):02x}" for i in range(3)
        )

    def _blink_tick(self) -> None:
        if self.stop.is_set():
            return
        phase = 0.5 + 0.5 * math.sin(time.time() * math.pi * 2 / 2.4)
        strength = 0.3 + 0.7 * phase
        for client, base in list(self.blinking.items()):
            row = self.rows.get(client)
            if not row:
                continue
            dim = self._blend_color(base, BG, 0.72)
            row["tps"].configure(fg=self._blend_color(dim, base, strength))
        self.root.after(110, self._blink_tick)

    def _preload_history(self) -> None:
        def run():
            try:
                stats = collect_session_stats(period_cutoff("today"))
            except Exception:
                return
            self.history_cache["today"] = (time.time(), stats)

        threading.Thread(target=run, daemon=True).start()

    def _toggle_popup(self, _event=None) -> None:
        if self.popup is not None:
            self._close_popup()
        else:
            self._show_popup()

    def _show_popup(self) -> None:
        self._close_popup()
        self.popup_period = "today"
        self.popup_window = "today"
        self.popup_loading = False
        self.popup_sessions = None
        self.popup_updated_at = 0.0

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 1.0)
        win.configure(bg=BORDER)
        self.popup = win

        outer = tk.Frame(
            win,
            bg=BG,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        outer.pack(fill="both", expand=True)

        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", padx=10, pady=(7, 4))
        tk.Label(
            head,
            text=f"TOKEN STATS {APP_VERSION}",
            bg=BG,
            fg=SUBTLE,
            font=("Consolas", 8, "bold"),
        ).pack(side="left")
        close = tk.Label(
            head,
            text="×",
            bg=BG,
            fg="#6b7280",
            font=("Consolas", 11, "bold"),
            cursor="hand2",
        )
        close.pack(side="right")
        close.bind("<Button-1>", lambda _e: self._close_popup())
        refresh = tk.Label(
            head,
            text="↻",
            bg=BG,
            fg="#6b7280",
            font=("Consolas", 10, "bold"),
            cursor="hand2",
        )
        refresh.pack(side="right", padx=(0, 8))
        refresh.bind(
            "<Button-1>",
            lambda _e: self._ensure_data(self.popup_window, force=True),
        )

        filters = tk.Frame(outer, bg=BG)
        filters.pack(fill="x", padx=10, pady=(0, 6))
        self.popup_filters = {}
        for key, text in (("today", "今天"), ("7d", "7天"), ("30d", "30天")):
            button = tk.Button(
                filters,
                text=text,
                command=lambda k=key: self._set_period(k),
                bd=0,
                relief="flat",
                padx=9,
                pady=2,
                font=("Microsoft YaHei UI", 8),
                bg=BG,
                fg=SUBTLE,
                activebackground="#1c2230",
                activeforeground=TEXT,
                cursor="hand2",
                highlightthickness=0,
            )
            button.pack(side="left", padx=(0, 4))
            self.popup_filters[key] = button

        summary = tk.Frame(outer, bg=BG)
        summary.pack(fill="x", padx=10, pady=(2, 7))
        self.popup_total = self._summary_card(summary, "OUT TOKENS", TEXT, 16)
        self.popup_overall = self._summary_card(summary, "TPS", GREEN, 16)
        self.popup_count = self._summary_card(summary, "SESSIONS", TEXT, 16)

        self.popup_breakdown = tk.Label(
            outer,
            text="",
            bg=BG,
            fg="#6b7280",
            font=("Consolas", 7),
            anchor="w",
        )
        self.popup_breakdown.pack(fill="x", padx=10, pady=(0, 4))

        tk.Frame(outer, bg=BORDER, height=1).pack(fill="x", padx=10)

        columns = tk.Frame(outer, bg=BG)
        columns.pack(fill="x", padx=10, pady=(4, 1))
        for index, (text, align, width) in enumerate(
            (("TIME", "w", 11), ("", "w", 2), ("SESSION", "w", 0), ("OUT", "e", 8), ("TPS", "e", 6))
        ):
            label = tk.Label(
                columns,
                text=text,
                bg=BG,
                fg="#596173",
                font=("Consolas", 7, "bold"),
                anchor=align,
                width=width if width else 0,
            )
            label.grid(row=0, column=index, sticky="ew", padx=(0, 4))
        columns.columnconfigure(2, weight=1)

        self.popup_note = tk.Label(
            outer,
            text="",
            bg=BG,
            fg="#596173",
            font=("Microsoft YaHei UI", 7),
            anchor="w",
        )
        self.popup_note.pack(side="bottom", fill="x", padx=10, pady=(2, 6))

        wrap = tk.Frame(outer, bg=BG)
        wrap.pack(fill="both", expand=True, padx=(10, 4), pady=(0, 0))
        self.popup_canvas = tk.Canvas(
            wrap, bg=BG, highlightthickness=0, yscrollincrement=16
        )
        self.popup_scrollbar = tk.Canvas(
            wrap, width=5, bg=BG, highlightthickness=0, bd=0
        )
        self.popup_canvas.configure(yscrollcommand=self._update_scrollbar)
        self.popup_canvas.pack(side="left", fill="both", expand=True)
        self.popup_scrollbar.pack(side="right", fill="y", padx=(2, 0))
        self.popup_scrollbar.bind("<Button-1>", self._scrollbar_jump)
        self.popup_scrollbar.bind("<B1-Motion>", self._scrollbar_jump)
        self.popup_scrollbar.bind("<Configure>", lambda _e: self._update_scrollbar())
        self.popup_rows = tk.Frame(self.popup_canvas, bg=BG)
        self.popup_canvas_window = self.popup_canvas.create_window(
            (0, 0), window=self.popup_rows, anchor="nw"
        )
        self.popup_rows.bind(
            "<Configure>",
            lambda _e: self.popup_canvas.configure(
                scrollregion=self.popup_canvas.bbox("all")
            ),
        )
        self.popup_canvas.bind(
            "<Configure>",
            lambda e: self.popup_canvas.itemconfigure(
                self.popup_canvas_window, width=e.width
            ),
        )
        win.bind("<MouseWheel>", self._popup_wheel)
        win.bind("<Escape>", lambda _e: self._close_popup())

        width, height = 430, 392
        self.root.update_idletasks()
        left, top, right, bottom = self.work
        hx, hy = self.root.winfo_x(), self.root.winfo_y()
        px = hx + self.width + 8
        if px + width > right:
            px = hx - width - 8
        px = self._clamp(px, left + 4, right - width - 4)
        py = self._clamp(hy, top + 4, bottom - height - 4)
        win.geometry(f"{width}x{height}+{px}+{py}")

        self._set_period("today", render=False)
        self._ensure_data("today")
        self.root.after(30000, self._auto_refresh_tick)

    def _update_scrollbar(self, first=None, last=None) -> None:
        if self.popup is None or not hasattr(self, "popup_scrollbar"):
            return
        scrollbar = self.popup_scrollbar
        if not scrollbar.winfo_exists():
            return
        if first is None or last is None:
            first, last = self.popup_canvas.yview()
        else:
            first, last = float(first), float(last)
        scrollbar.delete("all")
        if last - first >= 0.999:
            return
        height = max(1, scrollbar.winfo_height())
        top = 2 + first * height
        bottom = last * height - 2
        if bottom - top < 14:
            middle = (top + bottom) / 2
            top, bottom = middle - 7, middle + 7
        scrollbar.create_rectangle(
            1,
            max(1, top),
            4,
            min(height - 1, bottom),
            fill="#4a5264",
            outline="",
        )

    def _scrollbar_jump(self, event) -> None:
        if self.popup is None:
            return
        height = max(1, self.popup_scrollbar.winfo_height())
        fraction = max(0.0, min(1.0, event.y / height))
        self.popup_canvas.yview_moveto(fraction)
        self._update_scrollbar()

    def _popup_wheel(self, event):
        if self.popup is None:
            return "break"
        first, last = self.popup_canvas.yview()
        if last - first >= 0.999:
            return "break"
        if event.delta > 0:
            if first <= 0:
                return "break"
            step = -1
        elif event.delta < 0:
            if last >= 1:
                return "break"
            step = 1
        else:
            return "break"
        self.popup_canvas.yview_scroll(step, "units")
        self._update_scrollbar()
        return "break"

    def _summary_card(self, parent, caption, color, size):
        card = tk.Frame(parent, bg=BG)
        card.pack(side="left", expand=True, fill="x")
        value = tk.Label(
            card,
            text="—",
            bg=BG,
            fg=color,
            font=("Consolas", size, "bold"),
            anchor="w",
        )
        value.pack(anchor="w")
        tk.Label(
            card,
            text=caption,
            bg=BG,
            fg="#596173",
            font=("Consolas", 7, "bold"),
            anchor="w",
        ).pack(anchor="w")
        return value

    def _set_period(self, period: str, render: bool = True) -> None:
        self.popup_period = period
        for key, button in getattr(self, "popup_filters", {}).items():
            if key == period:
                button.configure(bg="#1c2230", fg=TEXT)
            else:
                button.configure(bg=BG, fg=SUBTLE)
        if not render:
            return
        window = period
        if window != self.popup_window or self.popup_sessions is None:
            self._ensure_data(window)
        else:
            self._render_popup()

    def _ensure_data(self, window: str, force: bool = False) -> None:
        if self.popup is None:
            return
        if self.popup_loading and not force and window == self.popup_window:
            return

        cached = self.history_cache.get(window)
        if cached:
            self.popup_window = window
            self.popup_sessions = cached[1]
            self.popup_loading = False
            self.popup_updated_at = cached[0]
            self._render_popup()
            if not force and (time.time() - cached[0]) < 60:
                return
        else:
            self.popup_window = window
            self.popup_loading = True
            self.popup_sessions = None
            self._render_popup()

        self.popup_token += 1
        token = self.popup_token
        self.popup_pending = True

        def run():
            stats = collect_session_stats(period_cutoff(window))
            self.popup_q.put((token, window, stats))

        threading.Thread(target=run, daemon=True).start()
        self._poll_history()

    def _poll_history(self) -> None:
        if self.popup is None:
            return
        try:
            while True:
                token, window, stats = self.popup_q.get_nowait()
                if token != self.popup_token:
                    continue
                self.popup_pending = False
                self.history_cache[window] = (time.time(), stats)
                if window != self.popup_window:
                    continue
                self.popup_loading = False
                self.popup_sessions = stats
                self.popup_updated_at = time.time()
                self._render_popup()
                return
        except queue.Empty:
            pass
        if self.popup_pending:
            self.root.after(140, self._poll_history)

    def _auto_refresh_tick(self) -> None:
        if self.popup is None:
            return
        if (
            self.popup_period == "today"
            and not self.popup_loading
            and not self.popup_pending
            and (time.time() - self.popup_updated_at) >= 60
        ):
            self._ensure_data("today", force=True)
        self.root.after(30000, self._auto_refresh_tick)

    def _render_popup(self) -> None:
        if self.popup is None:
            return
        if self.popup_loading:
            self.popup_total.configure(text="统计中…")
            self.popup_overall.configure(text="—")
            self.popup_count.configure(text="—")
            self.popup_breakdown.configure(text="")
            window_name = {"today": "今天", "7d": "近 7 天", "30d": "近 30 天"}.get(
                self.popup_window, ""
            )
            self.popup_note.configure(text=f"正在读取{window_name}四家本地会话数据")
            self._clear_rows()
            return

        sessions = self.popup_sessions or []
        cutoff = period_cutoff(self.popup_period)
        visible = [item for item in sessions if item.end_ms >= cutoff]
        total_tokens = sum(item.tokens for item in visible)
        total_input = sum(item.input_tokens for item in visible)
        total_cache = sum(item.cache_tokens for item in visible)
        tps_tokens = sum(item.tokens for item in visible if item.model_ms > 0)
        total_ms = sum(item.model_ms for item in visible)
        overall = tps_tokens / (total_ms / 1000.0) if total_ms > 0 else None
        approximate = any(item.approximate for item in visible)
        self.popup_total.configure(text=fmt_tokens(total_tokens))
        self.popup_overall.configure(
            text=fmt_tps(overall, approximate)
        )
        self.popup_count.configure(text=str(len(visible)))
        self.popup_breakdown.configure(
            text=f"IN {fmt_tokens(total_input)} · CACHE {fmt_tokens(total_cache)}"
        )
        stamp = (
            datetime.fromtimestamp(self.popup_updated_at).strftime("%H:%M:%S")
            if self.popup_updated_at
            else "—"
        )
        self.popup_note.configure(
            text=f"{min(len(visible), 80)} 个任务 · 更新 {stamp} · Qoder IDE 真值 / CLI 估算"
        )

        self._clear_rows()
        if not visible:
            tk.Label(
                self.popup_rows,
                text="这个时间段没有会话数据",
                bg=BG,
                fg=SUBTLE,
                font=("Microsoft YaHei UI", 9),
                anchor="w",
            ).pack(fill="x", pady=14, padx=2)
            return

        for item in visible[:80]:
            row = tk.Frame(self.popup_rows, bg=BG)
            row.pack(fill="x", pady=1)
            for index, (text, align, width, color, font) in enumerate(
                (
                    (fmt_time(item.end_ms), "w", 11, SUBTLE, ("Consolas", 8)),
                    (item.client[0], "w", 2, CLIENT_COLORS[item.client], ("Consolas", 8, "bold")),
                    (self._short_title(item.title), "w", 0, TEXT, ("Microsoft YaHei UI", 8)),
                    (f"+{item.subagents}" if item.subagents else "", "e", 4, SUBTLE, ("Consolas", 8)),
                    (fmt_tokens(item.tokens), "e", 8, TEXT, ("Consolas", 8)),
                    (fmt_tps(item.tps, item.approximate), "e", 6, GREEN if (item.tps or 0) >= 50 else YELLOW if (item.tps or 0) >= 25 else RED, ("Consolas", 8, "bold")),
                )
            ):
                label = tk.Label(
                    row,
                    text=text,
                    bg=BG,
                    fg=color,
                    font=font,
                    anchor=align,
                    width=width if width else 0,
                )
                label.grid(row=0, column=index, sticky="ew", padx=(0, 4))
            row.columnconfigure(2, weight=1)

    @staticmethod
    def _short_title(text: str) -> str:
        text = (text or "会话").replace("\n", " ").strip()
        return text if len(text) <= 24 else text[:23] + "…"

    def _clear_rows(self) -> None:
        if not hasattr(self, "popup_rows"):
            return
        for child in self.popup_rows.winfo_children():
            child.destroy()
        self.popup_canvas.yview_moveto(0)
        self._update_scrollbar()

    def _close_popup(self) -> None:
        self.popup_token += 1
        if self.popup is not None:
            try:
                self.popup.destroy()
            except Exception:
                pass
            self.popup = None
        self.popup_loading = False
        self.popup_pending = False

    def _start_drag(self, event) -> None:
        self._press_root = (event.x_root, event.y_root)
        self._moved = False
        if self.hidden:
            # A click on the 5px strip should open the stats too, not only slide
            # the HUD back out.
            self._expand()
            self.click_while_hidden = True
            return
        self.click_while_hidden = False
        self.dragging = True
        self.last_inside = time.time()
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag(self, event) -> None:
        if not self.dragging:
            return
        dx = event.x_root - self._press_root[0]
        dy = event.y_root - self._press_root[1]
        if not self._moved and dx * dx + dy * dy > 9:
            self._moved = True
            self._close_popup()
        if not self._moved:
            return
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")
        self.full_geom = (x, y)
        self.pending_geom = None

    def _end_drag(self, _event=None) -> None:
        if not self.dragging:
            if self.click_while_hidden:
                self.click_while_hidden = False
                self._toggle_popup()
            return
        moved = self._moved
        self.dragging = False
        if moved:
            self._snap()
        else:
            self._toggle_popup()

    @staticmethod
    def _clamp(value, low, high):
        return max(low, min(value, high))

    def _snap(self) -> None:
        self.root.update_idletasks()
        left, top, right, bottom = self.work
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        width, height = self.width, self.height
        edge = None
        fx, fy = x, y
        if x <= left + SNAP:
            edge = "left"
            fx = left
            fy = self._clamp(y, top, bottom - height)
        elif x + width >= right - SNAP:
            edge = "right"
            fx = right - width
            fy = self._clamp(y, top, bottom - height)
        elif y <= top + SNAP:
            edge = "top"
            fy = top
            fx = self._clamp(x, left, right - width)
        elif y + height >= bottom - SNAP:
            edge = "bottom"
            fy = bottom - height
            fx = self._clamp(x, left, right - width)

        self.edge = edge
        self.full_geom = (fx, fy)
        self.pending_geom = request_geometry(self.root, width, height, fx, fy)
        if edge:
            self._hide()
        else:
            self.hidden = False
            self.outer.configure(highlightbackground=BORDER, highlightthickness=1)
            self.last_inside = time.time()

    def _peek_rect(self):
        left, top, right, bottom = self.work
        fx, fy = self.full_geom
        width, height = self.width, self.height
        if self.edge == "left":
            return (left, fy, left + PEEK, fy + height)
        if self.edge == "right":
            return (right - PEEK, fy, right, fy + height)
        if self.edge == "top":
            return (fx, top, fx + width, top + PEEK)
        if self.edge == "bottom":
            return (fx, bottom - PEEK, fx + width, bottom)
        return (fx, fy, fx + width, fy + height)

    def _hide(self) -> None:
        if not self.edge:
            return
        left, top, right, bottom = self.work
        fx, fy = self.full_geom
        width, height = self.width, self.height
        if self.edge == "left":
            px, py = left - (width - PEEK), fy
        elif self.edge == "right":
            px, py = right - PEEK, fy
        elif self.edge == "top":
            px, py = fx, top - (height - PEEK)
        else:
            px, py = fx, bottom - PEEK
        self.hidden = True
        self.hover_armed = False
        self.hide_time = time.time()
        self.pending_geom = request_geometry(self.root, width, height, px, py)
        self.outer.configure(highlightbackground=ACCENT, highlightthickness=2)
        self.root.lift()

    def _expand(self) -> None:
        if not self.edge:
            return
        fx, fy = self.full_geom
        self.pending_geom = request_geometry(self.root, self.width, self.height, fx, fy)
        self.hidden = False
        self.hover_armed = True
        self.last_inside = time.time()
        self.outer.configure(highlightbackground=BORDER, highlightthickness=1)
        self.root.lift()

    @staticmethod
    def _inside(px, py, rect) -> bool:
        x1, y1, x2, y2 = rect
        return x1 <= px <= x2 and y1 <= py <= y2

    def _hover_check(self) -> None:
        if self.stop.is_set():
            return
        try:
            self.pending_geom = confirm_geometry(
                self.root, self.pending_geom, self.width, self.height
            )
            if self.popup is not None:
                self.last_inside = time.time()
                return
            px, py = self.root.winfo_pointerxy()
            if self.hidden and self.edge:
                if self._inside(px, py, self._peek_rect()):
                    if self.hover_armed or (time.time() - self.hide_time) > 1.2:
                        self._expand()
                else:
                    self.hover_armed = True
            elif self.edge and not self.dragging:
                rect = (
                    self.root.winfo_x(),
                    self.root.winfo_y(),
                    self.root.winfo_x() + self.width,
                    self.root.winfo_y() + self.height,
                )
                if self._inside(px, py, rect):
                    self.last_inside = time.time()
                elif (time.time() - self.last_inside) > HIDE_DELAY:
                    self._hide()
        finally:
            self.root.after(80, self._hover_check)

    def _close(self) -> None:
        self.stop.set()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    if "--once" in sys.argv:
        once()
    else:
        Overlay().run()
