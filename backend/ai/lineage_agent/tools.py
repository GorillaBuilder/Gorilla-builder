from __future__ import annotations

from .config import WRITE_BATCH_LIMIT, TOTAL_BATCH_LIMIT

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
    {
        "name": "preview_screenshot",
        "category": "read",
        "description": (
            "Take a screenshot of the running dev server's preview in a headless "
            "browser. Use this to visually verify UI changes — layout, styling, "
            "whether a page renders at all. The screenshot comes back to you as an "
            "image on the NEXT turn. Requires the dev server to already be running."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Route to screenshot, e.g. '/' or '/login'. Defaults to '/'.",
                },
            },
            "required": [],
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

    # ─── DELEGATION ────────────────────────────────────────────────────
    {
        "name": "spawn_subagent",
        "category": "delegate",
        "description": (
            "Delegate a bounded, scoped sub-task to a fresh nested agent that "
            "operates on the SAME project files. Use this to investigate or "
            "implement a self-contained piece of work off the main thread — e.g. "
            "'find every place that imports the old auth hook' or 'implement the "
            "settings page per this spec'. The sub-agent has its own turn budget "
            "and its own message history (it does not see this conversation); give "
            "it a complete, self-contained task description. It CANNOT spawn "
            "further sub-agents. You may call this MULTIPLE TIMES in the same "
            "<tool_call> for independent tasks — they run concurrently, not one "
            "after another, so batch every independent investigation together "
            "instead of spawning them one turn at a time. Each returns a summary "
            "of what it did, which files it touched, and its final message — not "
            "the full sub-conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The complete task for the sub-agent to investigate or implement. Be specific and self-contained.",
                },
                "max_turns": {
                    "type": "string",
                    "description": "Cap the sub-agent's turns, default 5, max 8.",
                },
            },
            "required": ["task"],
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
        "  - READ TOOLS (read_files, list_dir, grep_search, glob_files, preview_screenshot): unlimited per turn.",
        "  - USER ACTION TOOLS (connect_supabase, set_env_vars): call ALONE, one per turn.",
        "    After calling one, stop — the agent loop pauses until the user responds.",
        "  - DELEGATION TOOLS (spawn_subagent): may call MULTIPLE TIMES in one turn for",
        "    independent tasks — they run concurrently. Do not mix with write/read/bash",
        "    tools in the same turn. After calling it, stop — the loop pauses until",
        "    every spawned sub-agent finishes.",
        f"  - Total functions per <tool_call> capped at {TOTAL_BATCH_LIMIT}.",
        "  - Brief plain-text reasoning is allowed BEFORE <tool_call>, never after.",
        "  - After </tool_call>, stop — results come back next turn.",
    ])
    return "\n".join(parts)
