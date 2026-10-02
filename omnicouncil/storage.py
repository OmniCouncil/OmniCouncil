"""会话历史的本地持久化（SQLite，位于 data/history.sqlite）。

sessions        一个会话（左侧列表的一项）
runs            会话中的一次提问：原始问题、模式、各 Worker 输出与耗时、Leader / 复审结果及确信度
round_outputs   Co-work 模式每一轮、每个 Worker 的输出（v2 新增），重建历史时按轮次还原讨论过程

数据库版本记录在 PRAGMA user_version 中，打开时自动迁移旧库。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .agent import AgentResult
from .config import AgentSpec
from .orchestrate import RunOutcome
from .parsing import parse_judge_output
from .paths import DATA_DIR

DEFAULT_DB_PATH = DATA_DIR / "history.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    question    TEXT NOT NULL,
    record      TEXT NOT NULL          -- JSON，见 outcome_to_record()
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, id);
"""

SCHEMA_VERSION = 2

ROUND_OUTPUTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS round_outputs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    round       INTEGER NOT NULL,
    agent       TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    elapsed     REAL NOT NULL DEFAULT 0,
    result      TEXT NOT NULL      -- JSON，见 result_to_dict()
);
CREATE INDEX IF NOT EXISTS idx_round_outputs_run ON round_outputs(run_id, round, id);
"""


# ---------------------------------------------------------------------------
# AgentResult / RunOutcome <-> JSON
# ---------------------------------------------------------------------------


def result_to_dict(r: Optional[AgentResult]) -> Optional[dict]:
    if r is None:
        return None
    return {"name": r.name, "ok": r.ok, "output": r.output, "error": r.error, "elapsed": round(r.elapsed, 2),
            "returncode": r.returncode, "extra": r.extra}


def result_from_dict(d: Optional[dict]) -> Optional[AgentResult]:
    if d is None:
        return None
    r = AgentResult(name=d["name"], ok=d["ok"], output=d.get("output", ""), error=d.get("error", ""),
                    elapsed=d.get("elapsed", 0.0), returncode=d.get("returncode"), extra=d.get("extra") or {})
    # Markdown (non-JSON) verdicts are re-parsed from the full output, which is always stored: older versions cut
    # the final answer at the first sub-heading, and re-parsing repairs history saved by those versions.
    if r.ok and r.extra.get("structured") is False and r.output:
        r.extra["final_answer"] = parse_judge_output(r.output)["final_answer"]
    return r


def outcome_to_record(question: str, workers: list[AgentSpec], leader: AgentSpec, outcome: RunOutcome,
                      total_elapsed: float, reviewer_name: Optional[str] = None) -> dict:
    """转为可入库的记录。Co-work 的逐轮输出放在 record["rounds"]，入库时拆到 round_outputs 表。"""
    return {
        "version": 2,
        "mode": outcome.mode,
        "question": question,
        "workers": [w.name for w in workers],
        "leader": leader.name,
        "reviewer": reviewer_name,
        "code": outcome.code,
        "total_elapsed": round(total_elapsed, 2),
        "results": [result_to_dict(r) for r in outcome.results],
        "first_verdict": result_to_dict(outcome.first_verdict),
        "review": result_to_dict(outcome.review),
        "verdict": result_to_dict(outcome.verdict),
        "leader_rounds": [result_to_dict(r) for r in outcome.leader_rounds],
        "guidance": list(outcome.guidance),
        "rounds": [[result_to_dict(r) for r in rnd] for rnd in outcome.rounds],
    }


def record_to_outcome(record: dict) -> RunOutcome:
    return RunOutcome(
        results=[result_from_dict(r) for r in record.get("results", [])],
        verdict=result_from_dict(record.get("verdict")),
        code=record.get("code", 0),
        first_verdict=result_from_dict(record.get("first_verdict")),
        review=result_from_dict(record.get("review")),
        mode=record.get("mode", "judge"),
        rounds=[[result_from_dict(r) for r in rnd] for rnd in record.get("rounds", [])],
        leader_rounds=[result_from_dict(r) for r in record.get("leader_rounds", [])],
        guidance=list(record.get("guidance", [])),
    )


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class HistoryStore:
    def __init__(self, path: Path = DEFAULT_DB_PATH):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    @property
    def schema_version(self) -> int:
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        if self.schema_version < 2:
            cols = {row[1] for row in self._db.execute("PRAGMA table_info(runs)")}
            if "mode" not in cols:
                self._db.execute("ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'judge'")
            self._db.executescript(ROUND_OUTPUTS_SCHEMA)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    def list_sessions(self) -> list[dict]:
        """按最近更新时间倒序。"""
        rows = self._db.execute(
            "SELECT s.id, s.title, s.created_at, s.updated_at, COUNT(r.id) AS n_runs "
            "FROM sessions s LEFT JOIN runs r ON r.session_id = s.id "
            "GROUP BY s.id ORDER BY s.updated_at DESC, s.id DESC").fetchall()
        return [dict(r) for r in rows]

    def create_session(self, title: str) -> int:
        now = time.time()
        cur = self._db.execute("INSERT INTO sessions (title, created_at, updated_at) VALUES (?, ?, ?)",
                               (title, now, now))
        self._db.commit()
        return int(cur.lastrowid)

    def add_run(self, session_id: int, record: dict) -> int:
        now = time.time()
        record = dict(record)
        rounds = record.pop("rounds", [])  # 逐轮输出单独存表
        with self._db:  # 一个事务：run 与其逐轮输出要么都写入，要么都不写
            cur = self._db.execute(
                "INSERT INTO runs (session_id, created_at, question, record, mode) VALUES (?, ?, ?, ?, ?)",
                (session_id, now, record["question"], json.dumps(record, ensure_ascii=False),
                 record.get("mode", "judge")))
            run_id = int(cur.lastrowid)
            self._db.executemany(
                "INSERT INTO round_outputs (run_id, round, agent, ok, elapsed, result) VALUES (?, ?, ?, ?, ?, ?)",
                [(run_id, n, r["name"], int(r["ok"]), r.get("elapsed", 0.0), json.dumps(r, ensure_ascii=False))
                 for n, rnd in enumerate(rounds, 1) for r in rnd])
            self._db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        return run_id

    def get_rounds(self, run_id: int) -> list[list[dict]]:
        rows = self._db.execute("SELECT round, result FROM round_outputs WHERE run_id = ? ORDER BY round, id",
                                (run_id,)).fetchall()
        rounds: dict[int, list[dict]] = {}
        for row in rows:
            rounds.setdefault(row["round"], []).append(json.loads(row["result"]))
        return [rounds[n] for n in sorted(rounds)]

    def get_runs(self, session_id: int) -> list[dict]:
        rows = self._db.execute("SELECT id, record, created_at, mode FROM runs WHERE session_id = ? ORDER BY id",
                                (session_id,)).fetchall()
        out = []
        for row in rows:
            rec = json.loads(row["record"])
            rec["created_at"] = row["created_at"]
            rec.setdefault("mode", row["mode"])
            rec["rounds"] = self.get_rounds(row["id"])
            out.append(rec)
        return out

    def delete_session(self, session_id: int) -> None:
        self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self._db.commit()
