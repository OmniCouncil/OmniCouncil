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


def ensure_login_path() -> bool:
    """Apps launched from Finder get a minimal PATH (/usr/bin:/bin:…), so CLIs installed in ~/.local/bin,
    Homebrew or nvm wouldn't be found. If PATH looks minimal, adopt the PATH of the user's login shell.
    Returns True if PATH was changed."""
    import subprocess

    current = os.environ.get("PATH", "")
    if any(d in current for d in ("/opt/homebrew/bin", "/usr/local/bin", "/.local/bin")):
        return False
    shell = os.environ.get("SHELL") or "/bin/zsh"
    try:
        out = subprocess.run([shell, "-lic", "printf %s \"$PATH\""], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return False
    login_path = out[-1] if out else ""
    if not login_path or login_path == current:
        return False
    os.environ["PATH"] = login_path + (os.pathsep + current if current else "")
    return True
