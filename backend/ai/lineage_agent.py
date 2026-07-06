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
import json
import time
import asyncio
import hashlib
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

MODEL           = os.getenv("LINEAGE_MODEL",     "xiaomi/mimo-v2.5")
SMART_MODEL     = os.getenv("SMART_MODEL",       "xiaomi/mimo-v2.5-pro")
PLANNER_MODEL   = os.getenv("PLANNER_MODEL",     "xiaomi/mimo-v2.5-pro")
VISION_MODEL    = os.getenv("VISION_MODEL",      "xiaomi/mimo-v2.5")
DEEP_THINK_MODEL = os.getenv("DEEP_THINK_MODEL", "xiaomi/mimo-v2.5-pro")

# When this exact phrase appears in the user's prompt, every turn of that
# request (and any follow-up tool turns in the same agent instance) routes
# to DEEP_THINK_MODEL — overriding the smart/base picker. Match is
# whitespace-insensitive and case-insensitive but otherwise verbatim.
DEEP_THINK_TRIGGER = (
    "TAKE YOUR TIME AND THINK DEEPLY. Before writing any code, carefully "
    "plan your architecture, reason through edge cases, and think about "
    "potential failure points. Quality and thoroughness over speed and "
    "do this:"
)


def _matches_deep_think_trigger(text: str) -> bool:
    """Whitespace-insensitive, case-insensitive substring check."""
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


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_external_log_callback = None


def set_log_callback(cb):
    global _external_log_callback
    _external_log_callback = cb


def log_agent(role: str, message: str, project_id: str = "") -> None:
    prefix = f"[{project_id[:8]}]" if project_id else "[AGENT]"
    ts = time.strftime("%H:%M:%S")
    c = {
        "agent":    "\033[94m",
        "llm":      "\033[90m",
        "system":   "\033[97m",
        "debugger": "\033[91m",
    }.get(role.lower(), "\033[94m")
    print(
        f"\033[90m{ts}\033[0m {prefix} {c}{role.upper()}\033[0m: "
        f"{message[:300]}{'...' if len(message) > 300 else ''}"
    )
    if _external_log_callback and project_id and role.lower() != "llm":
        try:
            _external_log_callback(project_id, role.lower(), message)
        except Exception:
            pass


def _render_token_limit_message() -> str:
    return (
        '<div style="display:flex;flex-direction:column;align-items:center;'
        'justify-content:center;padding:40px 30px;'
        'background:linear-gradient(135deg,rgba(15,23,42,0.9),rgba(30,10,50,0.8));'
        'border:1px solid rgba(217,70,239,0.3);border-radius:20px;'
        'text-align:center;max-width:400px;margin:20px auto;'
        'box-shadow:0 20px 60px rgba(0,0,0,0.5);">'
        '<h2 style="color:#fff;font-size:24px;font-weight:700;margin:0 0 12px;">'
        "Token Limit Reached</h2>"
        '<p style="color:#94a3b8;font-size:14px;line-height:1.6;margin:0 0 28px;'
        'max-width:280px;">Upgrade to Premium for unlimited access.</p>'
        '<a href="/pricing" style="background:linear-gradient(135deg,#d946ef,#a855f7);'
        'color:white;text-decoration:none;padding:14px 32px;border-radius:12px;'
        'font-size:14px;font-weight:600;">Upgrade to Premium</a></div>'
    )


# ---------------------------------------------------------------------------
# Legacy shims
# ---------------------------------------------------------------------------
_HISTORY: Dict[str, list] = {}
HISTORY_CAP = 100


def _norm_role(r: str) -> str:
    return "user" if (r or "").strip().lower() in ("user", "you") else "assistant"


def _append_history(project_id: str, role: str, content: str) -> None:
    if not project_id or not content:
        return
    _HISTORY.setdefault(project_id, []).append(
        {"role": _norm_role(role), "content": content.strip()}
    )
    if len(_HISTORY[project_id]) > HISTORY_CAP:
        _HISTORY[project_id] = _HISTORY[project_id][-HISTORY_CAP:]


def _get_history(project_id: str, max_items: int = 20) -> list:
    return list(_HISTORY.get(project_id, []))[-max_items:]


def clear_history(project_id: str) -> None:
    _HISTORY.pop(project_id, None)


# ---------------------------------------------------------------------------
# Token substitution
# ---------------------------------------------------------------------------
class TokenSubstitution:
    THRESHOLD = 500

    def __init__(self):
        self._vault:   Dict[str, str] = {}
        self._reverse: Dict[str, str] = {}
        self._n = 0

    def _mk(self) -> str:
        self._n += 1
        return f"__BLOB_{self._n:04d}__"

    @staticmethod
    def _is_b64(s: str) -> bool:
        if len(s) < 100:
            return False
        sample = s[:200].strip()
        return (
            sum(1 for c in sample if c.isalnum() or c in "+/=") / len(sample)
        ) > 0.9 and "\n" not in sample[:100]

    def compress_tree(self, tree: Dict[str, str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for path, content in tree.items():
            if content and len(content) > self.THRESHOLD:
                if (
                    path.endswith(".b64")
                    or self._is_b64(content)
                    or (path.endswith(".json") and len(content) > 5000)
                    or (path.endswith(".svg")  and len(content) > 3000)
                ):
                    h = hashlib.md5(content[:200].encode()).hexdigest()
                    if h in self._reverse:
                        out[path] = self._reverse[h]
                    else:
                        pid = self._mk()
                        self._vault[pid]   = content
                        self._reverse[h]   = pid
                        out[path]          = pid
                    continue
            out[path] = content
        return out

    def expand(self, text: str) -> str:
        for ph, original in self._vault.items():
            if ph in text:
                text = text.replace(ph, original)
        return text


# ═══════════════════════════════════════════════════════════════════════════
# Tool definitions — v18 expanded toolset
# ═══════════════════════════════════════════════════════════════════════════

AGENT_TOOL_DEFS = [
    # ─── WRITE TOOLS (batch-limited) ─────────────────────────────────────
    {
        "name": "write_file",
        "category": "write",
        "description": (
            "Write the FULL content of a file to /home/user/app, overwriting if "
            "it exists. Use this for new files or when rewriting an entire file. "
            "For small edits to an existing file, prefer edit_file (much cheaper). "
            f"WRITE BATCH LIMIT: at most {WRITE_BATCH_LIMIT} write_file/edit_file "
            f"calls combined per turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from /home/user/app, e.g. src/components/Navbar.tsx",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content. No line numbers, no diff markers — raw file bytes.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional one-line note about what this file does.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "category": "write",
        "description": (
            "Surgically edit an existing file by replacing one unique snippet with "
            "another. The `old_str` must appear EXACTLY ONCE in the file — copy it "
            "verbatim (including whitespace) from a previous read_files output. "
            "Much cheaper than write_file for small changes (a single import, a "
            "prop tweak, a className fix). Counts against the WRITE BATCH LIMIT."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from /home/user/app.",
                },
                "old_str": {
                    "type": "string",
                    "description": "The exact text to replace. Must be unique in the file.",
                },
                "new_str": {
                    "type": "string",
                    "description": "The replacement text. May be empty to delete.",
                },
            },
            "required": ["path", "old_str", "new_str"],
        },
    },

    # ─── EXPLORATION TOOLS (unlimited per turn) ──────────────────────────
    {
        "name": "read_files",
        "category": "read",
        "description": (
            "Read up to 10 files in parallel. Use this AGGRESSIVELY on exploration "
            "turns — reading 8 files in one tool call is free and fast. Returns each "
            "file's content prefixed with its path. Skip files known to be huge "
            "(package-lock.json, *.b64). Counts against TOTAL batch limit only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "string",
                    "description": (
                        "Comma- or newline-separated list of relative paths from "
                        "/home/user/app. Example: src/App.tsx, server.js, src/index.css"
                    ),
                },
            },
            "required": ["paths"],
        },
    },
    {
        "name": "list_dir",
        "category": "read",
        "description": (
            "List the contents of a directory (one level deep) with file sizes. "
            "Cheaper than ls + stat in bash. Returns a clean tree summary."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from /home/user/app. Use '.' for the project root.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep_search",
        "category": "read",
        "description": (
            "Search the codebase for a regex pattern using ripgrep — extremely fast. "
            "Returns file:line:match for up to 100 hits. Use this instead of bash "
            "grep when hunting for symbols, imports, or wiring. Perfect for "
            "'where is this component used?' or 'which file imports X?'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern (ripgrep syntax — same as PCRE basics).",
                },
                "path": {
                    "type": "string",
                    "description": "Optional subdirectory to limit search to (e.g. 'src/components'). Default: project root.",
                },
                "file_glob": {
                    "type": "string",
                    "description": "Optional file glob filter (e.g. '*.tsx', '*.{ts,tsx}'). Default: all text files.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "glob_files",
        "category": "read",
        "description": (
            "Find files matching a glob pattern. Returns up to 200 paths. "
            "Use for 'show me all page components' or 'list every route file'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. 'src/**/*.tsx', 'routes/*.js', '**/*.config.*'.",
                },
            },
            "required": ["pattern"],
        },
    },

    # ─── BASH (unlimited per turn but use sparingly) ─────────────────────
    {
        "name": "run_bash",
        "category": "exec",
        "description": (
            "Run a bash command in the sandbox at /home/user/app. Use for npm "
            "install, starting the dev server, curl health checks, running "
            "migrations. NEVER use heredocs to write files — use write_file. "
            "NEVER use bash to read files — use read_files. NEVER use bash grep "
            "— use grep_search. Prefer one &&-chained command over multiple calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional one-line description.",
                },
            },
            "required": ["command"],
        },
    },

    # ─── WEB TOOLS (MCP-style) ───────────────────────────────────────────
    {
        "name": "web_search",
        "category": "web",
        "description": (
            "Search the web for documentation, error messages, or API references. "
            "Use when you hit an unfamiliar error or need current library docs. "
            "Returns top 5 results with titles + snippets."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query. Keep it short and specific (3-8 words).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "category": "web",
        "description": (
            "Fetch the contents of a URL as text (HTML stripped). Use to pull "
            "an exact docs page after web_search points to it. Returns up to "
            "12KB of text content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL with https:// prefix.",
                },
            },
            "required": ["url"],
        },
    },

    # ─── COMPLETION ──────────────────────────────────────────────────────
    {
        "name": "mark_done",
        "category": "done",
        "description": (
            "Signal the task is complete. Only call this AFTER both ports have "
            "been verified to return 200 and the build matches the spec. Provide "
            "a short user-facing summary."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "1-3 sentences of what shipped, written for the user.",
                },
            },
            "required": ["summary"],
        },
    },
    # Add to AGENT_TOOL_DEFS list, after mark_done:
    {
        "name": "connect_supabase",
        "category": "user_action",
        "description": (
            "Request the user to connect their Supabase account. Call this in your thought "
            "when the user wants database functionality but no Supabase is connected. "
            "The frontend will show a connect button and PAUSE the agent loop until "
            "the user completes the OAuth flow. Do NOT call this if has_supabase is already true."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "One sentence explaining why Supabase is needed.",
                },
            },
            "required": ["reason"],
        },
    },
    {
        "name": "set_env_vars",
        "category": "user_action",
        "description": (
            "Request the user to provide environment variables. Call this when you need "
            "API keys or secrets that must come from the user (e.g. Stripe, SendGrid, Twilio). "
            "The frontend will show an input form, PAUSE the agent loop, and resume with "
            "the values injected into .env. List only the variable NAMES — never guess the values."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "vars": {
                    "type": "string",
                    "description": "Comma-separated list of env var names needed, e.g. STRIPE_SECRET_KEY,SENDGRID_API_KEY",
                },
                "reason": {
                    "type": "string",
                    "description": "One sentence explaining what these keys are used for.",
                },
            },
            "required": ["vars", "reason"],
        },
    },
]


def _format_tools_for_prompt() -> str:
    parts = [
        "# Tools",
        "",
        "You have access to the following functions:",
        "",
        "<tools>",
    ]
    for tool in AGENT_TOOL_DEFS:
        params      = tool.get("parameters", {})
        properties  = params.get("properties", {}) or {}
        required    = set(params.get("required", []) or [])

        parts.append("<function>")
        parts.append(f"<name>{tool['name']}</name>")
        parts.append(f"<description>{tool['description']}</description>")
        parts.append("<parameters>")

        for pname, pinfo in properties.items():
            ptype = pinfo.get("type", "string")
            pdesc = pinfo.get("description", "")
            parts.append("<parameter>")
            parts.append(f"<name>{pname}</name>")
            parts.append(f"<type>{ptype}</type>")
            parts.append(f"<description>{pdesc}</description>")
            parts.append(f"<required>{'true' if pname in required else 'false'}</required>")
            parts.append("</parameter>")

        parts.append("</parameters>")
        parts.append("</function>")

    parts.append("</tools>")
    parts.extend([
        "",
        "Call functions with this exact XML format inside a single <tool_call> wrapper:",
        "",
        "<tool_call>",
        "<function=example_function_name>",
        "<parameter=example_parameter_1>",
        "value_1",
        "</parameter>",
        "<parameter=example_parameter_2>",
        "This is the value for the second parameter that can span multiple lines",
        "</parameter>",
        "</function>",
        "</tool_call>",
        "",
        "BATCHING RULES:",
        f"  - WRITE TOOLS (write_file, edit_file): max {WRITE_BATCH_LIMIT} per turn combined.",
        "  - READ TOOLS (read_files, list_dir, grep_search, glob_files): unlimited per turn.",
        "  - USER ACTION TOOLS (connect_supabase, set_env_vars): call ALONE, one per turn.",
        "    After calling one, stop — the agent loop pauses until the user responds.",
        f"  - Total functions per <tool_call> capped at {TOTAL_BATCH_LIMIT}.",
        "  - Brief plain-text reasoning is allowed BEFORE <tool_call>, never after.",
        "  - After </tool_call>, stop — results come back next turn.",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Context management
# ---------------------------------------------------------------------------
_token_estimate_cache: List[Any] = []


def _estimate_tokens(messages: list) -> int:
    global _token_estimate_cache
    msg_count = len(messages)
    if _token_estimate_cache and _token_estimate_cache[0] == msg_count:
        return _token_estimate_cache[1]
    total = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            total += len(c) // CHARS_PER_TOKEN
        elif isinstance(c, list):
            for item in c:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        total += len(item.get("text", "")) // CHARS_PER_TOKEN
                    elif item.get("type") == "image_url":
                        total += 1000
        rd = m.get("reasoning_content", "")
        if rd:
            total += len(str(rd)) // CHARS_PER_TOKEN
    _token_estimate_cache = [msg_count, total]
    return total


def _compress_history(messages: list, max_tokens: int = MAX_CONTEXT_TOKENS) -> list:
    if _estimate_tokens(messages) <= max_tokens:
        return messages

    sys_msg    = messages[0] if messages and messages[0].get("role") == "system" else None
    first_user = None
    for m in messages[1:]:
        if m.get("role") == "user":
            first_user = m
            break

    keep_full    = 10
    recent       = messages[-keep_full:]
    middle_start = 2 if first_user else 1
    middle_end   = len(messages) - keep_full
    compressed   = []

    for m in messages[middle_start:middle_end]:
        role    = m.get("role", "")
        content = str(m.get("content", ""))

        if role == "assistant":
            write_paths = re.findall(
                r"<function=write_file>[\s\S]*?<parameter=path>\s*([^\s<][^<]*?)\s*</parameter>",
                content,
            )
            edit_paths = re.findall(
                r"<function=edit_file>[\s\S]*?<parameter=path>\s*([^\s<][^<]*?)\s*</parameter>",
                content,
            )
            cmds = re.findall(
                r"<function=run_bash>[\s\S]*?<parameter=command>\s*([^<]+?)\s*</parameter>",
                content,
            )
            reads = re.findall(
                r"<function=read_files>[\s\S]*?<parameter=paths>\s*([^<]+?)\s*</parameter>",
                content,
            )
            greps = re.findall(
                r"<function=grep_search>[\s\S]*?<parameter=pattern>\s*([^<]+?)\s*</parameter>",
                content,
            )
            summary_parts = []
            if write_paths:
                summary_parts.append(f"wrote: {', '.join(p.strip() for p in write_paths[:3])}")
            if edit_paths:
                summary_parts.append(f"edited: {', '.join(p.strip() for p in edit_paths[:3])}")
            if reads:
                summary_parts.append(f"read: {reads[0].strip()[:60]}")
            if greps:
                summary_parts.append(f"grep: {greps[0].strip()[:40]}")
            if cmds:
                first_cmd = cmds[0].strip().split("\n")[0][:80]
                summary_parts.append(f"ran: {first_cmd}")
            if "<function=mark_done>" in content:
                summary_parts.append("DONE")
            if summary_parts:
                compressed.append({"role": "assistant", "content": "[" + " | ".join(summary_parts) + "]"})
            else:
                compressed.append({"role": "assistant", "content": content[:120]})

        elif role == "user":
            if content.startswith("OBSERVATION:") or content.startswith("[tool_result]"):
                first_line = content.split("\n", 2)[1] if "\n" in content else content
                compressed.append({"role": "user", "content": f"OBSERVATION: {first_line[:120]}..."})
            else:
                compressed.append({"role": role, "content": content[:200]})
        else:
            compressed.append({"role": role, "content": content[:200]})

    result = []
    if sys_msg:    result.append(sys_msg)
    if first_user and first_user not in recent:
        result.append(first_user)
    result.extend(compressed)
    result.extend(recent)

    if _estimate_tokens(result) > max_tokens:
        result = [sys_msg] if sys_msg else []
        if first_user:
            result.append(first_user)
        result.extend(recent)

    return result


# ---------------------------------------------------------------------------
# Observation noise filter
# ---------------------------------------------------------------------------
_VITE_NOISE_RE = re.compile(
    r"""(
        ^\s*(WARNING|warn)\b(?!.*\berror\b)     |
        node_modules/.*warning                  |
        Browserslist:.*outdated                 |
        @vitejs/plugin-react.*preamble          |
        vite\s+v\d                              |
        VITE\s+v\d                              |
        ^\s*→\s+Local:                          |
        ^\s*➜\s+Local:                          |
        ^\s*ready\s+in\s+\d+ms                  |
        ^\s*hmr\s                               |
        \[vite\]\s+(page reload|hot updated|connected)  |
        ^\s*(\d+\s+)?modules?\s+transformed     |
        eslint.*warning                         |
        ^\s*\d+\s+warning
    )""",
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)

_FATAL_SIGNALS = frozenset([
    "error ts", "syntaxerror", "cannot find module", "could not be resolved",
    "failed to compile", "exit code: 1", "exit code: -1", "enoent",
    "typeerror", "referenceerror", "error:", "[error]",
])


def _filter_observation(raw: str) -> str:
    if not raw:
        return raw
    lines       = raw.splitlines()
    kept        = []
    noise_count = 0
    for line in lines:
        low = line.lower()
        if any(sig in low for sig in _FATAL_SIGNALS):
            kept.append(line)
            continue
        if _VITE_NOISE_RE.search(line):
            noise_count += 1
            continue
        kept.append(line)
    result = "\n".join(kept).strip()
    if noise_count > 5 and len(result) < 100:
        result = (result + f"\n[{noise_count} Vite/lint warnings suppressed — no errors]").strip()
    return result or raw


# ═══════════════════════════════════════════════════════════════════════════

# SYSTEM PROMPT  v18 — multi-tool parallelism

# ═══════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT_BODY = r"""You are Gorilla, a senior full-stack engineer. You share one workspace with the user: an Ubuntu sandbox running React + Vite on port 8080 and Express on port 3000. Your job is to build real, working SaaS apps — not mockups — and stay with the work until it's genuinely done.

# How you work — the rhythm

You alternate between two modes:

**EXPLORE mode** — when you don't yet know enough to write good code. Fire many read tools IN PARALLEL inside a single <tool_call>:

* `read_files` (up to 10 files at once)
* `list_dir` (cheap directory tree)
* `grep_search` (ripgrep — find symbols, imports, wiring)
* `glob_files` (find by pattern)

There is NO batch limit on read/search tools. A good first turn fires 4-8 of them in one wrapper.

**IMPLEMENT mode** — when you know what to build. Use `write_file` for new files, `edit_file` for surgical changes. HARD LIMIT: at most 3 write_file/edit_file calls per turn combined. This guardrail keeps each turn observable — between turns you see Vite's hot-reload output.

# Pick the right write tool

* **`write_file`** — new files, or when more than ~30% of an existing file is changing. Provides the FULL content.
* **`edit_file`** — small surgical changes (add an import, change one prop, fix a className, mount a route). Cheaper, faster, less context burned. The `old_str` must appear exactly once and match verbatim — copy it from a previous read_files output.

Prefer `edit_file` whenever you're touching one piece of an existing file. It saves tokens and reduces the chance of introducing regressions.

# Order of operations for a greenfield build

Turn 1 — EXPLORE (one tool_call, many parallel reads):
<tool_call>
<function=read_files>
<parameter=paths>
src/App.tsx, server.js, src/index.css, package.json, vite.config.ts


<function=list_dir>
<parameter=path>
.


<function=glob_files>
<parameter=pattern>
src//*.tsx


</tool_call>

Turn 2 — Foundation: `write_file: src/index.css` (the full design system).

Turn 3 — Shared chrome: `write_file: Navbar.tsx + Footer.tsx` (2 in batch).

Turn 4-N — Pages and routes in batches of 2-3 writes per turn.

Turn N+1 — Wire it up: `edit_file: src/App.tsx` to add the routes (surgical), `edit_file: server.js` to mount the API.

Final turn — `run_bash` to start dev + verify both ports 200, then `mark_done`.

# Environment

* Ubuntu 22 / Node 20 / Python 3.11 — working directory `/home/user/app`
* Dev server: **already running in the background** (pre-warmed on sandbox boot). Vite on :8080, Express on :3000. Don't run `pkill -f vite` or `pkill -f 'npm run dev'` followed by a restart — that's the freeze pattern. Just `curl` the ports to verify, or `tail /tmp/dev.log` to debug. If — and only if — both ports return non-200 after a fix, restart with: `cd /home/user/app && npm run dev > /tmp/dev.log 2>&1 </dev/null & disown`.
* Vite has HMR — frontend file writes hot-reload; no restart needed for `src/` changes.
* Pre-installed: react, react-dom, react-router-dom, vite, @vitejs/plugin-react, typescript, tailwindcss, postcss, autoprefixer, clsx, tailwind-merge, class-variance-authority, @radix-ui/*, lucide-react, express, cors, body-parser, dotenv, concurrently
* Source layout: `src/` (React), `src/components/ui/` (shadcn), `routes/` (Express), `public/generated/` (AI images)
* Import alias `@/` → `src/`. Backend files use relative imports with `.js` extensions.
* Files you must not modify: `vite.config.ts`, `.env`, `src/utils/auth.ts`

# Auth

```tsx
import { login, logout, onAuthStateChanged } from '@/utils/auth';
useEffect(() => onAuthStateChanged(setUser), []);
<button onClick={() => login()}>Sign in</button>

```

# AI proxy

Base URL: `{GORILLA_PROXY}` — pass `$GORILLA_API_KEY` as the Authorization Bearer token.

* LLM chat:     `POST {GORILLA_PROXY}/api/v1/chat/completions`  (omit the model field)
* Image gen:    `POST {GORILLA_PROXY}/api/v1/images/generations` → save base64 to `public/generated/` also use in the users app for image gen, to learn the format, use it yourself with curl first.
* STT:          `POST {GORILLA_PROXY}/api/v1/audio/transcriptions`
* BG removal:   `POST {GORILLA_PROXY}/api/v1/images/remove-background`

# Engineering judgment

You bring a senior engineer's judgment to each decision. When the spec is open, you choose conservatively and in sympathy with what's already in the codebase. You prefer established patterns and keep edits tightly scoped.

# Frontend quality

Interfaces feel rich and domain-appropriate. A SaaS dashboard is quiet and work-focused; a game can be expressive. Use lucide-react icons, keep border-radius ≤ 8px, build tooltips for icon-only buttons, no decorative gradient orbs, ensure text fits all viewports.

# When something goes wrong

First, gather context in parallel — don't do five sequential reads:
<tool_call>
<function=grep_search>
<parameter=pattern>
useState

<parameter=path>
src/components/Broken.tsx


<function=read_files>
<parameter=paths>
src/components/Broken.tsx, src/App.tsx


<function=run_bash>
<parameter=command>
tail -60 /tmp/dev.log


</tool_call>

Then make the smallest fix that addresses the root cause — usually a single `edit_file`. Don't refactor on the way to a fix. If a component is missing an import, `edit_file` to add the import — don't rewrite the file.

If bash commands repeatedly produce no output or empty results for 3 or more turns, stop immediately and tell the user clearly: "The sandbox environment appears to be degraded — bash isn't responding. Your code is safe. Please reprompt" Do not keep retrying. Do not write placeholder files to force a restart. Stop, explain, and let the user recover.

# Verification before done

The sandbox pre-warms the dev server on boot and runs an automated health check on touched-dev turns — both ports are usually already responding before you act. Verify manually:

```bash
curl -so /dev/null -w '%{http_code}' http://localhost:8080
curl -so /dev/null -w '%{http_code}' http://localhost:3000

```

Both must return 200 before `mark_done`. If you see `tail` showing errors, fix them with `edit_file` and try again.

# Looking things up

If you hit an unfamiliar error or need to confirm a library's current API, use `web_search` then `web_fetch` on the best result. This is much faster than guessing or trial-and-error rebuilds.

# Autonomy

Stay with the work until it's handled end to end. Work through blockers rather than stopping and asking — unless something is genuinely impossible without user input.

# User action tools — when to call them

You have two tools that PAUSE the agent loop and request input from the user.
Call them ALONE in their own <tool_call> — never batch with other tools.

**connect_supabase** — call this when ALL of the following are true:

* The user's request requires a database (storing data, user records, etc.)
* `has_supabase` is False in the context you received
* You have NOT already called this tool in this conversation

**CRITICAL RULE FOR SUPABASE:** If `has_supabase` is False and the project requires a database, your VERY FIRST ACTION must be to call `connect_supabase`. Do NOT write code. Do NOT run bash. Do NOT explore files. You must STOP and call `connect_supabase` immediately.

**set_env_vars** — call this when ALL of the following are true:

* The feature requires a third-party API key you cannot infer or generate
(e.g. Stripe, Twilio, SendGrid, OpenAI, Mapbox)
* The key is not already present in .env (check with read_files first)
* You cannot build a meaningful stub or placeholder without the real key
List only the variable NAMES — never ask for values in chat.
Do NOT call this for GORILLA_API_KEY, VITE_SUPABASE_URL, VITE_SUPABASE_ANON_KEY,
or any variable Gorilla already manages — those are pre-populated.

After calling either tool, stop completely. Do not write files, do not run bash,
do not call mark_done. The loop resumes automatically once the user responds.

The user wants to save posts — I need a database but has_supabase is False.

<tool_call>
<function=connect_supabase>
<parameter=reason>
Saving and retrieving posts requires a database.


</tool_call>

Third party keys needed:

<tool_call>
<function=set_env_vars>
<parameter=vars>
STRIPE_SECRET_KEY,STRIPE_PUBLISHABLE_KEY

<parameter=reason>
Processing payments requires Stripe API keys from your Stripe dashboard.


</tool_call>

The Tool calls to add .env variables and to connect supabase are enabled, use them.

# Deployment and GitHub — CRITICAL UX RULE

When the user asks about deploying, pushing to GitHub, sharing their app, exporting code, or publishing — ALWAYS direct them to the built-in buttons in the top-right of the editor:

* **GitHub button** (top-right) — connects their GitHub account and pushes code in one click. No PAT, no token pasting, no CLI commands.
* **Deploy button** (top-right, rocket icon) — opens the deployment wizard.

NEVER ask the user to paste a Personal Access Token, a GitHub token, or any credential into the chat. NEVER attempt manual `git remote add`, `git push`, or PAT-based flows from the sandbox. If GitHub is not connected, tell them to click the GitHub button to connect first. That's the entire flow.

If the sandbox can't push to GitHub (no `github_access_token` found), say: "Click the **GitHub** button in the top-right to connect your account, then click it again to push your code."

# Tool call mechanics

The mental model: every turn ends with a single <tool_call>...</tool_call> block. Inside, place one or more <function=name>... sub-blocks. The exact format and limits are documented in the # Tools section below.

Never ever go around in circles and circles just doing nothing and wasting tool calls, unless you are working on something, and making progress, mark it done.

Unless you are building or iterating on an app, do not start the servers.

Examples of well-formed turns:

**Parallel exploration (one tool_call, many reads):**
I'll get the lay of the land before deciding what to build.

<tool_call>
<function=list_dir>
<parameter=path>
.


<function=read_files>
<parameter=paths>
src/App.tsx, server.js, src/index.css, package.json


<function=glob_files>
<parameter=pattern>
src//*.tsx


</tool_call>

**Surgical edit (one write, one verify):**
The Footer is missing from App.tsx — adding it inside the layout wrapper.

<tool_call>
<function=edit_file>
<parameter=path>
src/App.tsx

<parameter=old_str>



<parameter=new_str>





</tool_call>

**Batched writes (2 new files):**
Shipping Navbar and Footer together — they don't depend on each other.

<tool_call>
<function=write_file>
<parameter=path>
src/components/Navbar.tsx

<parameter=content>
...full file content...


<function=write_file>
<parameter=path>
src/components/Footer.tsx

<parameter=content>
...full file content...


</tool_call>

**Start + verify (single run_bash, automated health check follows):**
<tool_call>
<function=run_bash>
<parameter=command>
curl -so /dev/null -w 'vite=%{http_code} ' http://localhost:8080 && curl -so /dev/null -w 'api=%{http_code}\n' http://localhost:3000


</tool_call>

**Finish:**
<tool_call>
<function=mark_done>
<parameter=summary>
Built the dashboard with Navbar, three pages, and the items API. Both servers healthy.


</tool_call>
"""

# ---------------------------------------------------------------------------

# Conditional addons — Supabase / Debug

# ---------------------------------------------------------------------------

SUPABASE_ADDON = r"""

# Supabase — MANDATORY

Supabase is provisioned and active for this project. The env vars `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `SUPABASE_PROJECT_REF`, and `SUPABASE_MGMT_TOKEN` are already set in `.env`.

**You MUST use Supabase for ALL data persistence. Do NOT use SQLite, lowdb, JSON files, in-memory stores, localStorage, or any other database. There are no exceptions.**

Frontend client (already installed — `@supabase/supabase-js`):

```ts
import { createClient } from '@supabase/supabase-js';
const supabase = createClient(
  import.meta.env.VITE_SUPABASE_URL,
  import.meta.env.VITE_SUPABASE_ANON_KEY
);

```

Run migrations via the management API (use your own project, not the user's existing data):

```bash
cat > /tmp/migration.sql << 'SQL'
CREATE TABLE IF NOT EXISTS items (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid REFERENCES auth.users(id),
  created_at timestamptz DEFAULT now()
);
ALTER TABLE items ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own" ON items USING (auth.uid() = user_id);
SQL
curl -sS -X POST "https://api.supabase.com/v1/projects/$SUPABASE_PROJECT_REF/database/query" \
  -H "Authorization: Bearer $SUPABASE_MGMT_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(cat /tmp/migration.sql | jq -Rs '{query: .}')"

```

Always run migrations before writing frontend code that reads from the DB. Always enable RLS and write policies for every table.
"""

DEBUG_ADDON = r"""

# Debug mode

You are fixing a specific bug. Use this rhythm:

Turn 1 — Gather context in parallel:

* `grep_search` for the symbol or error keyword
* `read_files` for the suspect file plus its callers
* `run_bash` to tail /tmp/dev.log

Turn 2 — Apply the smallest fix as `edit_file` (almost never write_file).

Turn 3 — Verify with curl.

Do NOT refactor. Do NOT add features. The 3-write batch limit still applies — most bugs need only one `edit_file`.
"""

EXPANDER_SUPABASE_ADDON = """

Supabase is provisioned and active. The spec MUST include Supabase for all data persistence — do NOT spec SQLite, JSON files, or any other storage. Design tables, RLS policies, and which data is persisted. Supabase is non-negotiable for this project."""

PLANNER_SUPABASE_ADDON = """

Supabase is provisioned and active. The plan MUST include a migration step (run_bash: curl to Supabase management API) before any frontend DB reads. Do NOT plan for SQLite, JSON files, or any other storage. Every table must have RLS enabled."""


# ═══════════════════════════════════════════════════════════════════════════
#  Prompt Expander
# ═══════════════════════════════════════════════════════════════════════════

def _build_expander_system(gorilla_proxy_url: str) -> str:
    proxy = gorilla_proxy_url or "{GORILLA_PROXY}"
    return f"""You are a product designer for Gorilla Builder — a platform that builds real working SaaS apps.

The developer's sandbox has access to these capabilities — spec features that use them:

The worker/coder agent has these tools:
  - WRITE (limit 3/turn combined): write_file, edit_file
  - READ (unlimited/turn): read_files, list_dir, grep_search, glob_files
  - EXEC: run_bash
  - WEB: web_search, web_fetch
  - USER ACTION: connect_supabase, set_env_vars
  - mark_done

**Auth gateway** (zero-setup login):
```tsx
import {{ login, logout, onAuthStateChanged }} from '@/utils/auth';
<button onClick={{() => login('google')}}>Sign in with Google</button>
```
Use auth whenever the app saves per-user data or has a dashboard the user returns to.

**AI proxy** (base URL: `{proxy}`, auth via `$GORILLA_API_KEY`):
- LLM chat:       `POST {proxy}/api/v1/chat/completions`  — omit model field
- Image gen:      `POST {proxy}/api/v1/images/generations` — returns base64, save to `public/generated/`
- Speech-to-text: `POST {proxy}/api/v1/audio/transcriptions`
- BG removal:     `POST {proxy}/api/v1/images/remove-background`

Use these for features that genuinely benefit from AI — not as decorations.

**Express backend**: full API server at port 3000, routes in `routes/`, can store data, proxy AI calls, handle logic.

Take the user's short idea and expand it into a concrete product spec (200–350 words).

A good spec describes a FUNCTIONAL APP — what does the user actually do? What gets created, saved, generated, or shared? Which AI capability makes the core feature work?

Include:
- App name (creative, memorable)
- Color scheme (specific hex codes, dark mode preferred)
- Typography (a distinctive Google Font — not Inter, not system-ui)
- 3+ pages: what the user sees and does on each
- Backend: API routes, what data is stored
- AI integration: which proxy endpoint, what it does, how the result is shown
- Auth: which provider and why (only if the app genuinely needs it)

For minor tasks or bug fixes: restate the task in 1–2 sentences. No expansion needed.

If an image is attached, treat it as a UI mockup — extract layout, palette, and flows.

Output only the expanded prompt. NO CODE. NO SPEC

"""


async def expand_prompt(
    short_prompt: str,
    has_supabase: bool = False,
    image_b64: Optional[str] = None,
    gorilla_proxy_url: str = "",
) -> str:
    if len(short_prompt) > 300:
        return short_prompt

    system = _build_expander_system(gorilla_proxy_url)
    if has_supabase:
        system += "\n" + EXPANDER_SUPABASE_ADDON

    user_content: Any
    if image_b64:
        img_url = (
            image_b64 if image_b64.startswith("data:")
            else f"data:image/jpeg;base64,{image_b64}"
        )
        user_content = [
            {"type": "text",      "text": short_prompt},
            {"type": "image_url", "image_url": {"url": img_url}},
        ]
    else:
        user_content = short_prompt

    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user_content},
    ]
    try:
        model = VISION_MODEL if image_b64 else PLANNER_MODEL
        raw, _ = await _call_llm(messages, model=model, temperature=0.8)
        expanded = raw.strip()
        if len(expanded) > len(short_prompt) * 2:
            log_agent("agent", f"Expanded prompt: {expanded[:150]}...")
            return expanded
        return short_prompt
    except Exception as e:
        log_agent("agent", f"Expander failed ({e}), using original prompt")
        return short_prompt


# ═══════════════════════════════════════════════════════════════════════════
#  Planner
# ═══════════════════════════════════════════════════════════════════════════

def _build_planner_system(gorilla_proxy_url: str) -> str:
    return f"""You are a project planner for Gorilla Builder — React + Vite + Express full-stack apps.

The agent has these tools:
  - WRITE (limit 3/turn combined): write_file, edit_file
  - READ (unlimited/turn): read_files, list_dir, grep_search, glob_files
  - EXEC: run_bash
  - WEB: web_search, web_fetch
  - mark_done

Produce a markdown checklist where each item = ONE turn of the agent.

**Hard rule: at most 3 write_file/edit_file calls per checklist item.** If you'd want 6 components in one step, split into 2 steps (3+3).

**Hard rule: exploration is one turn.** The first step batches all reads in parallel (read_files, list_dir, glob_files). Do NOT plan multiple explore steps.

**Step order:**
1. Explore — parallel reads in one turn: read_files src/App.tsx + server.js + src/index.css + package.json, list_dir ., glob_files src/**/*.tsx
2. write_file: src/index.css (full design system — solo)
3. write_file (batch of 2-3): Navbar.tsx + Footer.tsx [+ a primitive]
4. write_file (batch of 2-3): page A + page B [+ page C]
5. write_file (batch of 2-3): backend route files
6. edit_file (batch of 2): wire src/App.tsx + server.js (surgical edits where possible)
7. run_bash: npm install (only if needed)
8. run_bash: curl :8080 and :3000 to verify both return 200 (dev server is already running — DO NOT pkill+restart)

**Format:**
```
# Task: <short title>

- [ ] EXPLORE: read_files App.tsx/server.js/index.css + list_dir + glob src/**/*.tsx
- [ ] write_file: src/index.css — dark design system, CSS vars, Google Font
- [ ] write_file (batch): Navbar.tsx + Footer.tsx + maybe ThemeProvider
- [ ] write_file (batch): Landing.tsx + Dashboard.tsx + About.tsx
- [ ] write_file (batch): routes/api.js + routes/items.js
- [ ] edit_file (batch): src/App.tsx wire routes + server.js mount api
- [ ] run_bash: curl :8080 and :3000 → both 200 (server pre-warmed; no pkill+restart)
```

Rules: 5–10 items. Every batch names the files. First step is EXPLORE (parallel reads). Last step verifies. For debug/minor tasks: 2-3 items, prefer edit_file. Output only the checklist."""


async def generate_plan(
    expanded_prompt: str,
    file_tree_summary: str,
    has_supabase: bool = False,
    image_b64: Optional[str] = None,
    gorilla_proxy_url: str = "",
) -> Optional[str]:
    system = _build_planner_system(gorilla_proxy_url)
    if has_supabase:
        system += "\n" + PLANNER_SUPABASE_ADDON

    user_text = f"Existing files:\n{file_tree_summary}\n\nSpec:\n{expanded_prompt}"

    user_content: Any
    if image_b64:
        img_url = (
            image_b64 if image_b64.startswith("data:")
            else f"data:image/jpeg;base64,{image_b64}"
        )
        user_content = [
            {"type": "text",      "text": user_text},
            {"type": "image_url", "image_url": {"url": img_url}},
        ]
    else:
        user_content = user_text

    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user_content},
    ]
    try:
        model = VISION_MODEL if image_b64 else PLANNER_MODEL
        raw, _ = await _call_llm(messages, model=model, temperature=0.4)
        plan = raw.strip()
        if "- [ ]" in plan:
            log_agent("agent", f"Plan generated: {plan[:200]}...")
            return plan
        return None
    except Exception as e:
        log_agent("agent", f"Planner failed ({e}), agent will self-plan")
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  Reviewer
# ═══════════════════════════════════════════════════════════════════════════

REVIEWER_SYSTEM = """You are a code reviewer. A developer just finished building a web app. Review the file listing and recent build output for obvious mistakes only.

Check for: components created but not imported anywhere, missing npm installs, Express routes not mounted in server.js, TypeScript errors in logs.

Respond with ONLY:
- "LGTM" if everything looks correct
- A numbered list of up to 3 specific, actionable fixes (no preamble)"""


async def review_output(file_tree_summary: str, last_output: str) -> Optional[str]:
    messages = [
        {"role": "system", "content": REVIEWER_SYSTEM},
        {
            "role": "user",
            "content": f"Files:\n{file_tree_summary}\n\nRecent output:\n{last_output[:3000]}",
        },
    ]
    try:
        raw, _ = await _call_llm(messages, model=SMART_MODEL, temperature=0.2)
        review = raw.strip()
        if "LGTM" in review.upper():
            log_agent("agent", "Reviewer: LGTM")
            return None
        log_agent("agent", f"Reviewer found issues: {review[:200]}")
        return review
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  XML repair
# ═══════════════════════════════════════════════════════════════════════════

def _repair_qwen3_xml(text: str) -> str:
    if text.count("<tool_call>") > text.count("</tool_call>"):
        text = text + "\n</tool_call>"

    open_funcs  = len(re.findall(r"<function=[^>]+>", text))
    close_funcs = text.count("</function>")
    if open_funcs > close_funcs:
        diff = open_funcs - close_funcs
        if "</tool_call>" in text:
            text = text.replace("</tool_call>", "</function>" * diff + "</tool_call>", 1)
        else:
            text = text + ("</function>" * diff)

    open_params  = len(re.findall(r"<parameter=[^>]+>", text))
    close_params = text.count("</parameter>")
    if open_params > close_params:
        diff = open_params - close_params
        if "</function>" in text:
            text = text.replace("</function>", "</parameter>" * diff + "</function>", 1)
        else:
            text = text + ("</parameter>" * diff)

    return text


# ═══════════════════════════════════════════════════════════════════════════
#  XML tool-call parser
# ═══════════════════════════════════════════════════════════════════════════

_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>([\s\S]*?)</tool_call>")
_FUNCTION_RE        = re.compile(r"<function=([^>\s]+)>([\s\S]*?)</function>")
_PARAMETER_RE       = re.compile(r"<parameter=([^>\s]+)>([\s\S]*?)</parameter>")

_TOOL_CATEGORY = {t["name"]: t.get("category", "exec") for t in AGENT_TOOL_DEFS}


def _strip_param_value(raw: str) -> str:
    if not raw:
        return raw
    if raw.startswith("\r\n"):
        raw = raw[2:]
    elif raw.startswith("\n"):
        raw = raw[1:]
    if raw.endswith("\r\n"):
        raw = raw[:-2]
    elif raw.endswith("\n"):
        raw = raw[:-1]
    return raw


def _parse_xml_functions(block_inner: str) -> List[Dict[str, Any]]:
    functions: List[Dict[str, Any]] = []
    for m in _FUNCTION_RE.finditer(block_inner):
        name   = m.group(1).strip()
        body   = m.group(2)
        params: Dict[str, str] = {}
        for pm in _PARAMETER_RE.finditer(body):
            pname = pm.group(1).strip()
            pval  = _strip_param_value(pm.group(2))
            params[pname] = pval
        functions.append({"name": name, "params": params})
    return functions


def _parse_response(raw_text: str) -> Dict[str, Any]:
    # In _parse_response, update the result dict initialization:
    result = {
        "thought":              "",
        "write_files":          [],
        "edit_files":           [],
        "read_calls":           [],
        "bash":                 "",
        "done":                 False,
        "message":              "",
        "extra_writes_dropped": 0,
        "user_action":          None,   # ← ADD THIS
    }

    if not raw_text:
        return result

    repaired = _repair_qwen3_xml(raw_text)
    blocks   = _TOOL_CALL_BLOCK_RE.findall(repaired)

    if blocks:
        first_pos = repaired.find("<tool_call>")
        result["thought"] = repaired[:first_pos].strip() if first_pos > 0 else ""

        bash_parts:    List[str] = []
        all_functions: List[Dict[str, Any]] = []
        for block_inner in blocks:
            all_functions.extend(_parse_xml_functions(block_inner))

        if len(all_functions) > TOTAL_BATCH_LIMIT:
            log_agent("agent", f"⚠ Capping {len(all_functions)} functions at {TOTAL_BATCH_LIMIT}")
            all_functions = all_functions[:TOTAL_BATCH_LIMIT]

        write_count = 0
        for fn in all_functions:
            n = fn["name"]
            p = fn["params"]

            if n == "write_file":
                if write_count < WRITE_BATCH_LIMIT:
                    result["write_files"].append({
                        "path":    p.get("path", "").strip(),
                        "content": p.get("content", ""),
                        "reason":  p.get("reason", "").strip(),
                    })
                    write_count += 1
                else:
                    result["extra_writes_dropped"] += 1

            elif n == "edit_file":
                if write_count < WRITE_BATCH_LIMIT:
                    result["edit_files"].append({
                        "path":    p.get("path", "").strip(),
                        "old_str": p.get("old_str", ""),
                        "new_str": p.get("new_str", ""),
                    })
                    write_count += 1
                else:
                    result["extra_writes_dropped"] += 1

            elif n == "run_bash":
                cmd = p.get("command", "").strip()
                if cmd:
                    bash_parts.append(cmd)

            elif n == "mark_done":
                result["done"]    = True
                result["message"] = p.get("summary", "Done.").strip()

            elif n == "connect_supabase":
                result["user_action"] = {
                    "type": "connect_supabase",
                    "reason": p.get("reason", "Database connection required.").strip(),
                }

            elif n == "set_env_vars":
                raw_vars = p.get("vars", "")
                var_names = [v.strip() for v in raw_vars.replace("\n", ",").split(",") if v.strip()]
                result["user_action"] = {
                    "type": "set_env_vars",
                    "vars": var_names,
                    "reason": p.get("reason", "Environment variables required.").strip(),
                }
            elif n in ("read_files", "list_dir", "grep_search", "glob_files",
                       "web_search", "web_fetch"):
                result["read_calls"].append({"tool": n, "params": p})

            else:
                log_agent("agent", f"Unknown tool: {n}")

        result["bash"] = "\n\n".join(bash_parts)

        if not result["message"]:
            if result["thought"]:
                result["message"] = result["thought"].split("\n")[0][:30000]
            else:
                paths = (
                    [w["path"] for w in result["write_files"]] +
                    [e["path"] for e in result["edit_files"]]
                )
                if paths:
                    result["message"] = f"Working on {', '.join(paths)}"
                elif result["read_calls"]:
                    result["message"] = "Exploring the codebase..."
                elif result["bash"]:
                    result["message"] = ""

        return result

    if "GORILLA_DONE" in raw_text:
        result["done"] = True
        parts   = raw_text.split("GORILLA_DONE", 1)
        body    = parts[0]
        summary = parts[1].strip() if len(parts) > 1 else ""
        bash_blocks = re.findall(r"```(?:bash|sh|shell)?\n([\s\S]*?)```", body)
        result["bash"]    = "\n\n".join(b.strip() for b in bash_blocks if b.strip())
        result["thought"] = body[:body.find("```")].strip() if "```" in body else body.strip()
        result["message"] = summary or result["thought"].split("\n")[0][:30000] or "Done."
        return result

    bash_blocks = re.findall(r"```(?:bash|sh|shell)?\n([\s\S]*?)```", raw_text)
    if bash_blocks:
        result["bash"]    = "\n\n".join(b.strip() for b in bash_blocks if b.strip())
        first_pos         = raw_text.find("```")
        result["thought"] = raw_text[:first_pos].strip() if first_pos > 0 else ""
    else:
        result["thought"] = raw_text.strip()

    if result["thought"]:
        sentences = re.split(r"(?<=[.!?])\s+", result["thought"])
        result["message"] = " ".join(sentences[:3])[:30000]

    return result


# ═══════════════════════════════════════════════════════════════════════════
#  Shell safety
# ═══════════════════════════════════════════════════════════════════════════

_DANGEROUS = [
    r"\brm\s+-rf\s+/($|\s)",
    r"\bsudo\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r">\s*/dev/(sda|nvme|hda)",
    r"\bmkfs\b",
    r":\(\)\s*{\s*:\|:",
    r"\bdd\s+if=.*\s+of=/dev/",
]


def _is_safe(cmd: str) -> bool:
    return not any(re.search(p, cmd, re.IGNORECASE) for p in _DANGEROUS)


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

    # ─── BILLING FIX: account for cached input tokens at discounted rate ───
    # Some providers return `prompt_tokens_details.cached_tokens` indicating
    # how many prompt tokens hit the prefix cache. Cached tokens are typically
    # billed at 10% of full rate. If the field is absent, behaviour is
    # unchanged.
    prompt_details = u.get("prompt_tokens_details") or {}
    cached_tokens  = int(prompt_details.get("cached_tokens", 0) or 0)
    fresh_prompt_tokens = max(0, prompt_tokens - cached_tokens)

    prompt_price, completion_price = await _fetch_model_price(model)

    # Cached prompt tokens billed at 10% of fresh rate (industry standard)
    weight_usd = (
        fresh_prompt_tokens * prompt_price
        + cached_tokens     * prompt_price * 0.1
        + completion_tokens * completion_price
    )
    weight = int(round(weight_usd * 1_000_000))

    cache_note = f" cached={cached_tokens}" if cached_tokens else ""
    log_agent(
        "agent",
        f"usage p={prompt_tokens}{cache_note} c={completion_tokens} "
        f"cost={weight}µ$ (${weight / 1_000_000:.6f})",
    )

    return content, weight


# ═══════════════════════════════════════════════════════════════════════════
#  Agent Skills helpers
# ═══════════════════════════════════════════════════════════════════════════

def _build_skills_block(agent_skills: Optional[Dict[str, Any]]) -> str:
    if not agent_skills:
        return ""
    enabled = [k for k, v in agent_skills.items() if v]
    if not enabled:
        return ""
    lines = "\n".join(f"- {skill}" for skill in enabled)
    return f"\n\n# User preferences\n{lines}"


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


# ═══════════════════════════════════════════════════════════════════════════
#  LineageAgent v18.1
# ═══════════════════════════════════════════════════════════════════════════

class LineageAgent:
    def __init__(self, project_id: str):
        self.project_id         = project_id
        self.total_tokens       = 0
        self.token_sub          = TokenSubstitution()
        self.messages:          List[Dict[str, Any]] = []
        self._system_prompt_set = False
        self._plan_injected     = False
        self._prompt_expanded   = False
        self._gorilla_proxy_url: str  = ""
        self._has_supabase:      bool = False
        # Once a request contains the DEEP_THINK_TRIGGER phrase, every turn
        # of this agent instance routes to DEEP_THINK_MODEL. Sticky so
        # multi-turn tool loops don't drop back to the base model.
        self._deep_think:        bool = False

    def _ensure_system_prompt(
        self,
        gorilla_proxy_url: str,
        has_supabase: bool,
        is_debug: bool,
        agent_skills: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._system_prompt_set and has_supabase == self._has_supabase:
            return

        prompt = SYSTEM_PROMPT_BODY.replace(
            "{GORILLA_PROXY}",
            gorilla_proxy_url or "https://your-proxy.ngrok-free.dev",
        )

        # explicitly declare the supabase state for the agent's logic rules
        if has_supabase:
            prompt += "\n" + SUPABASE_ADDON
            prompt += "\n\n**CONTEXT STATE:** `has_supabase` is currently True."
        else:
            prompt += "\n\n**CONTEXT STATE:** `has_supabase` is currently False. (No database is connected)."

        if is_debug:
            prompt += "\n" + DEBUG_ADDON

        prompt += "\n\n" + _format_tools_for_prompt()

        skills_block = _build_skills_block(agent_skills)
        if skills_block:
            prompt += skills_block
            log_agent(
                "agent",
                f"Skills injected: {[k for k, v in (agent_skills or {}).items() if v]}",
                self.project_id,
            )

        if self._system_prompt_set:
            log_agent("agent", f"System prompt rebuilt — has_supabase changed to {has_supabase}", self.project_id)
            if self.messages and self.messages[0].get("role") == "system":
                self.messages[0] = {"role": "system", "content": prompt}
            else:
                self.messages.insert(0, {"role": "system", "content": prompt})
        else:
            self.messages = [{"role": "system", "content": prompt}]

        self._has_supabase      = has_supabase
        self._system_prompt_set = True

    def _extract_prompt_image(self, file_tree: Dict[str, str]) -> Optional[str]:
        raw = file_tree.get(".gorilla/prompt_image.b64") or file_tree.get("prompt_image.b64")
        if not raw:
            return None
        stripped = raw.strip()
        return stripped if stripped.startswith("data:") else f"data:image/jpeg;base64,{stripped}"

    def _append_observation(
        self,
        write_paths:      List[str],
        edit_paths:       List[str],
        read_observation: str,
        bash_observation: str,
    ) -> None:
        parts: List[str] = []
        if write_paths:
            parts.append("Files written: " + ", ".join(write_paths))
        if edit_paths:
            parts.append("Files edited: " + ", ".join(edit_paths))
        if read_observation:
            parts.append(read_observation[:10000])
        if bash_observation:
            filtered = _filter_observation(bash_observation)[:8000]
            parts.append("Bash output:\n" + filtered)
        if not parts:
            parts.append("(no output)")

        self.messages.append({
            "role":    "user",
            "content": "OBSERVATION:\n" + "\n\n".join(parts),
        })

    async def run(
        self,
        user_request: str,
        file_tree: Dict[str, str],
        chat_history: Optional[list] = None,
        gorilla_proxy_url: str = "",
        has_supabase: bool = False,
        is_debug: bool = False,
        error_context: str = "",
        image_b64: Optional[str] = None,
        previous_command_output: Optional[str] = None,
        agent_skills: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:

        if gorilla_proxy_url:
            self._gorilla_proxy_url = gorilla_proxy_url

        self._ensure_system_prompt(gorilla_proxy_url, has_supabase, is_debug, agent_skills)

        compressed  = self.token_sub.compress_tree(file_tree)
        clean_paths = sorted(p for p in compressed if not p.endswith(".b64"))
        tree_str    = "\n".join(f"  {p}" for p in clean_paths)

        pkg_snippet = ""
        if "package.json" in compressed:
            pkg = compressed["package.json"]
            if len(pkg) < 3000:
                pkg_snippet = f"\n\npackage.json:\n{pkg}"

        prompt_image_b64: Optional[str] = None
        if not previous_command_output:
            prompt_image_b64 = self._extract_prompt_image(file_tree)
            if prompt_image_b64:
                log_agent("agent", "prompt_image.b64 found", self.project_id)

        if previous_command_output:
            filtered = _filter_observation(previous_command_output)
            self.messages.append({"role": "user", "content": f"OBSERVATION:\n{filtered[:12000]}"})
        else:
            effective_request = user_request
            plan_text         = ""

            if not is_debug and user_request:
                if not self._prompt_expanded:
                    effective_request = await expand_prompt(
                        user_request,
                        has_supabase=has_supabase,
                        image_b64=prompt_image_b64,
                        gorilla_proxy_url=self._gorilla_proxy_url,
                    )
                    self._prompt_expanded = True

                if not self._plan_injected and bool(file_tree):
                    plan = await generate_plan(
                        effective_request,
                        tree_str,
                        has_supabase=has_supabase,
                        image_b64=prompt_image_b64,
                        gorilla_proxy_url=self._gorilla_proxy_url,
                    )
                    if plan:
                        plan_text = (
                            "\n\nHere's a plan — follow it step by step. Each item "
                            "= one turn. Use parallel reads on explore turns; "
                            "respect the 3-write batch limit on implementation turns:\n"
                            + plan
                        )
                        self._plan_injected = True

            parts = [f"Project files in /home/user/app:\n{tree_str}{pkg_snippet}"]

            if is_debug and error_context:
                parts.append(f"\nError to fix:\n{error_context}")
            elif effective_request:
                parts.append(f"\nTask:\n{effective_request}{plan_text}")

            if chat_history:
                recent = chat_history[-6:]
                history_text = "\n".join(
                    f"{m.get('role','user').upper()}: {m.get('content','')[:200]}"
                    for m in recent if m.get("content")
                )
                if history_text:
                    parts.append(f"\nRecent conversation:\n{history_text}")

            user_text        = "\n".join(parts)
            first_turn_image = image_b64 or prompt_image_b64

            if first_turn_image:
                img_url = (
                    first_turn_image if first_turn_image.startswith("data:")
                    else f"data:image/jpeg;base64,{first_turn_image}"
                )
                self.messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text",      "text": user_text},
                        {"type": "image_url", "image_url": {"url": img_url}},
                    ],
                })
            else:
                self.messages.append({"role": "user", "content": user_text})

        # ─── DEEP-THINK OVERRIDE ─────────────────────────────────────
        # Detect the trigger phrase in this turn's user_request OR in any
        # earlier message of this agent (sticky). Once latched, every turn
        # — including vision and post-image turns — routes to DEEP_THINK_MODEL.
        if not self._deep_think and _matches_deep_think_trigger(user_request or ""):
            self._deep_think = True
            log_agent(
                "agent",
                f"DEEP-THINK trigger matched → routing to {DEEP_THINK_MODEL}",
                self.project_id,
            )

        first_turn_has_image = bool(
            (image_b64 or prompt_image_b64) and not previous_command_output
        )

        if self._deep_think:
            # Override: use the deep-think model for every turn, including
            # vision turns. The override takes precedence over the smart
            # picker and the vision-model branch — the user asked for
            # deliberate, deep reasoning, so we honour it everywhere.
            model = DEEP_THINK_MODEL
        elif first_turn_has_image:
            model = VISION_MODEL
        else:
            turn_index = max(0, len(self.messages) // 2 - 1)
            model = _pick_model(
                turn=turn_index,
                previous_output=previous_command_output,
                user_request=user_request,
                is_debug=is_debug,
            )

        log_agent(
            "agent",
            f"v18.1 model={model.split('/')[-1]} step={len(self.messages) // 2} "
            f"write_limit={WRITE_BATCH_LIMIT} supabase={has_supabase}",
            self.project_id,
        )

        try:
            raw_text, turn_tokens = await _call_llm(
                self.messages,
                model=model,
                temperature=0.6,
            )
            # ─── BILLING FIX: track per-turn delta cleanly ───
            self.total_tokens += turn_tokens
        except Exception as e:
            log_agent("agent", f"LLM error: {e}", self.project_id)
            return {
                "message":     f"AI error: {str(e)[:150]}",
                "write_files": [],
                "edit_files":  [],
                "read_calls":  [],
                "commands":    [],
                "done":        True,
                "tokens":      self.total_tokens,
                "turn_tokens": 0,
            }

        self.messages.append({"role": "assistant", "content": raw_text or ""})

        parsed = _parse_response(raw_text)

        if parsed["thought"]:
            log_agent("agent", f"THOUGHT: {parsed['thought'][:300]}", self.project_id)

        if parsed["extra_writes_dropped"] > 0:
            log_agent(
                "agent",
                f"⚠ Dropped {parsed['extra_writes_dropped']} extra writes (limit = {WRITE_BATCH_LIMIT})",
                self.project_id,
            )

        safe_bash = ""
        if parsed["bash"]:
            bash_content = self.token_sub.expand(parsed["bash"])
            if _is_safe(bash_content):
                safe_bash = bash_content
            else:
                log_agent("agent", "Blocked dangerous command", self.project_id)

        for wf in parsed["write_files"]:
            wf["content"] = self.token_sub.expand(wf["content"])
        for ef in parsed["edit_files"]:
            ef["old_str"] = self.token_sub.expand(ef["old_str"])
            ef["new_str"] = self.token_sub.expand(ef["new_str"])

        done   = parsed["done"]
        n_writes = len(parsed["write_files"])
        n_edits  = len(parsed["edit_files"])
        n_reads  = len(parsed["read_calls"])
        log_agent(
            "agent",
            f"{'DONE' if done else f'writes={n_writes} edits={n_edits} reads={n_reads} bash={bool(safe_bash)}'} "
            f"turn={turn_tokens}µ$ total={self.total_tokens}µ$",
            self.project_id,
        )

        return {
            "message":      parsed["message"],
            "write_files":  parsed["write_files"],
            "edit_files":   parsed["edit_files"],
            "read_calls":   parsed["read_calls"],
            "commands":     [safe_bash] if safe_bash else [],
            "done":         done,
            "tokens":       self.total_tokens,
            "turn_tokens":  turn_tokens,
            "user_action":  parsed.get("user_action"),   # ← ADD THIS
            "_parsed":      parsed,
        }

    def record_tool_results(
        self,
        parsed:            Dict[str, Any],
        write_observation: str = "",
        read_observation:  str = "",
        bash_observation:  str = "",
    ) -> None:
        write_paths = [wf.get("path", "") for wf in parsed.get("write_files", []) if wf.get("path")]
        edit_paths  = [ef.get("path", "") for ef in parsed.get("edit_files",  []) if ef.get("path")]
        if write_observation and not bash_observation and not read_observation:
            bash_observation = write_observation
        self._append_observation(write_paths, edit_paths, read_observation, bash_observation)


# ---------------------------------------------------------------------------
# Legacy shim
# ---------------------------------------------------------------------------
class Agent:
    def __init__(self, timeout_s: float = 120.0):
        self.timeout_s = timeout_s

    def remember(self, project_id: str, role: str, text: str) -> None:
        _append_history(project_id, role, text)


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