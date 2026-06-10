"""Single source of truth for loading SUPABASE_DB_URL (and friends) from .env.

Every CLI in this package needs the DB URL but the repo has no python-dotenv
dependency, so each module used to carry its own tiny .env reader. This is that
reader, once: check ``./.env`` then ``<repo root>/.env``, and set any key that
isn't already in the environment (real env vars always win).
"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv() -> None:
    """Populate os.environ from the first .env found (cwd, then repo root)."""
    for env_path in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        break
