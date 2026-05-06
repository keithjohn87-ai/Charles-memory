"""Charles configuration. Loads .env and exposes constants."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT / "workspace"
LOGS = ROOT / "logs"

load_dotenv(ROOT / ".env")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_TELEGRAM_ID = int(os.environ["OWNER_TELEGRAM_ID"])
MLX_BASE_URL = os.environ.get("MLX_BASE_URL", "http://127.0.0.1:8080/v1")
MLX_MODEL = os.environ.get("MLX_MODEL", "mlx-community/Qwen3.6-35B-A3B-4bit")

# Created at runtime so charles.py can boot from a fresh clone
for _d in (WORKSPACE, WORKSPACE / "memory", WORKSPACE / "projects", LOGS):
    _d.mkdir(parents=True, exist_ok=True)
