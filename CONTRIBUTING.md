# Contributing to OmniCouncil

First off, thank you for considering contributing to OmniCouncil! 🎉

OmniCouncil is a local-first, zero-API-cost Mixture-of-Agents (MoA) desktop environment designed for high-signal-to-noise ratio AI consensus. To keep the project stable, secure, and performant, please follow these guidelines.

## 🛠️ Local Development Setup (本地开发环境搭建)
1. Fork the repository and clone your fork locally.
2. Create a virtual environment: `python3 -m venv .venv`
3. Activate it and install core dependencies: `pip install -r requirements.txt`
4. Run the desktop app: `python gui.py`

## 🤖 Adding a New Model CLI (添加新的本地/云端模型桥接)
A major goal of OmniCouncil is interoperability. If you are adding support for a new CLI (e.g., Ollama, LM Studio, or a new browser-based bridge):
- Ensure your CLI can run non-interactively with plain stdout output. For the Warm Pool, it must also offer a structured streaming mode (newline-delimited JSON on stdin/stdout with an explicit end-of-turn event, like `--input-format stream-json`); interactive REPLs are not supported.
- If the new CLI modifies the file system, ensure you inject sandbox flags (like `--tools ""`) to prevent data contamination.
- Update `omnicouncil/config.template.json` with the new default `available_models` and `model_flag`.

## 🚀 Pull Request Process (提交 PR 流程)
1. Create a new branch (`git checkout -b feature/your-feature-name`).
2. Ensure all temporary caches, API keys, and local SQLite databases in the `data/` folder are excluded.
3. Run the test suite (`pip install -e ".[dev]"`, then `pytest` and `python -m pyflakes omnicouncil tests`). Add tests for new behavior; they must not call real models.
4. Commit your changes with a clear commit message.
5. Push to your fork and open a Pull Request against the `main` branch.
6. Ensure all items in the PR template checklist are verified.
