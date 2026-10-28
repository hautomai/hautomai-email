from __future__ import annotations

import re

from config import config


def _clean(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


def _match(patterns: list[str], text: str) -> bool:
    cleaned = _clean(text)
    for pattern in patterns:
        normalized = " ".join(_clean(pattern).split())
        if re.search(rf"(^|\s){re.escape(normalized)}($|\s)", cleaned):
            return True
    return False


def classify(text: str) -> str:
    reply = config["reply"]
    if _match(reply["negative_keywords"], text):
        return "negative"
    if _match(reply["positive_keywords"], text):
        return "positive"
    return "neutral"
