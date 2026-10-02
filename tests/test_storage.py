"""SQLite history: v1 → v2 migration, round trip of Judge and Co-work runs, cascade delete."""

import json
import sqlite3

from omnicouncil import storage
from omnicouncil.agent import AgentResult
from omnicouncil.config import AgentSpec
from omnicouncil.orchestrate import RunOutcome


def make_outcome(mode="judge"):
    r = lambda name, out, **kw: AgentResult(name, ok=True, output=out, elapsed=1.0, **kw)  # noqa: E731
    if mode == "judge":
        v = r("Judge", "{}", extra={"confidence": "高", "consensus_score": 1.0, "final_answer": "A"})
        return RunOutcome([r("W1", "a"), r("W2", "b")], v, 0, first_verdict=v)
    rounds = [[r("W1", "r1a"), r("W2", "r1b")], [r("W1", "r2a"), r("W2", "r2b")]]
    v = r("Judge", "x", extra={"confidence": "高", "round": 2, "mode": "cowork", "final_answer": "B"})
    return RunOutcome(rounds[-1], v, 0, first_verdict=v, mode="cowork", rounds=rounds,
                             leader_rounds=[v], guidance=[])


def test_v1_database_is_migrated(tmp_path):
    db = tmp_path / "h.sqlite"
    con = sqlite3.connect(db)
    con.executescript(storage.SCHEMA)  # v1 schema: no mode column, no round_outputs
    con.execute("INSERT INTO sessions (title, created_at, updated_at) VALUES ('old', 1, 1)")
    con.execute("INSERT INTO runs (session_id, created_at, question, record) VALUES (1, 1, 'q', ?)",
                (json.dumps({"question": "q", "results": [], "verdict": None, "code": 0}),))
    con.commit()
    con.close()
    st = storage.HistoryStore(db)
    assert st.schema_version == storage.SCHEMA_VERSION
    runs = st.get_runs(1)
    assert runs[0]["mode"] == "judge" and runs[0]["rounds"] == []


def test_round_trip_judge_and_cowork(tmp_path):
    st = storage.HistoryStore(tmp_path / "h.sqlite")
    sid = st.create_session("t")
    spec = lambda n: AgentSpec(n, ["x", "{prompt}"])  # noqa: E731
    for mode in ("judge", "cowork"):
        st.add_run(sid, storage.outcome_to_record("q", [spec("W1"), spec("W2")], spec("Judge"), make_outcome(mode), 3.0))
    judge, cowork = (storage.record_to_outcome(r) for r in st.get_runs(sid))
    assert judge.mode == "judge" and judge.verdict.extra["final_answer"] == "A" and judge.rounds == []
    assert cowork.mode == "cowork" and [[x.output for x in rnd] for rnd in cowork.rounds] == [["r1a", "r1b"], ["r2a", "r2b"]]
    assert st.list_sessions()[0]["n_runs"] == 2


def test_delete_cascades_to_rounds(tmp_path):
    st = storage.HistoryStore(tmp_path / "h.sqlite")
    sid = st.create_session("t")
    spec = AgentSpec("W", ["x", "{prompt}"])
    st.add_run(sid, storage.outcome_to_record("q", [spec], spec, make_outcome("cowork"), 1.0))
    st.delete_session(sid)
    assert st._db.execute("SELECT COUNT(*) FROM round_outputs").fetchone()[0] == 0
