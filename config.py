from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
]

TOKEN_FILE = ROOT / "token.json"
CREDENTIALS_FILE = ROOT / "credentials.json"

_dotenv: dict[str, str] = {}


def _read_dotenv() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", ":"):
            if sep in line:
                key, value = line.split(sep, 1)
                normalized = re.sub(r"[^A-Z0-9]", "_", key.strip().upper())
                values[normalized] = value.strip().strip('"').strip("'")
                break
    return values


def env(key: str, default: str | None = None) -> str | None:
    if os.environ.get(key):
        return os.environ[key]
    if key in _dotenv:
        return _dotenv[key]
    return _dotenv.get(_ALIASES.get(key, "")) or default


_ALIASES = {
    "APIFY_TOKEN": "APIFY",
    "APIFY_TOKEN_BACKUP": "APIFY_BACKUP",
    "OPENROUTER_API_KEY": "OPENROUTER",
    "OPENROUTER_MODEL": "OPENROUTER_MODEL",
    "HUNTER_API_KEY": "HUNTER",
}


def load_config() -> dict:
    with (ROOT / "config.yaml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def save_config(data: dict) -> None:
    with (ROOT / "config.yaml").open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)


_dotenv.update(_read_dotenv())

config = load_config()
