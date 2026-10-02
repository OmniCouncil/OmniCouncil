"""Judge-output parsing, score normalization, guidance extraction and context injection."""

import pytest

from conftest import i18n
from omnicouncil.orchestrate import CONTEXT_TURNS, build_context_prompt
from omnicouncil.parsing import extract_guidance, normalize_score, parse_confidence, parse_judge_output

P = parse_judge_output


@pytest.mark.parametrize("text,expected", [
    ("### 确信度：低", "低"),
    ("### 置信度: **High**", "高"),
    ("Confidence: medium", "中"),
    ("确信度：【低】", "低"),
    ("### Confidence: <High>", "高"),
    ("no confidence here", None),
])
def test_parse_confidence(text, expected):
    assert parse_confidence(text) == expected


@pytest.mark.parametrize("value,expected", [
    ("1", 1.0), (1, 1.0), ("1.0", 1.0), ("0.5", 0.5), ("0.6", 0.5), ("0.8", 1.0), ("0.2", 0.0), ("0", 0.0),
    ("50%", 0.5), ("50", 0.5), ("100", 1.0),
    ("2.5", None), ("abc", None), (None, None), (-1, None),
])
def test_normalize_score(value, expected):
    assert normalize_score(value) == expected


def test_clean_json():
    r = P('{"consensus_score": 0.5, "confidence": "medium", "analysis": "A vs B", "final_answer": "**9.9**"}')
    assert r == {"consensus_score": 0.5, "confidence": "中", "final_answer": "**9.9**",
                 "analysis": "A vs B", "structured": True}


def test_fenced_json_with_prose():
    r = P('Sure:\n```json\n{"consensus_score": "1.0", "confidence": "高", "final_answer": "ok"}\n```\nThanks')
    assert (r["consensus_score"], r["confidence"], r["final_answer"], r["structured"]) == (1.0, "高", "ok", True)


def test_raw_newlines_and_escaped_quotes_inside_strings():
    r = P('{"consensus_score": 0, "confidence": "low", "final_answer": "line1\nline2 \\"q\\""}')
    assert r["final_answer"] == 'line1\nline2 "q"'
    assert r["consensus_score"] == 0.0 and r["structured"]


def test_braces_inside_strings_and_surrounding_text():
    r = P('prefix {"consensus_score": 0.5, "analysis": "has {braces}", "final_answer": "ok"} suffix')
    assert r["final_answer"] == "ok" and r["analysis"] == "has {braces}"


def test_truncated_json_recovers_partial_answer():
    r = P('{"consensus_score": 0.5, "confidence": "high", "analysis": "x", "final_answer": "Partial\\nanswer')
    assert not r["structured"]
    assert r["final_answer"] == "Partial\nanswer"
    assert (r["consensus_score"], r["confidence"]) == (0.5, "高")


def test_markdown_only_falls_back_to_sections():
    r = P("### 准确性评估\n都对\n\n### 共识度：1.0\n### 确信度：高\n\n### 最终定论\n9.9 更大。")
    assert (r["consensus_score"], r["confidence"], r["final_answer"]) == (1.0, "高", "9.9 更大。")


def test_english_markdown_consensus_score():
    r = P("### Consensus score: 0.5\n### Confidence: Medium\n### Final verdict\nX")
    assert (r["consensus_score"], r["confidence"], r["final_answer"]) == (0.5, "中", "X")


def test_plain_text_uses_whole_output():
    r = P("I think 9.9 is bigger.")
    assert r["final_answer"] == "I think 9.9 is bigger." and r["consensus_score"] is None


def test_confidence_falls_back_to_score():
    assert P('{"consensus_score": 0, "final_answer": "x"}')["confidence"] == "低"


def test_extract_guidance_section():
    text = "### Confidence: Low\n### Final verdict\nnone\n### Dispute guidance\n- check units\n- cite source"
    assert extract_guidance(text) == "- check units\n- cite source"


def test_extract_guidance_without_section_returns_whole_text():
    assert extract_guidance("### 确信度：低\n只有这些") == "### 确信度：低\n只有这些"


def test_build_context_prompt_formats_and_limits():
    assert build_context_prompt([], "q") == "q"
    out = build_context_prompt([("2+2?", "4."), ("times 3?", "12.")], "minus 5?")
    assert out == ("Previous conversation:\nUser: 2+2?\nAI: 4.\n\nUser: times 3?\nAI: 12.\n\n"
                   "Current question: minus 5?")
    many = [(f"q{i}", "a" * 5000) for i in range(10)]
    out = build_context_prompt(many, "now")
    assert out.count("User:") <= CONTEXT_TURNS
    assert "q9" in out and "q0" not in out


def test_build_context_prompt_chinese():
    i18n.set_language("zh")
    assert build_context_prompt([("问", "答")], "再问").startswith("此前的对话：\n用户: 问")


# —— Markdown sections with sub-headings (regression: Co-work final answer was cut at the first "####") ——

COWORK_VERDICT = """### 共识分析
一致。

### 共识度：1.0
### 确信度：中

### 最终定论
**前提**：没有任何名单能保证赚钱。

#### 五只候选与参考配置
| 股票 | 占比 |
|---|---|
| AMZN | 18% |

#### 执行纪律
1. 分批建仓。

以上内容不构成投资建议。
"""


def test_final_answer_keeps_sub_sections():
    fa = parse_judge_output(COWORK_VERDICT)["final_answer"]
    assert fa.startswith("**前提**") and "| AMZN | 18% |" in fa and "#### 执行纪律" in fa
    assert fa.endswith("以上内容不构成投资建议。")


def test_final_answer_stops_at_same_level_heading():
    text = COWORK_VERDICT + "\n### 争议指导意见\n- 核实财报日期\n"
    fa = parse_judge_output(text)["final_answer"]
    assert "执行纪律" in fa and "核实财报日期" not in fa


def test_guidance_keeps_sub_sections_and_stops_at_same_level():
    text = ("### 最终定论\n暂无\n### 争议指导意见\n#### 数据\n- 核实日期\n#### 方法\n- 统一口径\n"
            "### 附注\n不属于指导意见")
    g = extract_guidance(text)
    assert "核实日期" in g and "统一口径" in g and "不属于指导意见" not in g
