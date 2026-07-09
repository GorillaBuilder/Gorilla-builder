"""
Lineage Agent v18.1 — Claude Code-style power, MiMo-native (Qwen3 XML)
======================================================================

v18.1 patch notes (from v18):
  - run() now returns "turn_tokens" (per-call delta) in addition to "tokens"
    (cumulative). Callers should bill on turn_tokens to avoid double-charging
    when the same agent instance is reused across reviewer/fix passes.

Major upgrades from v17 (unchanged):
  - 8 tools (was 3): write_file, edit_file, run_bash, read_files, list_dir,
    grep_search, glob_files, web_search, web_fetch, mark_done
  - Differentiated batch limits: writes capped at 3 per turn (was 2), but
    read/search/exploration tools are UNLIMITED per turn.
  - edit_file does surgical str_replace edits (cheap, fast, focused)
  - grep_search uses ripgrep (rg) for blazing-fast code search
  - web_search / web_fetch for live docs lookup (MCP-style)
"""

from __future__ import annotations

import os
import re
from typing import Dict

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

MODEL           = os.getenv("LINEAGE_MODEL",     "xiaomi/mimo-v2.5")
SMART_MODEL     = os.getenv("SMART_MODEL",       "xiaomi/mimo-v2.5-pro")
PLANNER_MODEL   = os.getenv("PLANNER_MODEL",     "xiaomi/mimo-v2.5-pro")
VISION_MODEL    = os.getenv("VISION_MODEL",      "xiaomi/mimo-v2.5")
DEEP_THINK_MODEL = os.getenv("DEEP_THINK_MODEL", "xiaomi/mimo-v2.5-pro")

# ---------------------------------------------------------------------------
# Think-mode selection
# ---------------------------------------------------------------------------
# Callers (app.py) pass an explicit think_mode — "fast" | "normal" | "deep" —
# through the API instead of embedding a magic instruction string in the
# user's visible prompt. LineageAgent uses it to (a) pick DEEP_THINK_MODEL
# for "deep" and (b) prepend the matching steering instruction to the LLM's
# task text server-side, so the user's stored prompt/chat history stays
# exactly what they typed.
DEEP_THINK_TRIGGER = (
    "TAKE YOUR TIME AND THINK DEEPLY. Before writing any code, carefully "
    "plan your architecture, reason through edge cases, and think about "
    "potential failure points. Quality and thoroughness over speed and "
    "do this:"
)

MODE_INSTRUCTIONS: Dict[str, str] = {
    "fast":   "BE TOKEN EFFICIENT AND FAST AND DO THIS:\n\n",
    "deep":   DEEP_THINK_TRIGGER + "\n\n",
    "normal": "",
}


def _matches_deep_think_trigger(text: str) -> bool:
    """Whitespace-insensitive, case-insensitive substring check.

    Kept as a fallback for any caller still embedding the legacy trigger
    phrase directly in the prompt text; new callers should pass an explicit
    think_mode="deep" to LineageAgent.run() instead.
    """
    if not text:
        return False
    norm_text    = re.sub(r"\s+", " ", text).strip().lower()
    norm_trigger = re.sub(r"\s+", " ", DEEP_THINK_TRIGGER).strip().lower()
    return norm_trigger in norm_text

OPENROUTER_URL = os.getenv(
    "OPENROUTER_URL",
    "https://openrouter.ai/api/v1/chat/completions",
).strip()
SITE_URL  = os.getenv("SITE_URL",  "https://gorillabuilder.dev").strip()
SITE_NAME = os.getenv("SITE_NAME", "Gorilla Builder")

MAX_CONTEXT_TOKENS = 230_000
CHARS_PER_TOKEN    = 4

# v18: writes capped at 3 (was 2). Reads/searches are unlimited.
WRITE_BATCH_LIMIT = 3
# Total tool calls per turn (safety cap — well above typical usage)
TOTAL_BATCH_LIMIT = 12

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY must be set")
