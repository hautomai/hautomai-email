from __future__ import annotations

import json
import re

import httpx

from config import ROOT, config, env

SYSTEM_PROMPT_ICEBREAKER = (
    "You craft one personalisation line for a cold email to a PPC agency: a sincere "
    "compliment about one of their client case studies (or a recent win / highlight found "
    "on their own website: a company update, their biggest service, or their years of "
    "experience). Analyse the website text below and return JSON only, no markdown "
    "fences:\n"
    '{"client": "<named client company found on the site, or null>", '
    '"result": "<concrete number or outcome, or null>", '
    '"phrase": "<full compliment sentence, 6-12 words, about the case study>"}\n'
    'Rules: the "phrase" is a complete sentence and MUST contain the named client OR a '
    "concrete metric (number, %, £) OR the years of experience. Never mention the "
    "agency's own name inside the phrase. Generic service wording (\"PPC management\", "
    "\"Google Ads management\") is NOT a proof point on its own. It will be inserted as: "
    '"I\'ve been checking your portfolio for a hot minute. {phrase}. Congrats!\' - it must '
    'read naturally there and sound like an impressive, specific achievement. Examples: '
    '{"client": "Yodel", "result": "34% conversion lift", "phrase": '
    '"The Yodel campaign with a 34% conversion lift is genuinely impressive."} / '
    '{"client": "MuscleFood", "result": "3.2x ROAS", "phrase": "That MuscleFood 3.2x ROAS '
    'breakthrough is seriously strong work."} / {"client": null, "result": "18 years", '
    '"phrase": "18 years in paid media is a proper track record."}. If no specific client '
    "or number exists, still pick the most concrete specific claim available. Never invent "
    "anything."
)

SYSTEM_PROMPT_STRICT = (
    "Strict output rule: reply with ONLY the compliment sentence (6-12 words) about one "
    "of their client case studies. It MUST include a real client name from the site or a "
    "concrete number/metric (%, £, x) or their years of experience. It will be inserted "
    'after "I\'ve been checking your portfolio for a hot minute. " and before ". '
    'Congrats!". No quotes, no explanation, no agency name.'
)


def call_llm(
    system: str, user: str, model: str | None = None, max_tokens: int | None = None
) -> str:
    api_key = env("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY missing in .env or environment")
    llm = config["llm"]
    payload = {
        "model": model or env("OPENROUTER_MODEL") or llm["model"],
        "temperature": llm.get("temperature", 0.3),
        "max_tokens": max_tokens or llm.get("max_tokens", 350),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    response = httpx.post(
        f"{llm['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    response.raise_for_status()
    message = response.json()["choices"][0]["message"]
    content = message.get("content") or ""
    if not content:
        raise RuntimeError("Empty LLM response")
    return content.strip()


def _extract_fields(raw: str) -> tuple[str, str]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and data.get("phrase"):
            return str(data["phrase"]).strip(), str(data.get("client") or "").strip()
    except (json.JSONDecodeError, ValueError):
        pass
    return cleaned, ""


GENERIC_WORDS = {
    "google", "ads", "ppc", "seo", "media", "paid", "marketing", "agency",
    "management", "services", "service", "campaigns", "campaign", "results",
    "result", "performance", "digital", "search", "growth", "getting", "with",
    "for", "and", "their", "our", "new", "best", "top", "using", "from", "on",
    "roi", "crm", "data", "approach", "strategies", "strategy", "solutions",
    "solutions", "maximise", "maximize", "drive", "driven",
}


def _valid_phrase(phrase: str, agency_hint: str = "") -> bool:
    words = phrase.split()
    if not 6 <= len(words) <= 16:
        return False
    dropped = [w for w in words if not agency_hint or agency_hint.lower() not in w.lower()]
    remaining = " ".join(dropped)
    if any(ch.isdigit() for ch in remaining) or "£" in remaining or "%" in remaining:
        return True
    return any(w[0].isupper() and len(w) >= 3 and w.strip("'s.").lower() not in GENERIC_WORDS for w in dropped)


def _cleanup(result: str) -> str:
    result = result.strip().strip('"').strip("'")
    if not result:
        return result
    if result[0].islower():
        result = result[0].upper() + result[1:]
    return result[:160]


def _generate_phrase(
    user: str,
    icebreaker_model: str,
    base_model: str,
    agency_hint: str,
) -> tuple[str, str]:
    try:
        phrase, client = _extract_fields(call_llm(SYSTEM_PROMPT_ICEBREAKER, user, model=icebreaker_model, max_tokens=800))
        if not _valid_phrase(phrase, agency_hint):
            phrase, client = _extract_fields(call_llm(SYSTEM_PROMPT_STRICT, user, model=icebreaker_model, max_tokens=800))
        if not _valid_phrase(phrase, agency_hint):
            phrase, client = _extract_fields(call_llm(SYSTEM_PROMPT_ICEBREAKER, user, model=base_model, max_tokens=800))
    except Exception:
        try:
            phrase, client = _extract_fields(call_llm(SYSTEM_PROMPT_ICEBREAKER, user, model=base_model, max_tokens=800))
        except Exception:
            return "", ""
    if not _valid_phrase(phrase, agency_hint):
        return "", ""
    return _cleanup(phrase)[:120], (_cleanup(client)[:120] if client else "")


def icebreaker(
    site: dict | None,
    agency_hint: str = "",
    model: str | None = None,
    exclude: list[str] | None = None,
) -> tuple[str, str]:
    fallback = config["outbound"].get(
        "fallback_icebreaker",
        "Been checking out your company on LinkedIn, your growth so far piqued my curiosity. Had to send you this!",
    )
    if not config["llm"].get("enabled", True):
        return fallback, ""
    if not site or len(site.get("text", "")) < 80:
        return fallback, ""
    user = f"Website: {site['url']}\n\n{site['text']}"
    if exclude:
        user += "\n\nAlready-used angles for this company (choose a different one, do NOT repeat):\n- " + "\n- ".join(exclude)
    icebreaker_model = model or config["llm"].get("icebreaker_model")
    base_model = env("OPENROUTER_MODEL") or config["llm"].get("model")
    used = {p.strip().lower() for p in (exclude or []) if p}
    for attempt in range(3):
        prompt = user
        if attempt:
            prompt += f"\n\nIMPORTANT: a previous attempt gave '{last_phrase}' which is not acceptable. You MUST pick a clearly different angle now."
        phrase, client = _generate_phrase(prompt, icebreaker_model, base_model, agency_hint)
        last_phrase = phrase
        if not phrase:
            continue
        if phrase.lower() not in used:
            return phrase, client
    return fallback, ""


def render(template_text: str, **variables) -> str:
    def _substitute(match: re.Match) -> str:
        return str(variables.get(match.group(1).strip(), ""))
    return re.sub(r"\{\{(.*?)\}\}", _substitute, template_text)


def load_template(name: str) -> tuple[str, str]:
    path = ROOT / "templates" / f"{name}.md"
    content = path.read_text(encoding="utf-8")
    first, separator, rest = content.partition("\n")
    if separator and first.startswith("Subject:"):
        return first[len("Subject:"):].strip(), rest.strip()
    return "", content.strip()