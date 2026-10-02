# Evaluation pilot (2026-10) — TIGER-Lab/MMLU-Pro (test)

- Questions: 20 (seed 7); Co-work on the first 10
- Workers: Claude:default, Gemini:gemini-3.8-flash-medium, Codex:gpt-5.6-sol · Leader: Claude Opus · second review: True · Co-work rounds: 3
- Run started: 2026-10-01 22:43

| System | Accuracy | 95% CI | n | Unparsed / failed | Median time / question | Model calls / question |
|---|---|---|---|---|---|---|
| single:Claude | **19/20 = 95%** | 76%–99% | 20 | 0 | 4.0s | 1.0 |
| single:Claude Opus (alone) | **18/20 = 90%** | 70%–97% | 20 | 0 | 4.8s | 1.0 |
| single:Codex | **17/20 = 85%** | 64%–95% | 20 | 0 | 14.7s | 1.0 |
| single:Gemini | **16/20 = 80%** | 58%–92% | 20 | 3 | 9.9s | 1.0 |
| cowork | **9/10 = 90%** | 60%–98% | 10 | 0 | 65.7s | 9.9 |
| judge | **20/20 = 100%** | 84%–100% | 20 | 0 | 26.1s | 4.1 |
| majority | **17/20 = 85%** | 64%–95% | 20 | 2 | 15.9s | 3.0 |

- Judge right where the majority vote was wrong or tied: **3**
- Judge wrong where the majority vote was right: **0**
- Second reviews triggered: 2
- On the 10 questions run in both modes: Judge 10/10, Co-work 9/10

*Small samples give wide confidence intervals; treat differences inside the CI as noise.*
