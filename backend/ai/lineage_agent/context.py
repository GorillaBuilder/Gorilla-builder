from __future__ import annotations

import re
from typing import Any, List

from .config import CHARS_PER_TOKEN, MAX_CONTEXT_TOKENS

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
