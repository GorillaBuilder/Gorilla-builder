from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from .config import (
    MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_URL,
    SITE_NAME,
    SITE_URL,
    SMART_MODEL,
)
from .context import _compress_history
from .logging_ import log_agent

# ═══════════════════════════════════════════════════════════════════════════
#  Live model price fetching
# ═══════════════════════════════════════════════════════════════════════════

_model_price_cache:     Dict[str, Tuple[float, float]] = {}
_model_price_cache_ttl: Dict[str, float]               = {}
_PRICE_CACHE_TTL_S = 300


async def _fetch_model_price(model: str) -> Tuple[float, float]:
    now = time.monotonic()
    cached_at = _model_price_cache_ttl.get(model, 0)
    if model in _model_price_cache and (now - cached_at) < _PRICE_CACHE_TTL_S:
        return _model_price_cache[model]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "HTTP-Referer":  SITE_URL,
                    "X-Title":       SITE_NAME,
                },
            )
            resp.raise_for_status()
            models_list = resp.json().get("data", [])

        for entry in models_list:
            if entry.get("id") == model:
                pricing          = entry.get("pricing", {})
                prompt_price     = float(pricing.get("prompt",     0) or 0)
                completion_price = float(pricing.get("completion", 0) or 0)
                _model_price_cache[model]     = (prompt_price, completion_price)
                _model_price_cache_ttl[model] = now
                log_agent(
                    "agent",
                    f"Price fetched — {model}: ${prompt_price}/pt ${completion_price}/ct",
                )
                return (prompt_price, completion_price)

        log_agent("agent", f"Model '{model}' not found in /api/v1/models — price=0")

    except Exception as e:
        log_agent("agent", f"Price fetch failed for '{model}': {e} — defaulting to 0")

    _model_price_cache[model]     = (0.0, 0.0)
    _model_price_cache_ttl[model] = now
    return (0.0, 0.0)


# ═══════════════════════════════════════════════════════════════════════════
#  LLM call
# ═══════════════════════════════════════════════════════════════════════════

async def _call_llm(
    messages:    list,
    model:       str   = MODEL,
    temperature: float = 0.6,
) -> Tuple[str, int]:
    messages = _compress_history(messages)

    api_messages = []
    for m in messages:
        api_msg = {"role": m["role"], "content": m.get("content", "")}
        api_messages.append(api_msg)

    payload: Dict[str, Any] = {
        "model":       model,
        "messages":    api_messages,
        "max_tokens":  16000,
        "temperature": temperature,
        # OpenRouter now always returns usage.cost; flag kept for proxies
        # (OPENROUTER_URL is overridable) that still gate cost behind it.
        "usage":       {"include": True},
        "provider": {
            "order":           ["xiaomi", "fireworks", "alibaba", "novita"],
            "allow_fallbacks": False,
        },
    }

    if any(x in model.lower() for x in ["mimo", "qwen", "minimax"]):
        payload["reasoning"] = {"exclude": False}

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  SITE_URL,
        "X-Title":       SITE_NAME,
    }

    data = None
    for attempt in range(5):
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            if resp.status_code == 429:
                wait = 0.5 * (attempt + 1)
                log_agent("agent", f"429 — waiting {wait}s (attempt {attempt + 1}/5)")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break

    if data is None:
        raise RuntimeError("LLM call failed after 5 attempts (persistent 429)")

    choice  = data["choices"][0]
    msg     = choice["message"]
    content = msg.get("content") or ""

    reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
    if reasoning and "<tool_call>" not in content and "<tool_call>" in str(reasoning):
        content = str(reasoning) + "\n\n" + content
    elif reasoning and "<tool_call>" not in content and not content.strip():
        content = str(reasoning)

    u = data.get("usage", {})
    prompt_tokens     = u.get("prompt_tokens",     0)
    completion_tokens = u.get("completion_tokens", 0)

    prompt_details = u.get("prompt_tokens_details") or {}
    cached_tokens  = int(prompt_details.get("cached_tokens", 0) or 0)

    # ─── BILLING: use OpenRouter's actual billed cost (`usage.cost`, USD),
    # which already reflects prompt-cache discounts, cache writes, and the
    # exact upstream provider's pricing. Conversion: 1¢ = 10,000 tokens,
    # i.e. tokens = USD × 1_000_000 — numerically identical to the old µ$
    # unit, so downstream accounting (turn_tokens / total_tokens) is
    # unchanged. Falls back to the local token×price estimate only if the
    # gateway doesn't return `cost` (e.g. a proxy set via OPENROUTER_URL).
    billed_usd = u.get("cost")
    if billed_usd is not None:
        cost_details = u.get("cost_details") or {}
        billed_usd = float(billed_usd or 0) + float(
            cost_details.get("upstream_inference_cost") or 0  # BYOK only
        )
    else:
        fresh_prompt_tokens = max(0, prompt_tokens - cached_tokens)
        prompt_price, completion_price = await _fetch_model_price(model)
        billed_usd = (
            fresh_prompt_tokens * prompt_price
            + cached_tokens     * prompt_price * 0.1
            + completion_tokens * completion_price
        )

    weight = int(round(billed_usd * 1_000_000))  # 1¢ = 10k tokens

    cache_note = f" cached={cached_tokens}" if cached_tokens else ""
    log_agent(
        "agent",
        f"usage p={prompt_tokens}{cache_note} c={completion_tokens} "
        f"billed=${billed_usd:.6f} → {weight} tok (1¢=10k)",
    )

    return content, weight


# ═══════════════════════════════════════════════════════════════════════════
#  Smart model routing
# ═══════════════════════════════════════════════════════════════════════════

_HARD_OBSERVATION_SIGNALS = frozenset([
    "error ts", "syntaxerror", "cannot find", "could not be resolved",
    "is not defined", "is not a function", "failed to compile",
    "exit code: 1", "exit code: -1", "enoent", "module not found",
    "cannot read propert", "typeerror", "referenceerror",
])

_HARD_DOMAIN_SIGNALS = frozenset([
    "auth", "supabase", "migration", "rls", "policy", "realtime",
    "subscription", "webhook", "oauth", "race condition", "async",
    "promise", "cors", "jwt", "token", "session", "cookie",
    "database", "schema", "foreign key", "join",
])


def _pick_model(
    turn: int,
    previous_output: Optional[str],
    user_request: str,
    is_debug: bool,
) -> str:
    obs = (previous_output or "").lower()
    req = (user_request    or "").lower()

    if turn == 0:
        return SMART_MODEL
    if any(sig in obs for sig in _HARD_OBSERVATION_SIGNALS):
        return SMART_MODEL
    if turn <= 4 and any(sig in req for sig in _HARD_DOMAIN_SIGNALS):
        return SMART_MODEL
    if is_debug and previous_output:
        return SMART_MODEL

    return MODEL
