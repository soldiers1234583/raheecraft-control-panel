#!/usr/bin/env python3
"""
Run the RaheeCraft Control Panel backend.

The README/guide workflow is `cp .env.example .env`, edit it, then start the
panel. Nothing loaded that file, so the panel silently started with no admin
password (login impossible) and the default port. This entry point loads
`.env` before importing the app.

Real environment variables always win: values from `.env` are only used for
keys that are not already set, so the Kaggle notebook cell (which passes the
environment explicitly) keeps full control.
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)


def load_env_file(path: str = None) -> int:
    """Load KEY=VALUE lines from `.env` into os.environ (setdefault)."""
    env_path = path or os.path.join(PROJECT_ROOT, ".env")
    if not os.path.isfile(env_path):
        return 0
    loaded = 0
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
                    loaded += 1
    except OSError:
        return loaded
    return loaded


if __name__ == "__main__":
    count = load_env_file()
    print(f"RaheeCraft panel: loaded {count} setting(s) from .env")

    from backend.app import main

    main()
