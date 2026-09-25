"""Optional, explicit import of the user's yolo provider key; never write it."""
from __future__ import annotations

import os
from pathlib import Path

from .client import AIConfig


def import_yolo_config() -> AIConfig:
    """Read only the authorized key, without shell expansion or OMP dependency."""
    key = os.environ.get("YOLO_AUTO_API_KEY", "").strip()
    if not key:
        try:
            lines = (Path.home() / ".omp" / "agent" / ".env").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            lines = []
        for line in lines:
            name, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and name.strip() == "YOLO_AUTO_API_KEY":
                key = value.strip()
                if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                    key = key[1:-1]
                break
    if not key:
        raise ValueError("No YOLO_AUTO_API_KEY found in the environment or ~/.omp/agent/.env.")
    return AIConfig(base_url="https://yolo-auto.com/v1", model="yolo", api_key=key)
