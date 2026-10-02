"""Grading helpers of evals/run_eval.py (no network, no models)."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("run_eval", Path(__file__).resolve().parent.parent / "evals" / "run_eval.py")
run_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_eval)


@pytest.mark.parametrize("text,expected", [
    ("Reasoning...\nAnswer: C", "C"),
    ("The answer is (D).", "D"),
    ("**Answer: B**", "B"),
    ("First I thought Answer: A but actually\nAnswer: E", "E"),
    ("Final answer = J", "J"),
    ("...\n\nG", "G"),
    ("Answer: K", None),                 # outside the 10 options
    ("No letter here", None),
    ("I can't answer that.", None),      # 'answer' followed by no letter
])
def test_extract_choice(text, expected):
    assert run_eval.extract_choice(text, 10) == expected


def test_extract_choice_respects_option_count():
    assert run_eval.extract_choice("Answer: J", 4) is None


@pytest.mark.parametrize("choices,expected", [
    (["A", "A", "B"], "A"), (["A", "B", None], None), (["C", None, None], "C"), ([None, None], None),
])
def test_majority(choices, expected):
    assert run_eval.majority(choices) == expected


def test_wilson_interval_bounds():
    lo, hi = run_eval.wilson(15, 20)
    assert 0.5 < lo < 0.75 < hi < 0.95
