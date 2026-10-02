"""Where OmniCouncil keeps its files.

* Source checkout (``pyproject.toml`` next to the package): ``./config.json`` and ``./data/`` in the repo,
  so running ``python gui.py`` from a clone keeps working as before.
* Installed package (``pipx install`` / ``pip install``): ``~/Library/Application Support/OmniCouncil``.
* ``OMNICOUNCIL_HOME`` overrides both.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parent


def _app_home() -> Path:
    env = os.environ.get("OMNICOUNCIL_HOME")
    if env:
        return Path(env).expanduser()
    if (SOURCE_ROOT / "pyproject.toml").is_file():
        return SOURCE_ROOT
    return Path.home() / "Library" / "Application Support" / "OmniCouncil"


APP_HOME = _app_home()
CONFIG_PATH = APP_HOME / "config.json"
DATA_DIR = APP_HOME / "data"
USER_PROMPTS_DIR = APP_HOME / "prompts"      # optional user overrides, same file names as the built-in prompts
TEMPLATE_PATH = PACKAGE_DIR / "config.template.json"
PROMPTS_DIR = PACKAGE_DIR / "prompts"
ASSETS_DIR = PACKAGE_DIR / "assets"
