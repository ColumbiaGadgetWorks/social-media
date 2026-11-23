"""The running build's version, shown in the page header.

The image build passes STUDIO_VERSION (see the Dockerfile and .github/workflows/image.yml). Outside a
container it falls back to the commit count and short hash from git, then to "dev".
"""

import os
import subprocess
from pathlib import Path


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=Path(__file__).parent, capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def current() -> str:
    env = os.environ.get("STUDIO_VERSION", "").strip()
    if env and env != "dev":
        run, _, sha = env.partition("-")
        return f"{run}-{sha[:7]}" if sha else run
    count, sha = _git("rev-list", "--count", "HEAD"), _git("rev-parse", "--short", "HEAD")
    return f"{count}-{sha}" if count and sha else "dev"


VERSION = current()
