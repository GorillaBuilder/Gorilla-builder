from __future__ import annotations

from .config import (
    CHARS_PER_TOKEN,
    DEEP_THINK_MODEL,
    DEEP_THINK_TRIGGER,
    MAX_CONTEXT_TOKENS,
    MODE_INSTRUCTIONS,
    MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_URL,
    PLANNER_MODEL,
    SITE_NAME,
    SITE_URL,
    SMART_MODEL,
    TOTAL_BATCH_LIMIT,
    VISION_MODEL,
    WRITE_BATCH_LIMIT,
    _matches_deep_think_trigger,
)
from .logging_ import (
    _external_log_callback,
    _render_token_limit_message,
    log_agent,
    set_log_callback,
)
from .history import (
    HISTORY_CAP,
    _append_history,
    _get_history,
    _HISTORY,
    _norm_role,
    clear_history,
)
from .token_substitution import TokenSubstitution
from .tools import AGENT_TOOL_DEFS, _format_tools_for_prompt
from .context import (
    _compress_history,
    _estimate_tokens,
    _filter_observation,
    _token_estimate_cache,
)
from .prompts import (
    DEBUG_ADDON,
    SUPABASE_ADDON,
    SYSTEM_PROMPT_BODY,
    _build_expander_system,
    _build_planner_system,
    _build_skills_block,
    expand_prompt,
    generate_plan,
    review_output,
)
from .parsing import (
    _is_safe,
    _parse_response,
    _parse_xml_functions,
    _repair_qwen3_xml,
    _strip_param_value,
    _TOOL_CALL_BLOCK_RE,
    _TOOL_CATEGORY,
)
from .llm import _call_llm, _fetch_model_price, _pick_model
from .core import Agent, LineageAgent

__all__ = [
    "LineageAgent",
    "Agent",
    "set_log_callback",
    "log_agent",
    "_render_token_limit_message",
    "_append_history",
    "_get_history",
    "clear_history",
    "TokenSubstitution",
    "expand_prompt",
    "generate_plan",
    "review_output",
    "_filter_observation",
    "_parse_response",
    "_repair_qwen3_xml",
    "AGENT_TOOL_DEFS",
    "WRITE_BATCH_LIMIT",
    "TOTAL_BATCH_LIMIT",
    "BATCH_LIMIT",
]

BATCH_LIMIT = WRITE_BATCH_LIMIT
