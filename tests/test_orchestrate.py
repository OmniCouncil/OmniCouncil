"""Judge and Co-work flows end to end with fake CLIs."""

import asyncio

from conftest import ECHO, static
from omnicouncil.cowork import run_cowork_loop
from omnicouncil.orchestrate import AgentSpec, EXIT_LEADER_FAILED, EXIT_NO_WORKERS, EXIT_OK, orchestrate

JUDGE_HIGH = '{"consensus_score": 1.0, "confidence": "high", "analysis": "ok", "final_answer": "FINAL"}'
JUDGE_LOW = '{"consensus_score": 0.5, "confidence": "low", "analysis": "split", "final_answer": "UNSURE"}'


def run(coro):
    return asyncio.run(coro)


def test_judge_mode_happy_path(agent):
    w = [agent("W1", static("answer one")), agent("W2", static("answer two"))]
    leader = agent("Judge", static(JUDGE_HIGH))
    events = []
    o = run(orchestrate("q", w, leader, on_event=lambda k, p: events.append(k)))
    assert o.code == EXIT_OK
    assert [r.output for r in o.results] == ["answer one", "answer two"]
    assert o.verdict.extra["final_answer"] == "FINAL" and o.verdict.extra["consensus_score"] == 1.0
    assert events.count("worker_done") == 2 and "leader_done" in events and "review_start" not in events


def test_failed_worker_is_reported_and_others_continue(agent):
    w = [agent("Good", static("fine")), agent("Bad", static("", code=3, stderr="boom"))]
    o = run(orchestrate("q", w, agent("Judge", static(JUDGE_HIGH))))
    good, bad = o.results
    assert good.ok and not bad.ok and bad.returncode == 3 and "boom" in bad.error
    assert o.code == EXIT_OK


def test_missing_cli_and_all_workers_failing(agent):
    missing = AgentSpec("Ghost", ["/nonexistent/cli", "{prompt}"])
    o = run(orchestrate("q", [missing], agent("Judge", static(JUDGE_HIGH))))
    assert o.code == EXIT_NO_WORKERS and not o.results[0].ok


def test_leader_failure(agent):
    o = run(orchestrate("q", [agent("W", static("a"))], agent("Judge", static("", code=1, stderr="down"))))
    assert o.code == EXIT_LEADER_FAILED


def test_low_confidence_triggers_review_by_other_vendor(agent):
    leader = agent("Judge", static(JUDGE_LOW), vendor="anthropic")
    same_vendor = agent("Same", static(JUDGE_HIGH), vendor="anthropic", tier="pro")
    other = agent("Other", static(JUDGE_HIGH), vendor="google", tier="pro")
    events = []
    o = run(orchestrate("q", [agent("W", static("a"))], leader, on_event=lambda k, p: events.append(k),
                               reviewer_pool=[leader, same_vendor, other]))
    assert o.review.name == "Other" and o.verdict.name == "Other"
    assert o.verdict.extra["reviewed"] and o.verdict.extra["first_confidence"] == "低"
    assert "review_start" in events


def test_review_disabled(agent):
    leader = agent("Judge", static(JUDGE_LOW))
    o = run(orchestrate("q", [agent("W", static("a"))], leader, review_on_low=False, reviewer_pool=[leader]))
    assert o.review is None and o.verdict.name == "Judge"


def test_prompt_with_shell_metacharacters_is_passed_verbatim(agent):
    tricky = "$(rm -rf /) `id` ; | & 'single' \"double\""
    o = run(orchestrate(tricky, [agent("Echo", ECHO)], agent("Judge", static(JUDGE_HIGH))))
    assert o.results[0].output == tricky


COWORK_WORKER = """
import re, sys
p = sys.argv[-1]
m = re.search(r"round (\\d+) of (\\d+)", p)
print(f"round={m.group(1) if m else 1} guided={'dispute guidance' in p.lower()}")
"""

COWORK_LEADER_LOW_THEN_HIGH = """
import sys
p = sys.argv[-1]
if "previous summary you issued" in p:
    print("### Consensus score: 1.0\\n### Confidence: High\\n### Final verdict\\nAGREED")
else:
    print("### Consensus score: 0\\n### Confidence: Low\\n### Final verdict\\nnone\\n### Dispute guidance\\n- settle units")
"""


def test_cowork_rounds_and_extension(agent):
    w = [agent("A", COWORK_WORKER), agent("B", COWORK_WORKER)]
    o = run(run_cowork_loop("q", w, agent("Lead", COWORK_LEADER_LOW_THEN_HIGH), rounds=3, max_rounds=4))
    assert o.mode == "cowork" and len(o.rounds) == 4
    assert [r.output for r in o.rounds[1]] == ["round=2 guided=False"] * 2
    assert [r.output for r in o.rounds[3]] == ["round=4 guided=True"] * 2
    assert o.guidance == ["- settle units"]
    assert [v.extra["confidence"] for v in o.leader_rounds] == ["低", "高"]
    assert o.verdict.extra["final_answer"] == "AGREED"


def test_cowork_without_extension_stops_at_base_rounds(agent):
    w = [agent("A", COWORK_WORKER), agent("B", COWORK_WORKER)]
    o = run(run_cowork_loop("q", w, agent("Lead", COWORK_LEADER_LOW_THEN_HIGH), rounds=2, max_rounds=4,
                                   extend_on_low=False))
    assert len(o.rounds) == 2 and len(o.leader_rounds) == 1


def test_cowork_drops_failed_worker(agent):
    w = [agent("A", COWORK_WORKER), agent("B", COWORK_WORKER), agent("Bad", static("", code=1, stderr="limit"))]
    o = run(run_cowork_loop("q", w, agent("Lead", static("### Confidence: High\n### Final verdict\nok")),
                                   rounds=3, max_rounds=3))
    assert [len(r) for r in o.rounds] == [3, 2, 2]
